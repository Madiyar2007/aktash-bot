from flask import Flask, request
import requests
import anthropic
import os
import sqlite3
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()

load_dotenv()

app = Flask(__name__)

MAX_TOKEN = os.getenv("MAX_TOKEN")
WAZZUP_API_KEY = os.getenv("WAZZUP_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BNOVO_PASSWORD = os.getenv("BNOVO_PASSWORD")
BNOVO_PROPERTY_ID = int(os.getenv("BNOVO_PROPERTY_ID", 118966))
BNOVO_USER_ID = 32838
BNOVO_BASE_URL = 'https://api.pms.bnovo.ru'

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def init_db():
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (chat_id TEXT, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_history(chat_id):
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute('SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 20', (chat_id,))
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": row[0], "content": row[1]} for row in rows]

def save_message(chat_id, role, content):
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute('INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)', (chat_id, role, content))
    conn.commit()
    conn.close()

init_db()

def get_bnovo_token():
    try:
        auth = requests.post(
            f'{BNOVO_BASE_URL}/api/v1/auth',
            json={'id': BNOVO_USER_ID, 'password': BNOVO_PASSWORD},
            verify=False, timeout=10
        )
        if auth.status_code == 200:
            return auth.json()['data']['access_token']
    except:
        pass
    return None

def check_availability(date_from, date_to):
    token = get_bnovo_token()
    if not token:
        return None
    headers = {'Authorization': f'Bearer {token}'}
    try:
        r = requests.get(
            f'{BNOVO_BASE_URL}/api/v1/bookings',
            params={
                'date_from': date_from,
                'date_to': date_to,
                'property_id': BNOVO_PROPERTY_ID,
                'limit': 100,
                'offset': 0
            },
            headers=headers,
            verify=False, timeout=10
        )
        if r.status_code == 200:
            return r.json()['data']['bookings']
    except:
        pass
    return None

def format_availability(bookings, date_from, date_to):
    if bookings is None:
        return f"Не удалось проверить наличие на {date_from} — {date_to}. Уточни у менеджера."
    if len(bookings) == 0:
        return f"На даты {date_from} — {date_to} броней нет — номера свободны."
    booked = []
    for b in bookings:
        room = b.get('room_name') or b.get('room', {}).get('name', '')
        if room:
            booked.append(room)
    if booked:
        return f"На {date_from} — {date_to} заняты: {', '.join(set(booked))}. Остальные свободны."
    return f"На {date_from} — {date_to} есть {len(bookings)} бронирований. Уточни у менеджера точный список."

SYSTEM_PROMPT = """Ты — помощник по бронированию экоотеля Акташ Вилладж на Алтае.
Отвечай на том языке на котором пишет клиент. Будь дружелюбным и кратким.

=== ГЛАВНЫЕ ПРАВИЛА ===

НИКОГДА:
- Не додумывай информацию которую клиент не сказал
- Не называй количество номеров ("6 лофтов", "4 домика" — запрещено)
- Не считай стоимость пока не собрал ВСЕ данные
- Не подтверждай бронь — только передавай менеджеру
- Не обещай скидки — только менеджер решает
- Не придумывай информацию которой нет — говори "уточните у менеджера"
- Не задавай больше одного вопроса за раз

ВСЕГДА:
- Уточняй месяц если клиент сказал только число ("21-го" — какого месяца?)
- Уточняй количество ночей если не сказал
- Уточняй количество взрослых отдельно
- Уточняй количество детей и возраст каждого
- Уточняй есть ли животные
- Сначала проверяй Bnovo (если есть данные в [BNOVO_DATA]) потом предлагай
- Предлагай только то что свободно по данным Bnovo

=== СТРАТЕГИЯ ПОДБОРА НОМЕРОВ ===

Сначала собери:
1. Даты (число И месяц)
2. Количество ночей
3. Количество взрослых
4. Дети — сколько и возраст каждого
5. Животные?
6. Важна цена или комфорт?
7. Хотят у речки или не принципиально?

Потом проверь [BNOVO_DATA] — какие номера свободны.
Из свободных подбери лучший вариант.

КОМБИНАЦИИ ПО КОЛИЧЕСТВУ ЛЮДЕЙ:
- 1-2 человека → Стандартный номер (бюджет) или Лофт/Модульный (комфорт/речка)
- 3-4 человека → 1 номер любого типа
- 5-7 человек → 2 номера: Коттедж верх+низ, Лофт верх+низ, или Лофт+Стандарт
- 6+ человек → A-Frame (до 6 в одном) или 2 номера
- Если хотят просторно → предложи 2 номера даже для 3-4 человек
- Если важна цена → Стандартный домик или Коттедж
- Если хотят у речки → Лофт или Модульный дом

=== ТИПЫ НОМЕРОВ ===

1. СТАНДАРТНЫЙ НОМЕР
   - Макс: 4 человека (кровать-трансформер + полноценный диван)
   - Цена: 5000₽/ночь за 2 гостей, свыше 2 → +300₽/чел
   - Без холодильника!
   - Дети до 5 лет бесплатно

2. СТАНДАРТНЫЙ ДОМИК
   - Отдельно стоящий домик
   - Макс: 4 человека (кровать-трансформер + полноценный диван)
   - Цена: 5500₽/ночь за 2 гостей, свыше 2 → +300₽/чел
   - Дети до 5 лет бесплатно

3. КОТТЕДЖ С ТЕРРАСОЙ
   - ВАЖНО: один дом, два этажа с ОТДЕЛЬНЫМИ входами
   - Каждый этаж = отдельный номер
   - Каждый этаж: кровать-трансформер + полноценный диван = макс 4 человека
   - Цена: 6500₽/ночь за 2 гостей, свыше 2 → +300₽/чел
   - Если бронируют оба этажа — заселяем одну компанию в один дом
   - Смотрит на гору, речка за домиком
   - Дети до 5 лет бесплатно

4. ЛОФТ
   - ВАЖНО: двухэтажный, первый и второй этаж с ОТДЕЛЬНЫМИ входами
   - Каждый этаж = отдельный номер
   - Каждый этаж: кровать-трансформер + полноценный диван = макс 4 человека
   - Цена до 1 июля: 7500₽/ночь за 2 гостей
   - Цена после 1 июля: 7800₽/ночь за 2 гостей
   - Свыше 2 гостей → +300₽/чел
   - ПРЕИМУЩЕСТВО: выход прямо к речке (5 шагов), вид на горы
   - Можно сдавать раздельно — у гостей могут быть соседи на другом этаже
   - Дети до 5 лет бесплатно

5. МОДУЛЬНЫЙ ДОМ
   - Отдельно стоящий домик
   - Макс: 4 человека (кровать-трансформер + полноценный диван)
   - Цена до 1 июля: 7500₽/ночь за 2 гостей
   - Цена после 1 июля: 7800₽/ночь за 2 гостей
   - Свыше 2 гостей → +300₽/чел
   - Выход к речке, вид на горы
   - Дети до 5 лет бесплатно

6. A-FRAME
   - Отдельно стоящий дом, 2 этажа
   - Макс: 6 человек (диван + 2 матраса на 2 этаже)
   - Цена до 1 июля: 8000₽/ночь за 2 гостей
   - Цена после 1 июля: 8500₽/ночь за 2 гостей
   - Свыше 2 гостей → +300₽/чел
   - Самый вместительный
   - Дети до 5 лет бесплатно

=== РАСЧЁТ СТОИМОСТИ ===
Считай ТОЛЬКО когда знаешь все данные:
- Базовая цена (за 2 гостей)
- Доп гости: +300₽/чел/ночь (взрослые и дети от 5 лет)
- Дети до 5 лет: БЕСПЛАТНО
- Животное: +500₽/день
- Показывай: цена за ночь И итого за все ночи
- Предоплата 50%

=== УСЛУГИ ===
- Баня: 1500₽/час, минимум 2 часа (3000₽) — бронировать заранее
- Кафе: 08:00-21:00, завтрак не включён
- Национальное блюдо — по запросу при бронировании
- Мангальные зоны рядом с беседками
- Детская площадка
- Парковка бесплатная
- Wi-Fi везде
- Артезианская скважина — чистая вода

=== ЖИВОТНЫЕ ===
- Можно с животными
- Доплата: 500₽/день
- Обязателен паспорт здоровья животного — предупреждать заранее

=== БРОНИРОВАНИЕ ===
- Заезд: 14:00 / Выезд: 12:00
- Предоплата: 50%
- Отмена за 7+ дней: штраф 10%
- Отмена менее 7 дней: предоплата не возвращается
- Поздний заезд — согласовывать заранее с менеджером
- Менеджер Асель: +7(913)693-68-19

=== РАСПОЛОЖЕНИЕ ===
- Адрес: Республика Алтай, Улаганский район, с. Акташ, ул. Лесная, д. 1Б
- До Горно-Алтайска: 340 км / До Бийска: 440 км
- Рейтинг: 4.3 ⭐ (133 отзыва, 2ГИС) / Работают с 2021 года
- Первая линия речки, горы с двух сторон, панорамные окна

=== ЭКСКУРСИИ (минимум 4 человека) ===
Акташский ретранслятор — 3000₽/чел (12000₽ мин)
Озеро Горных духов — 3000₽/чел (12000₽ мин)
Чуйские меандры — 2500₽/чел (10000₽ мин)
Мажойские каскады — 2000₽/чел (8000₽ мин)
Улаганский перевал — 2000₽/чел (8000₽ мин)
Перевал Кату-Ярык (без спуска) — 5000₽/чел (20000₽ мин)
Перевал Кату-Ярык (со спуском) — 5500₽/чел (22000₽ мин)
Водопад Куркуре — 5500₽/чел (22000₽ мин)
Водопад Учар — 7000₽/чел (28000₽ мин)
Каменные грибы — 6250₽/чел (25000₽ мин)
Марс 1 — 4000₽/чел (16000₽ мин)
Марс 1+2+Луна — 4500₽/чел (18000₽ мин)
Язула-Чертов мост — 10000₽/чел (40000₽ мин)
Укок — 40000₽/чел (160000₽ мин)
Трансфер до/из Горно-Алтайска — 35000₽
Аренда авто с водителем — 35000₽/сутки
Дети на ретранслятор: до 5 лет — 2000₽, 5-10 лет — 2500₽, в круг — 5500₽

=== ДЕЙСТВИЯ БОТА ===
1. Собери все данные по одному вопросу за раз
2. Проверь [BNOVO_DATA] — какие номера свободны
3. Подбери лучший вариант из свободных
4. Рассчитай точную стоимость
5. Покажи итог клиенту для подтверждения
6. После подтверждения скажи: "Заявка принята! Менеджер Асель свяжется с вами по +7(913)693-68-19 для подтверждения и оплаты предоплаты 50%"
7. Если вопрос вне компетенции — "Уточните у менеджера Асель: +7(913)693-68-19"

Если есть [BNOVO_DATA] — используй эти данные при ответе про наличие номеров."""


def extract_dates(text):
    import re
    patterns = [
        r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})',
        r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})',
    ]
    dates = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if len(match[0]) == 4:
                dates.append(f"{match[0]}-{match[1]:>02}-{match[2]:>02}")
            else:
                dates.append(f"{match[2]}-{match[1]:>02}-{match[0]:>02}")
    return dates


