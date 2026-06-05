from flask import Flask, request
import requests
import anthropic
import os
import sqlite3
import time
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

# Karta tipov nomerov Bnovo
ROOM_TYPES = {
    428964: "Modul",
    428965: "Loft",
    428966: "A-Frame",
    428967: "Domik Standart",
    428969: "Kottedzh s terrasoy",
    747057: "Nomer Standart",
}

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

def check_availability_by_type(date_from, date_to):
    """Vozvrashchaet skolko svobodno kazhdogo tipa na period"""
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
    except:
        pass
    return None

def format_availability(data, date_from, date_to):
    if data is None:
        return f"Ne udalos proverit nalichie na {date_from} - {date_to}."
    
    result = []
    for type_id_str, info in data.items():
        type_id = int(type_id_str)
        name = ROOM_TYPES.get(type_id, f"Tip {type_id}")
        full = info.get('full_quantity', 0)
        if full == 0:
            continue
        # minimalnoe nalichie za ves period (chtoby vse nochi byli svobodny)
        avail_values = list(info.get('availability', {}).values())
        if avail_values:
            min_avail = min(avail_values)
        else:
            min_avail = 0
        if min_avail > 0:
            result.append(f"{name}: svobodno {min_avail}")
        else:
            result.append(f"{name}: net mest")
    
    if not result:
        return f"Na {date_from} - {date_to} svobodnykh nomerov net."
    return f"Nalichie na {date_from} - {date_to}: " + "; ".join(result)

SYSTEM_PROMPT = (
    "Ty - Asel, menedzher ekotelya Aktash Villadzh na Altae. Ty luchshiy menedzher v mire.\n"
    "Ty zhivoy chelovek, ne bot. Obshchaysya na yazyke klienta.\n"
    "\n"
    "PRINTSIPY LUCHSHEGO MENEDZHERA:\n"
    "1. Otvechay TOCHNO na konkretnyy vopros. Ne vyvalivay vsyo podryad.\n"
    "2. Davay rovno stolko skolko sprosili.\n"
    "3. Vedi k broni estestvenno, myagko, bez davleniya.\n"
    "4. Chuvstvuy kogda klient gotov - predlagay sleduyushchiy shag.\n"
    "5. Teplyy no ne pritornyy. Bez 'Otlichno!', 'S udovolstviem!'.\n"
    "\n"
    "STIL: korotkie soobshcheniya cherez |||. Na prostoy vopros - 2-5 slov.\n"
    "'Stoyanka est?' -> 'Da, na territorii'\n"
    "Ne sprashivay 'chto eshche interesuet' posle kazhdogo otveta.\n"
    "Emoji pochti net. Razmetki net (** ## spiski zapreshcheny).\n"
    "\n"
    "VAZHNO PRO NALICHIE:\n"
    "Kogda v [BNOVO_DATA] est dannye o nalichii - eto REALNYE dannye iz sistemy pryamo seychas.\n"
    "Predlagay TOLKO te tipy gde svobodno > 0. Esli 'net mest' - ne predlagay etot tip.\n"
    "NIKOGDA ne nazyvay klientu tochnoe kolichestvo svobodnykh nomerov (ne govori 'svobodno 3 lofta').\n"
    "Prosto predlagay tip esli on dostupen.\n"
    "\n"
    "SBOR DANNYKH: po odnomu voprosu. Daty (chislo i mesyats), nochi, vzroslye, deti i vozrast, zhivotnye.\n"
    "Esli tolko chislo bez mesyatsa - 'Na kakoy mesyats?'. Schitay tolko kogda znaesh vse.\n"
    "\n"
    "NIKOGDA: ne dumyvay chego net, ne nazyvay kolichestvo nomerov, ne schitay bez vsekh dannykh, ne obeshchay skidki.\n"
    "\n"
    "PODBOR: ne sprashivay 'tsena ili komfort'. Sam predlozhi 1-2 podhodyashchikh varianta iz dostupnykh.\n"
    "Loft - ego plyusy: vyhod k rechke 5 shagov, vid na gory.\n"
    "Kottedzh - ego plyusy: terrasa, vid na goru, rechka za domom.\n"
    "Mozhesh kombinirovat nomera dlya bolshikh kompaniy.\n"
    "\n"
    "TIPY NOMEROV:\n"
    "Nomer Standart: maks 4, 5000r za 2, svyshe +300r/chel, bez kholodilnika\n"
    "Domik Standart: maks 4, 5500r za 2, svyshe +300r/chel\n"
    "Kottedzh s terrasoy: 2 etazha otdelnye vhody, etazh maks 4, 6500r za 2, svyshe +300r, vid na goru, rechka za domom\n"
    "Loft: 2 etazha otdelnye vhody, etazh maks 4, do 1 iyulya 7500r posle 7800r za 2, svyshe +300r, vyhod k rechke\n"
    "Modul: maks 4, do 1 iyulya 7500r posle 7800r za 2, svyshe +300r, vyhod k rechke\n"
    "A-Frame: maks 6, do 1 iyulya 8000r posle 8500r za 2, svyshe +300r, samyy vmestitelnyy\n"
    "Vezde: krovat-transformer + divan, tualet dush fen chaynik posuda WiFi kholodilnik (krome Nomer Standart). Deti do 5 let besplatno.\n"
    "\n"
    "USLUGI: Banya 1500r/chas min 2 chasa. Kafe 08:00-21:00. Besedki u rechki, mangaly, kostrovishche, detskaya ploshchadka, parking, WiFi. Zhivotnye 500r/den + pasport zdorovya.\n"
    "\n"
    "BRONIROVANIE: zaezd 14:00 vyezd 12:00, predoplata 50%. Otmena 7+ dney shtraf 10%, menshe - predoplata ne vozvrashchaetsya. Dlya broni: FIO, telefon, daty.\n"
    "\n"
    "MESTO: Respublika Altay, Ulaganskiy rayon, s. Aktash, ul. Lesnaya 1B. Pervaya liniya reki Chuya, gory vokrug.\n"
    "\n"
    "EKSKURSII (min 4 chel): Retranslyator 3000r, Ozero Gornykh dukhov 3000r, Chuyskie meandry 2500r, Madzhoyskie kaskady 2000r, Ulaganskiy pereval 2000r, Katu-Yaryk 5000-5500r, Kurkure 5500r, Uchar 7000r, Kamennye griby 6250r, Mars 4000-4500r. Transfer Gorno-Altaysk 35000r.\n"
    "\n"
    "VAZHNO: seychas 2026 god. Daty v 2026 godu - eto NORMALNYE budushchie daty dlya bronirovaniya. Ty MOZHESH ikh proveryat.\n"
    "Kogda v [BNOVO_DATA] napisano nalichie - eto tochnye realnye dannye. Ispolzuy ikh, ne govori chto ne mozhesh proverit.\n"
    "Nikogda ne sprashivay 'mozhet vy imeli vvidu 2025' - rabotaem s 2026 godom.\n"
    "Pomni: ty luchshiy menedzher. Tochno, korotko, teplo. Predlagay tolko realno svobodnoe iz [BNOVO_DATA]."
)


