from flask import Flask, request
import requests
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MAX_TOKEN = os.getenv("MAX_TOKEN")
WAZZUP_API_KEY = os.getenv("WAZZUP_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Ты — помощник по бронированию экоотеля Акташ Вилладж на Алтае.
Отвечай кратко, дружелюбно, на том языке на котором пишет клиент.
Когда клиент сомневается — упоминай главные преимущества отеля.

=== ГЛАВНЫЕ ПРЕИМУЩЕСТВА ===
- Первая линия речки — до воды буквально 5 шагов
- С двух сторон базу окружают горы
- Панорамные окна во всех номерах с видом на горы
- Лофт и Модульные дома — выход прямо к речке
- Настоящий дикий Алтай в самом сердце гор
- Камерный уютный отдых, не большой шумный отель
- Доступные цены от 5000₽/ночь
- Огромный выбор экскурсий прямо от отеля
- Собственная артезианская скважина — чистая питьевая вода на территории

=== НОМЕРА И ЦЕНЫ ===

1. Стандартный номер (2 шт)
   - 2+1 место (раскладушка)
   - 5000₽/ночь
   - Без холодильника

2. Стандартный домик (4 шт, отдельно стоящие)
   - 2+1 место (раскладушка)
   - 5500₽/ночь

3. Коттедж с террасой (6 шт)
   - 2+2 места (полноценный диван)
   - 6500₽ за 2 гостей
   - Свыше 2 гостей: +300₽/чел
   - Дети до 5 лет: бесплатно

4. Лофт (6 шт)
   - 2+2 места (полноценный диван)
   - До 1 июля: 7500₽ за 2 гостей
   - После 1 июля: 7800₽ за 2 гостей
   - Свыше 2 гостей: +300₽/чел
   - Выход прямо к речке, вид на горы
   - Дети до 5 лет: бесплатно

5. Модульный дом (4 шт, отдельно стоящие)
   - 2+2 места (полноценный диван)
   - До 1 июля: 7500₽ за 2 гостей
   - После 1 июля: 7800₽ за 2 гостей
   - Свыше 2 гостей: +300₽/чел
   - Выход прямо к речке, вид на горы
   - Дети до 5 лет: бесплатно

6. A-Frame (1 шт, отдельно стоящий)
   - 2+2 места (диван) + 2 матраса на 2 этаже = до 6 человек
   - До 1 июля: 8000₽ за 2 гостей
   - После 1 июля: 8500₽ за 2 гостей
   - Свыше 2 гостей: +300₽/чел
   - Дети до 5 лет: бесплатно

В КАЖДОМ НОМЕРЕ: панорамные окна с видом на горы, туалет и душ, полотенца,
отельное постельное бельё, фен, чайник, посуда, жидкое мыло, туалетная бумага,
Wi-Fi, холодильник (кроме стандартного номера). Шампуни не предоставляются.

=== УСЛУГИ ===
- Баня: 1500₽/час, минимум 2 часа (3000₽)
- Кафе на территории: 08:00-21:00 (завтрак не включён, оплачивается отдельно)
- По запросу: национальное блюдо (оговаривается при бронировании)
- Мангальные зоны рядом с беседками
- Детская площадка
- Парковка: бесплатно
- Животные: 500₽/день + обязателен паспорт здоровья животного
- Трансфер: уточнять у менеджера, до/из Горно-Алтайска 35000₽

=== РЕСЕПШН ===
- Работает: 09:00-21:00
- Поздний заезд согласовывать заранее

=== БРОНИРОВАНИЕ ===
- Заезд: 14:00 / Выезд: 12:00
- Предоплата: 50%
- Отмена за 7+ дней: штраф 10%
- Отмена менее 7 дней: предоплата не возвращается
- Менеджер Асель: +7(913)693-68-19

=== РАСПОЛОЖЕНИЕ ===
- Адрес: Республика Алтай, Улаганский район, с. Акташ, ул. Лесная, д. 1Б
- До Горно-Алтайска: 340 км / До Бийска: 440 км
- Рейтинг: 4.3 ⭐ (133 отзыва, 2ГИС) / Работают с 2021 года

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
Аренда авто с водителем на сутки — 35000₽
Дети на ретранслятор: до 5 лет — 2000₽, 5-10 лет — 2500₽, в круг — 5500₽

=== ТВОИ ДЕЙСТВИЯ ===
1. Отвечай на вопросы об отеле
2. Помоги выбрать номер под нужды клиента
3. Когда клиент готов бронировать — собери: имя, телефон, даты заезда/выезда, количество гостей, тип номера
4. После сбора данных скажи: "Спасибо! Ваша заявка принята. Менеджер Асель свяжется с вами по номеру +7(913)693-68-19 для подтверждения и оплаты предоплаты 50%."
5. Если вопрос про трансфер или поздний заезд — говори звонить Аселе: +7(913)693-68-19"""


def get_ai_response(user_message, chat_history):
    messages = chat_history + [{"role": "user", "content": user_message}]
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
    print("Wazzup ответ:", r.status_code, r.text[:200])


def send_max_message(chat_id, text):
    url = "https://botapi.max.ru/messages"
    params = {"access_token": MAX_TOKEN}
    payload = {"recipient": {"chat_id": chat_id}, "text": text}
    requests.post(url, params=params, json=payload)


chat_histories = {}


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("ВХОДЯЩИЕ ДАННЫЕ:", data)
    if not data:
        return "OK"

    # Wazzup вебхук
    if "messages" in data:
        for msg in data.get("messages", []):
            print("СООБЩЕНИЕ:", msg)
            if msg.get("status") != "inbound":
                continue
            text = msg.get("text", "")
            chat_id = msg.get("chatId", "")
            channel_id = msg.get("channelId", "")

            if not text or not chat_id:
                continue

            if chat_id not in chat_histories:
                chat_histories[chat_id] = []

            ai_reply = get_ai_response(text, chat_histories[chat_id])
            chat_histories[chat_id].append({"role": "user", "content": text})
            chat_histories[chat_id].append({"role": "assistant", "content": ai_reply})

            send_wazzup_message(chat_id, channel_id, ai_reply)

    # MAX вебхук
    event_type = data.get("type")
    if event_type == "message_created":
        message = data.get("body", {})
        text = message.get("text", "")
        chat_id = data.get("recipient", {}).get("chat_id")

        if text and chat_id:
            if chat_id not in chat_histories:
                chat_histories[chat_id] = []

            ai_reply = get_ai_response(text, chat_histories[chat_id])
            chat_histories[chat_id].append({"role": "user", "content": text})
            chat_histories[chat_id].append({"role": "assistant", "content": ai_reply})

            send_max_message(chat_id, ai_reply)

    return "OK"


@app.route("/", methods=["GET"])
def index():
    return "Акташ Вилладж Бот работает!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