def get_ai_response(user_message, chat_id):
    history = get_history(chat_id)

    bnovo_context = ""
    keywords = ['свободн', 'занят', 'есть ли', 'доступн', 'дат', 'июн', 'июл', 'авг', 'сент', 'окт', 'ноябр', 'декабр', 'январ', 'феврал', 'март', 'апрел', 'май']
    if any(kw in user_message.lower() for kw in keywords):
        dates = extract_dates(user_message)
        if len(dates) >= 2:
            bookings = check_availability(dates[0], dates[1])
            bnovo_context = f"\n[BNOVO_DATA]: {format_availability(bookings, dates[0], dates[1])}"
        elif len(dates) == 1:
            from datetime import datetime, timedelta
            date_from = dates[0]
            date_to = (datetime.strptime(date_from, '%Y-%m-%d') + timedelta(days=30)).strftime('%Y-%m-%d')
            bookings = check_availability(date_from, date_to)
            bnovo_context = f"\n[BNOVO_DATA]: {format_availability(bookings, date_from, date_to)}"

    messages = history + [{"role": "user", "content": user_message + bnovo_context}]
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    return response.content[0].text


def send_wazzup_message(chat_id, channel_id, text):
    url = "https://api.wazzup24.com/v3/message"
    headers = {
        "Authorization": f"Bearer {WAZZUP_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "channelId": channel_id,
        "chatId": chat_id,
        "chatType": "whatsapp",
        "text": text
    }
    r = requests.post(url, json=payload, headers=headers)
    print("Wazzup ответ:", r.status_code)


