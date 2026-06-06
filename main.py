import sys
from flask import Flask, request
import requests
import anthropic
import os
import sqlite3
import time
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()

load_dotenv()

app = Flask(__name__)

MAX_TOKEN = os.getenv("MAX_TOKEN")
WAZZUP_API_KEY = os.getenv("WAZZUP_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BNOVO_PASSWORD = os.getenv("BNOVO_PASSWORD")
BNOVO_USER_ID = 118966
BNOVO_BASE_URL = 'https://api.pms.bnovo.ru'

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)



GITHUB_PHOTOS = "https://raw.githubusercontent.com/Madiyar2007/aktash-bot/main/photos"

ROOM_PHOTOS = {
    "loft":     [f"{GITHUB_PHOTOS}/loft/{i}.jpg" for i in range(1, 4)],
    "aframe":   [f"{GITHUB_PHOTOS}/aframe/{i}.jpg" for i in range(1, 7)],
    "kottedzh": [f"{GITHUB_PHOTOS}/kottedzh/{i}.jpg" for i in range(1, 6)],
    "modul":    [f"{GITHUB_PHOTOS}/modul/{i}.jpg" for i in range(1, 4)],
    "domik":    [f"{GITHUB_PHOTOS}/domik/{i}.jpg" for i in range(1, 5)],
    "standart": [f"{GITHUB_PHOTOS}/nomer_standart/{i}.jpg" for i in range(1, 3)],
}

ROOM_KEYWORDS = {
    "loft":     ["лофт"],
    "aframe":   ["a-frame", "афрейм", "а-фрейм", "aframe"],
    "kottedzh": ["коттедж"],
    "modul":    ["модуль", "модульный"],
    "domik":    ["домик стандарт", "стандартный домик"],
    "standart": ["номер стандарт", "стандартный номер"],
}

ROOM_TYPES = {
    428964: "Модуль",
    428965: "Лофт",
    428966: "A-Frame",
    428967: "Домик Стандарт",
    428969: "Коттедж с террасой",
    747057: "Номер Стандарт",
}

def detect_room_type(text):
    text_low = text.lower()
    for room_type, keywords in ROOM_KEYWORDS.items():
        for kw in keywords:
            if kw in text_low:
                return room_type
    return None

def init_db():
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    # FIX #4: явный автоинкрементный id — сортируем по нему, а не по timestamp (точность до секунды ломала порядок ролей)
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT, role TEXT, content TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    # FIX #5: дедуп по реальному id сообщения, а не "любое сообщение от чата за 5 сек"
    c.execute('''CREATE TABLE IF NOT EXISTS seen_messages
                 (message_id TEXT PRIMARY KEY, ts REAL)''')
    conn.commit()
    conn.close()

def get_history(chat_id):
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    # FIX #4: ORDER BY id (а не timestamp) — стабильный порядок вопрос/ответ
    c.execute('SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 20', (chat_id,))
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

def already_processed(message_id):
    """FIX #5: True если это сообщение уже обрабатывали (защита от ретраев вебхука Wazzup)."""
    now = time.time()
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute('DELETE FROM seen_messages WHERE ts < ?', (now - 3600,))  # лёгкая чистка старого
    c.execute('SELECT 1 FROM seen_messages WHERE message_id = ?', (message_id,))
    seen = c.fetchone() is not None
    if not seen:
        c.execute('INSERT OR IGNORE INTO seen_messages (message_id, ts) VALUES (?, ?)', (message_id, now))
    conn.commit()
    conn.close()
    return seen

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
        sys.stderr.write(f"BNOVO auth status={auth.status_code} {auth.text[:200]}\n"); sys.stderr.flush()
    except Exception as e:
        # FIX #7: не глотаем ошибку молча
        sys.stderr.write(f"BNOVO auth error: {e}\n"); sys.stderr.flush()
    return None