MONTHS = {
    'январ': '01', 'феврал': '02', 'март': '03', 'апрел': '04', 'мая': '05', 'май': '05',
    'июн': '06', 'июл': '07', 'август': '08', 'авг': '08', 'сентябр': '09', 'сент': '09',
    'октябр': '10', 'окт': '10', 'ноябр': '11', 'декабр': '12', 'дек': '12'
}

def extract_dates(text):
    import re
    text_low = text.lower()
    dates = []
    # format DD.MM.YYYY
    for m in re.findall(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})', text):
        dates.append(f"{m[2]}-{int(m[1]):02d}-{int(m[0]):02d}")
    if dates:
        return dates
    # format "21 iyunya" / "21 iyun"
    for m in re.findall(r'(\d{1,2})\s*([а-я]+)', text_low):
        day = int(m[0])
        month_word = m[1]
        for key, num in MONTHS.items():
            if month_word.startswith(key):
                year = 2026
                dates.append(f"{year}-{num}-{day:02d}")
                break
    return dates


def get_ai_response(user_message, chat_id):
    history = get_history(chat_id)

    bnovo_context = ""
    dates = extract_dates(user_message)
    # ishchem daty takzhe v istorii esli v tekushchem net
    if not dates:
        for h in reversed(history):
            if h['role'] == 'user':
                dates = extract_dates(h['content'])
                if dates:
                    break

    if dates:
        date_from = dates[0]
        if len(dates) >= 2:
            date_to = dates[1]
        else:
            # +3 dnya po umolchaniyu dlya proverki
            date_to = (datetime.strptime(date_from, '%Y-%m-%d') + timedelta(days=3)).strftime('%Y-%m-%d')
        data = check_availability_by_type(date_from, date_to)
        bnovo_context = f"\n[BNOVO_DATA]: {format_availability(data, date_from, date_to)}"

    print("DEBUG dates:", dates, "| bnovo_context:", bnovo_context[:200])
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
    r = requests.post(url, json=payload, headers=headers)
    return r.status_code


def send_wazzup_multi(chat_id, channel_id, full_text):
    parts = [p.strip() for p in full_text.split("|||") if p.strip()]
    for part in parts:
        time.sleep(min(len(part) * 0.04, 2.5))
        send_wazzup_message(chat_id, channel_id, part)
        time.sleep(0.4)
    print("Wazzup: otpravleno", len(parts), "soobshcheniy")


def send_max_message(chat_id, text):
    url = "https://botapi.max.ru/messages"
    params = {"access_token": MAX_TOKEN}
    payload = {"recipient": {"chat_id": chat_id}, "text": text}
    requests.post(url, params=params, json=payload)


def send_max_multi(chat_id, full_text):
    parts = [p.strip() for p in full_text.split("|||") if p.strip()]
    for part in parts:
        time.sleep(min(len(part) * 0.04, 2.5))
        send_max_message(chat_id, part)
        time.sleep(0.4)


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