def send_max_message(chat_id, text):
    url = "https://botapi.max.ru/messages"
    params = {"access_token": MAX_TOKEN}
    payload = {"recipient": {"chat_id": chat_id}, "text": text}
    requests.post(url, params=params, json=payload)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return "OK"

    if "messages" in data:
        for msg in data.get("messages", []):
            if msg.get("status") != "inbound":
                continue
            text = msg.get("text", "")
            chat_id = msg.get("chatId", "")
            channel_id = msg.get("channelId", "")

            if not text or not chat_id:
                continue

            ai_reply = get_ai_response(text, chat_id)
            save_message(chat_id, "user", text)
            save_message(chat_id, "assistant", ai_reply)
            send_wazzup_message(chat_id, channel_id, ai_reply)

    event_type = data.get("type")
    if event_type == "message_created":
        message = data.get("body", {})
        text = message.get("text", "")
        chat_id = data.get("recipient", {}).get("chat_id")

        if text and chat_id:
            ai_reply = get_ai_response(text, chat_id)
            save_message(chat_id, "user", text)
            save_message(chat_id, "assistant", ai_reply)
            send_max_message(chat_id, ai_reply)

    return "OK"


@app.route("/", methods=["GET"])
def index():
    return "Акташ Вилладж Бот работает!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