def check_availability_by_type(date_from, date_to):
    token = get_bnovo_token()
    if not token:
        return None
    headers = {'Authorization': f'Bearer {token}'}
    try:
        r = requests.get(
            f'{BNOVO_BASE_URL}/api/v1/availability/roomtypes',
            params={'date_from': date_from, 'date_to': date_to},
            headers=headers, verify=False, timeout=10
        )
        if r.status_code == 200:
            return r.json()['data']
        sys.stderr.write(f"BNOVO availability status={r.status_code} {r.text[:200]}\n"); sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"BNOVO availability error: {e}\n"); sys.stderr.flush()
    return None

def format_availability(data, date_from, date_to):
    if data is None:
        return f"Данные о наличии недоступны на {date_from} - {date_to}."
    result = []
    for type_id_str, info in data.items():
        type_id = int(type_id_str)
        name = ROOM_TYPES.get(type_id, f"Тип {type_id}")
        full = info.get('full_quantity', 0)
        if full == 0:
            continue
        avail_values = list(info.get('availability', {}).values())
        min_avail = min(avail_values) if avail_values else 0
        if min_avail > 0:
            result.append(f"{name}: есть")
        else:
            result.append(f"{name}: занят")
    if not result:
        return f"На {date_from} - {date_to} свободных номеров нет."
    return "Наличие на " + date_from + " - " + date_to + ": " + "; ".join(result)

SYSTEM_PROMPT = """АБСОЛЮТНЫЕ ЗАПРЕТЫ:
- Никогда не говори клиенту "напишите на номер +7-913-693-68-19 за фото" — фото отправляются автоматически
- Никогда не говори что не можешь отправить фото — система умеет это делать
- Никогда не спрашивай про год (2025 или 2026)
- Никогда не говори "сбой системы" или "не могу проверить"
- Никогда не упоминай год в ответе — пиши просто "21 июня"
- Никогда не отвечай на транслите — только на русском языке
- Никогда не называй себя ботом или ИИ первым

Текущий год — 2026. Июнь, июль, август 2026 — обычные даты для бронирования.
Когда в [BNOVO_DATA] есть данные — используй их. Это реальные данные из системы.

Ты — Асель, менеджер эко-отеля Акташ Вилладж на Алтае.
Отвечай ТОЛЬКО на русском языке. Всегда.

СТИЛЬ (как живой менеджер):
- Короткие сообщения через |||
- Без "Отлично!", "С удовольствием!", "Замечательно!"
- На простой вопрос — 2-5 слов
- Один вопрос за раз
- Цены формулой: 7500+7500=15 000
- Эмодзи максимум одно за весь разговор

ПЕРВОЕ СООБЩЕНИЕ:
Если история пустая — просто: Здравствуйте! ||| Чем могу помочь?
Если в первом сообщении уже есть вопрос — поздоровайся и сразу ответь.
Не здоровайся повторно если диалог уже идёт.

НАЛИЧИЕ НОМЕРОВ:
Используй [BNOVO_DATA] — там реальные данные.
Предлагай только те типы где написано "есть".
Не называй количество свободных номеров.

СБОР ДАННЫХ для брони: даты, ночи, взрослые, дети и возраст, животные.
Считай стоимость только когда знаешь всё.

ЗВОНИТЬ АСЕЛИ +7-913-693-68-19:
- Группа 10+ человек — Позвоните — +7-913-693-68-19
- Жалоба или конфликт — Позвоните — +7-913-693-68-19
- Договор/счёт для организации — Позвоните — +7-913-693-68-19
- Скидки — Цены фиксированные
- Проблема при заезде — Позвоните прямо сейчас — +7-913-693-68-19

НОМЕРА:
Номер Стандарт: макс 4, 5000р/ночь за 2, +300р/чел, без холодильника
Домик Стандарт: макс 4, 5500р/ночь за 2, +300р/чел
Коттедж с террасой: 2 этажа отд входы, этаж макс 4, 6500р/ночь за 2, +300р, вид на гору, речка за домом
Лофт: 2 этажа отд входы, этаж макс 4, до 1 июля 7500р после 7800р за 2, +300р, выход к речке
Модуль: макс 4, до 1 июля 7500р после 7800р за 2, +300р, выход к речке
A-Frame: макс 6, до 1 июля 8000р после 8500р за 2, +300р, самый вместительный
Везде: кровать-трансформер, диван, туалет, душ, фен, чайник, посуда, WiFi, холодильник (кроме Номер Стандарт).
Дети до 5 лет бесплатно, от 5 лет +300р.
Животные: 500р/день + паспорт здоровья.

УСЛУГИ:
Баня 1500р/час мин 2 часа. Кафе 08:00-21:00.
Беседки у речки, мангалы, костровище, детская площадка, парковка бесплатно.

БРОНИРОВАНИЕ:
Заезд 14:00, выезд 12:00. Предоплата 50%.
Отмена 7+ дней — штраф 10%, менее 7 дней — предоплата не возвращается.
Для брони: ФИО, телефон, даты.
Реквизиты: 89833275585 Бегенов Бекжан М. Сбербанк

МЕСТО: Республика Алтай, Улаганский район, с. Акташ, ул. Лесная 1Б. Первая линия реки Чуя, горы вокруг.

ЭКСКУРСИИ (мин 4 чел):
Ретранслятор 3000р, Озеро Горных духов 3000р, Чуйские меандры 2500р,
Мажойские каскады 2000р, Улаганский перевал 2000р, Кату-Ярык 5000-5500р,
Куркуре 5500р, Учар 7000р, Каменные грибы 6250р, Марс 4000-4500р.
Трансфер Горно-Алтайск 35000р.

ПРИМЕРЫ ДИАЛОГОВ (стиль Асель):

Клиент: Добрый день! Хотела забронировать. С нами собачка.
Асель: Добрый день! Да можно с паспортом здоровья. ||| Даты напишите какие вас интересует?

Клиент: С 9 по 13 июня, семья 4 человека
Асель: Здравствуйте ||| К сожалению все занято

Клиент: Комната на 6 человек, июль
Асель: Здравствуйте! Все вместе хотели 1 домик? ||| Если да — могу предложить A-Frame 8500р ||| Либо Коттедж с террасой 2 номера — 6500+6500=13 000

Клиент: Стоянка есть?
Асель: Да, на территории

Клиент: Баня есть?
Асель: Да, 1500р/час, минимум 2 часа

Клиент: Кондиционер есть?
Асель: Нету. Они и так в тени, жары нет"""

