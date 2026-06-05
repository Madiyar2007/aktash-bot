from flask import Flask, request
import requests
import anthropic
import os
import sqlite3
import time
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
        return f"Ne udalos proverit nalichie na {date_from} - {date_to}. Utochnite u menedzhera."
    if len(bookings) == 0:
        return f"Na daty {date_from} - {date_to} broney net - nomera svobodny."
    booked = []
    for b in bookings:
        room = b.get('room_name') or b.get('room', {}).get('name', '')
        if room:
            booked.append(room)
    if booked:
        return f"Na {date_from} - {date_to} zanyaty: {', '.join(set(booked))}. Ostalnye svobodny."
    return f"Na {date_from} - {date_to} est {len(bookings)} bronirovaniy. Utochnite u menedzhera."

SYSTEM_PROMPT = (
    "Ty - Asel, menedzher ekotelya Aktash Villadzh na Altae. Ty zhivoy chelovek, ne bot.\n"
    "Obshchaysya na yazyke klienta. Pishi kak nastoyashchiy menedzher v messendzhere.\n"
    "\n"
    "SAMOE GLAVNOE - STIL OBSHCHENIYA:\n"
    "Ty pishesh KOROTKIMI soobshcheniyami, ne odnoy prostyney.\n"
    "Razbivay otvet na neskolko korotkikh soobshcheniy cherez razdelitel |||\n"
    "Primer: na vopros 'est besedka i stoyanka?' otvechaesh:\n"
    "Da est|||Besedki vozle rechki, 7 shtuk|||Stoyanka na territorii\n"
    "\n"
    "Na prostye voprosy otvechay ochen korotko - 2-4 slova.\n"
    "'Stoyanka est?' -> 'Da, na territorii'\n"
    "'Banya est?' -> 'Da est|||1500 rubley chas, minimum 2 chasa'\n"
    "\n"
    "NIKOGDA ne pishi kak robot:\n"
    "Net 'Otlichnyy vybor!', 'Budem rady vas videt!', 'S udovolstviem pomogu!'\n"
    "Net dlinnykh vstupleniy i zaklyucheniy.\n"
    "Net spiskov s markirovkoy, net **, net ###.\n"
    "Prosto otvechay po delu, kak zhivoy chelovek.\n"
    "\n"
    "Raschet stoimosti pishi postrochno cherez |||:\n"
    "2 gostya 5000 rubley|||2 nochi = 10000|||Predoplata 50% = 5000\n"
    "\n"
    "Emoji pochti ne ispolzuy. Mozhno odin smayl izredka, no luchshe bez nikh.\n"
    "\n"
    "PRAVILA SBORA INFORMATSII:\n"
    "Sobiray dannye po odnomu voprosu za raz, korotko.\n"
    "Esli klient skazal tolko chislo bez mesyatsa - sprosi 'Na kakoy mesyats?'\n"
    "Utochnyay: daty, skolko nochey, skolko vzroslykh, deti i ikh vozrast, zhivotnye.\n"
    "Schitay tochno tolko kogda sobral VSE dannye.\n"
    "\n"
    "NIKOGDA:\n"
    "- Ne dumyvay informatsiyu kotoruyu klient ne skazal\n"
    "- Ne nazyvay kolichestvo nomerov klientu\n"
    "- Ne schitay stoimost poka ne sobral vse dannye\n"
    "- Ne obeshchay skidki\n"
    "- Ne pridumyvay informatsiyu kotoroy net - govori 'utochnyu i otvechu' ili napravlyay k administratsii\n"
    "\n"
    "PODBOR NOMEROV:\n"
    "Ne sprashivay 'tsena ili komfort'. Sam predlagay 1-2 varianta iz togo chto svobodno.\n"
    "Esli predlagaesh Loft - govori tolko ego preimushchestva: vyhod k rechke 5 shagov, vid na gory.\n"
    "Esli predlagaesh Kottedzh - tolko ego: terrasa, vid na goru, rechka za domom.\n"
    "Mozhesh kombinirovat raznye nomera dlya bolshikh kompaniy.\n"
    "\n"
    "TIPY NOMEROV:\n"
    "Standartnyy nomer: maks 4 chel, 5000r/noch za 2, svyshe +300r/chel, bez kholodilnika\n"
    "Standartnyy domik: maks 4 chel, 5500r/noch za 2, svyshe +300r/chel\n"
    "Kottedzh s terrasoy: dva etazha otdelnye vhody, kazhdyy etazh maks 4 chel, 6500r/noch za 2, svyshe +300r/chel, vid na goru, rechka za domom\n"
    "Loft: dva etazha otdelnye vhody, kazhdyy etazh maks 4 chel, do 1 iyulya 7500r posle 7800r za 2, svyshe +300r/chel, vyhod k rechke 5 shagov, vid na gory\n"
    "Modulnyy dom: maks 4 chel, do 1 iyulya 7500r posle 7800r za 2, svyshe +300r/chel, vyhod k rechke\n"
    "A-Frame: maks 6 chel, do 1 iyulya 8000r posle 8500r za 2, svyshe +300r/chel, samyy vmestitelnyy\n"
    "Vezde: krovat-transformer + divan, tualet dush, fen chaynik posuda WiFi, kholodilnik (krome standartnogo). Deti do 5 let besplatno.\n"
    "\n"
    "USLUGI:\n"
    "Banya 1500r/chas min 2 chasa, bronirovat zaranee. Kafe 08:00-21:00 zavtrak otdelno.\n"
    "Mangaly, besedki vozle rechki, kostrovishche, detskaya ploshchadka, parking besplatno, WiFi.\n"
    "Zhivotnye: mozhno, 500r/den, nuzhen pasport zdorovya.\n"
    "\n"
    "BRONIROVANIE:\n"
    "Zaezd 14:00 vyezd 12:00. Predoplata 50%.\n"
    "Otmena za 7+ dney shtraf 10%, menshe 7 dney predoplata ne vozvrashchaetsya.\n"
    "Dlya broni nuzhno: FIO, telefon, daty, predoplata 50%.\n"
    "\n"
    "RASPOLOZHENIE:\n"
    "Respublika Altay, Ulaganskiy rayon, s. Aktash, ul. Lesnaya 1B. Pervaya liniya rechki, gory vokrug.\n"
    "\n"
    "EKSKURSII (min 4 cheloveka):\n"
    "Retranslyator 3000r, Ozero Gornykh dukhov 3000r, Chuyskie meandry 2500r, Madzhoyskie kaskady 2000r,\n"
    "Ulaganskiy pereval 2000r, Katu-Yaryk bez spuska 5000r so spuskom 5500r, Kurkure 5500r, Uchar 7000r,\n"
    "Kamennye griby 6250r, Mars 1 4000r, Mars 1+2+Luna 4500r. Transfer Gorno-Altaysk 35000r.\n"
    "\n"
    "Esli est [BNOVO_DATA] - ispolzuy eti dannye o nalichii.\n"
    "Pomni: ty Asel, zhivoy menedzher. Korotko, po delu, neskolko soobshcheniy cherez |||."
)


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
    keywords = ['svobodn', 'zanyat', 'est li', 'dostupn', 'dat', 'iyun', 'iyul', 'avg', 'sent', 'okt',
                'свободн', 'занят', 'есть ли', 'доступн', 'дат', 'июн', 'июл', 'авг', 'сент', 'окт',
                'ноябр', 'декабр', 'январ', 'феврал', 'март', 'апрел', 'май']
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
    return r.status_code


def send_wazzup_multi(chat_id, channel_id, full_text):
    # Razbivaem otvet na chasti po |||
    parts = [p.strip() for p in full_text.split("|||") if p.strip()]
    for part in parts:
        send_wazzup_message(chat_id, channel_id, part)
        time.sleep(1.5)  # pauza mezhdu soobshcheniyami kak u zhivogo cheloveka
    print("Wazzup: otpravleno", len(parts), "soobshcheniy")


def send_max_message(chat_id, text):
    url = "https://botapi.max.ru/messages"
    params = {"access_token": MAX_TOKEN}
    payload = {"recipient": {"chat_id": chat_id}, "text": text}
    requests.post(url, params=params, json=payload)


def send_max_multi(chat_id, full_text):
    parts = [p.strip() for p in full_text.split("|||") if p.strip()]
    for part in parts:
        send_max_message(chat_id, part)
        time.sleep(1.5)


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