MONTHS = {
    'январ': '01', 'феврал': '02', 'март': '03', 'апрел': '04', 'мая': '05', 'май': '05',
    'июн': '06', 'июл': '07', 'август': '08', 'авг': '08', 'сентябр': '09', 'сент': '09',
    'октябр': '10', 'окт': '10', 'ноябр': '11', 'декабр': '12'
}

def _month_from_word(word):
    for key, num in MONTHS.items():
        if word.startswith(key):
            return int(num)
    return None

def _make_date(day, month, today):
    """FIX #3: год выбираем динамически. Если дата в этом году уже прошла — берём следующий."""
    for year in (today.year, today.year + 1):
        try:
            d = datetime(year, month, day)
        except ValueError:
            return None  # невалидный день/месяц (напр. 31 июня) — для любого года невалиден
        if d.date() >= (today.date() - timedelta(days=1)):
            return d.strftime('%Y-%m-%d')
    return None

def extract_dates(text):
    text_low = text.lower()
    today = datetime.now()
    dates = []

    # 1) Явные числовые даты: 21.06.2026 / 21-06-26 / 21/6/2026
    for m in re.findall(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})', text):
        day, month = int(m[0]), int(m[1])
        year = int(m[2])
        year += 2000 if year < 100 else 0
        try:
            dates.append(datetime(year, month, day).strftime('%Y-%m-%d'))
        except ValueError:
            pass
    if dates:
        return dates

    # 2) FIX #2: диапазон с общим месяцем — "с 9 по 13 июня", "9-13 июня"
    rng = re.search(r'(\d{1,2})\s*(?:по|до|[-–—])\s*(\d{1,2})\s*([а-я]+)', text_low)
    if rng:
        month = _month_from_word(rng.group(3))
        if month:
            d1 = _make_date(int(rng.group(1)), month, today)
            d2 = _make_date(int(rng.group(2)), month, today)
            if d1 and d2:
                return [d1, d2]

    # 3) Одиночные пары "число месяц" — "13 июня", "с 9 июня по 13 июля"
    for m in re.finditer(r'(\d{1,2})\s*([а-я]+)', text_low):
        month = _month_from_word(m.group(2))
        if month:
            d = _make_date(int(m.group(1)), month, today)
            if d:
                dates.append(d)
    return dates

def analyze_image(image_url):
    try:
        img_response = requests.get(image_url, timeout=10, verify=False)
        if img_response.status_code != 200:
            return None
        import base64
        image_data = base64.b64encode(img_response.content).decode('utf-8')
        content_type = img_response.headers.get('content-type', 'image/jpeg').split(';')[0]
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            system="Ты менеджер эко-отеля Акташ Вилладж. Клиент прислал фото. Определи что на фото: чек об оплате (извлеки сумму), паспорт (извлеки ФИО), или другое фото (опиши кратко). Отвечай коротко на русском.",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": image_data}},
                {"type": "text", "text": "Что на этом фото?"}
            ]}]
        )
        return response.content[0].text
    except Exception as e:
        sys.stderr.write(f"Image error: {e}\n")
        return None

def get_ai_response(user_message, chat_id):
    history = get_history(chat_id)
    bnovo_context = ""
    dates = extract_dates(user_message)
    if not dates:
        for h in reversed(history):
            if h['role'] == 'user':
                found = extract_dates(h['content'])
                if found:
                    dates = found
                    break
    if dates:
        date_from = dates[0]
        date_to = (datetime.strptime(date_from, '%Y-%m-%d') + timedelta(days=3)).strftime('%Y-%m-%d') if len(dates) < 2 else dates[1]
        data = check_availability_by_type(date_from, date_to)
        bnovo_context = f"\n[BNOVO_DATA]: {format_availability(data, date_from, date_to)}"
    sys.stderr.write(f"DEBUG dates={dates} | bnovo={bnovo_context[:150]}\n"); sys.stderr.flush()
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
    headers = {"Authorization": f"Bearer {WAZZUP_API_KEY}", "Content-Type": "application/json"}
    payload = {"channelId": channel_id, "chatId": chat_id, "chatType": "whatsapp", "text": text}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)  # FIX #7: timeout
        return r.status_code
    except Exception as e:
        sys.stderr.write(f"Wazzup send error: {e}\n"); sys.stderr.flush()
        return None

def send_wazzup_photo(chat_id, channel_id, image_url):
    url = "https://api.wazzup24.com/v3/message"
    headers = {"Authorization": f"Bearer {WAZZUP_API_KEY}", "Content-Type": "application/json"}
    payload = {"channelId": channel_id, "chatId": chat_id, "chatType": "whatsapp", "contentUri": image_url}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)  # FIX #7: timeout
        sys.stderr.write(f"PHOTO: {r.status_code} {image_url[-30:]}\n"); sys.stderr.flush()
        return r.status_code
    except Exception as e:
        sys.stderr.write(f"Wazzup photo error: {e}\n"); sys.stderr.flush()
        return None

def send_room_photos(chat_id, channel_id, room_type):
    photos = ROOM_PHOTOS.get(room_type, [])
    for photo_url in photos:
        time.sleep(0.5)
        send_wazzup_photo(chat_id, channel_id, photo_url)

def send_wazzup_multi(chat_id, channel_id, full_text):
    parts = [p.strip() for p in full_text.split("|||") if p.strip()]
    for part in parts:
        time.sleep(min(len(part) * 0.04, 2.5))
        send_wazzup_message(chat_id, channel_id, part)
        time.sleep(0.4)
    sys.stderr.write(f"Wazzup: отправлено {len(parts)} сообщений\n"); sys.stderr.flush()

def send_max_message(chat_id, text):
    url = "https://botapi.max.ru/messages"
    params = {"access_token": MAX_TOKEN}
    payload = {"recipient": {"chat_id": chat_id}, "text": text}
    try:
        requests.post(url, params=params, json=payload, timeout=15)  # FIX #7: timeout
    except Exception as e:
        sys.stderr.write(f"Max send error: {e}\n"); sys.stderr.flush()

def send_max_multi(chat_id, full_text):
    parts = [p.strip() for p in full_text.split("|||") if p.strip()]
    for part in parts:
        time.sleep(min(len(part) * 0.04, 2.5))
        send_max_message(chat_id, part)
        time.sleep(0.4)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)  # FIX #7: не падаем на кривом payload
    if not data:
        return "OK"
    sys.stderr.write(f"WEBHOOK: {str(data)[:300]}\n"); sys.stderr.flush()
    if "messages" in data:
        for msg in data.get("messages", []):
            sys.stderr.write(f"MSG status={msg.get('status')} type={msg.get('type')} text={msg.get('text','')[:50]}\n"); sys.stderr.flush()
            if msg.get("status") != "inbound":
                continue
            if msg.get("chatType") == "whatsgroup":
                continue
            chat_id = msg.get("chatId", "")
            channel_id = msg.get("channelId", "")
            if not chat_id:
                continue

            # FIX #5: дедуп по id сообщения (а не по chat_id+5сек, который глушил быстрые follow-up)
            msg_id = msg.get("messageId") or f"{chat_id}:{msg.get('text','')[:30]}:{int(time.time())}"
            if already_processed(msg_id):
                sys.stderr.write(f"SKIP duplicate msg_id={msg_id}\n"); sys.stderr.flush()
                continue

            msg_type = msg.get("type", "text")

            # Аудио
            if msg_type in ("audio", "voice", "ptt"):
                send_wazzup_message(chat_id, channel_id, "Голосовые не принимаю — напишите текстом")
                continue

            # Фото от клиента
            if msg_type in ("image", "photo"):
                image_url = msg.get("fileUrl") or msg.get("url") or msg.get("imageUrl", "")
                if image_url:
                    image_reply = analyze_image(image_url)
                    if image_reply:
                        caption = msg.get("text", "") or msg.get("caption", "")
                        prompt = f"[Клиент прислал фото. Содержимое: {image_reply}]"
                        if caption:
                            prompt += f" Подпись: {caption}"
                        full_reply = get_ai_response(prompt, chat_id)
                        save_message(chat_id, "user", f"[фото: {image_reply[:100]}]")
                        save_message(chat_id, "assistant", full_reply)
                        send_wazzup_multi(chat_id, channel_id, full_reply)
                    else:
                        send_wazzup_message(chat_id, channel_id, "Фото получила, но не смогла открыть. Попробуйте ещё раз")
                continue

            text = msg.get("text", "")
            if not text:
                continue

            # Фото номеров по запросу
            room_type = detect_room_type(text)
            foto_keywords = ["фото", "покажи", "фотки", "посмотреть", "как выглядит", "покажите", "фотографии", "скинь", "скиньте", "пришли", "прислать", "загляни", "посмотри"]
            wants_photo = any(kw in text.lower() for kw in foto_keywords)
            if room_type and wants_photo:
                send_room_photos(chat_id, channel_id, room_type)
                time.sleep(0.5)

            ai_reply = get_ai_response(text, chat_id)
            save_message(chat_id, "user", text)
            save_message(chat_id, "assistant", ai_reply)
            send_wazzup_multi(chat_id, channel_id, ai_reply)

    event_type = data.get("type")
    if event_type == "message_created":
        message = data.get("body", {})
        text = message.get("text", "")
        chat_id = data.get("recipient", {}).get("chat_id")
        if text and chat_id:
            ai_reply = get_ai_response(text, chat_id)
            save_message(chat_id, "user", text)
            save_message(chat_id, "assistant", ai_reply)
            send_max_multi(chat_id, ai_reply)

    return "OK"

@app.route("/", methods=["GET"])
def index():
    return "Aktash Villadzh Bot rabotaet!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
