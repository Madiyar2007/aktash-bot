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
    "loft":          [f"{GITHUB_PHOTOS}/loft/{i}.jpg" for i in range(1, 4)],
    "aframe":        [f"{GITHUB_PHOTOS}/aframe/{i}.jpg" for i in range(1, 7)],
    "kottedzh":      [f"{GITHUB_PHOTOS}/kottedzh/{i}.jpg" for i in range(1, 6)],
    "modul":         [f"{GITHUB_PHOTOS}/modul/{i}.jpg" for i in range(1, 4)],
    "domik":         [f"{GITHUB_PHOTOS}/domik/{i}.jpg" for i in range(1, 5)],
    "standart":      [f"{GITHUB_PHOTOS}/nomer_standart/{i}.jpg" for i in range(1, 3)],
}

# Klyuchevye slova dlya raspoznavaniya tipa nomera
ROOM_KEYWORDS = {
    "loft":     ["лофт"],
    "aframe":   ["a-frame", "афрейм", "а-фрейм", "aframe"],
    "kottedzh": ["коттедж", "коттедж с террасой"],
    "modul":    ["модуль", "модульный"],
    "domik":    ["домик", "стандартный домик", "домик стандарт"],
    "standart": ["стандарт", "номер стандарт", "стандартный номер"],
}

def detect_room_type(text):
    text_low = text.lower()
    for room_type, keywords in ROOM_KEYWORDS.items():
        for kw in keywords:
            if kw in text_low:
                return room_type
    return None

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
        return f"Bnovo nedostupen, predlagay po baze znany."
    result = []
    for type_id_str, info in data.items():
        type_id = int(type_id_str)
        name = ROOM_TYPES.get(type_id, f"Tip {type_id}")
        full = info.get('full_quantity', 0)
        if full == 0:
            continue
        avail_values = list(info.get('availability', {}).values())
        min_avail = min(avail_values) if avail_values else 0
        if min_avail > 0:
            result.append(f"{name}: est")
        else:
            result.append(f"{name}: zanyat")
    if not result:
        return f"Na {date_from} - {date_to} svobodnykh nomerov net."
    return "Nalichie na " + date_from + " - " + date_to + ": " + "; ".join(result)

SYSTEM_PROMPT = (
    "Ty - Asel, menedzher ekotelya Aktash Villadzh na Altae.\n"
    "Ty zhivoy chelovek, ne bot. Obshchaysya na yazyke klienta.\n"
    "\n"
    "=== TEKUSHCHIY GOD - 2026 ===\n"
    "Seychas 2026 god. Eto tekushchiy god, ne budushchee.\n"
    "Iyun 2026, iyul 2026, avgust 2026 - eto samye obychnye daty dlya bronirovaniya.\n"
    "NIKOGDA ne govori 'ne mogu proverit nalichie na 2026 god' - eto absolyutno nepravilno.\n"
    "NIKOGDA ne govori 'sboy sistemy' - prosto otvechay po [BNOVO_DATA].\n"
    "NIKOGDA ne sprashivay 'vy imeli vvidu 2025' - my rabotaem s 2026 godom.\n"
    "\n"
    "=== DANNYE IZ BNOVO ===\n"
    "Kogda v [BNOVO_DATA] napisano nalichie - eto REALNYE TOCHYNE DANNYE iz sistemy pryamo seychas.\n"
    "Esli napisano 'est' - predlagay etot tip. Esli 'zanyat' - ne predlagay.\n"
    "VSEGDA ispolzuy [BNOVO_DATA] dlya otveta o nalichii.\n"
    "\n"
    "=== STIL OBSHCHENIYA ===\n"
    "Korotkie soobshcheniya cherez |||. Bez shutok, bez igrivosti.\n"
    "Bez 'Tогда быстро!', 'Ха, ладно', 'Otlichno!', 'Zamechatelno!'.\n"
    "Teplo no delovito. Kak khoroshiy menedzher, ne kak drug.\n"
    "Na prostoy vopros - korotkiy otvet 2-5 slov.\n"
    "Emoji - maksimum odno za ves razgovor, luchshe bez nikh.\n"
    "Razmetka zapreshchena: net **, ##, spiskov.\n"
    "\n"
    "=== PRINTSIPY ===\n"
    "1. Otvechay TOCHNO na vopros - ne vyvalivay vsyo.\n"
    "2. Odin vopros za raz - ne zadavay dva srazu.\n"
    "3. Ne sprashivay 'tsena ili komfort' - sam predlozhi podkhodyashchee.\n"
    "4. Ne perechislyay tipy nomerov - predlozhi 1-2 iz dostupnykh.\n"
    "5. Vedi k broni myagko, bez davleniya.\n"
    "\n"
    "=== SBOR DANNYKH ===\n"
    "Uznavay po odnomu: daty (chislo i mesyats), nochi, vzroslye, deti i vozrast, zhivotnye.\n"
    "Esli tolko chislo bez mesyatsa - sprosi 'Na kakoy mesyats?'.\n"
    "Schitay stoimost TOLKO kogda znaesh vse.\n"
    "\n"
    "=== TIPY NOMEROV ===\n"
    "Nomer Standart: maks 4, 5000r/noch za 2, svyshe +300r/chel, bez kholodilnika\n"
    "Domik Standart: maks 4, 5500r/noch za 2, svyshe +300r/chel\n"
    "Kottedzh s terrasoy: 2 etazha otd vhody, etazh maks 4, 6500r/noch za 2, svyshe +300r, vid na goru, rechka za domom\n"
    "Loft: 2 etazha otd vhody, etazh maks 4, do 1 iyulya 7500r posle 7800r za 2, svyshe +300r, vyhod k rechke\n"
    "Modul: maks 4, do 1 iyulya 7500r posle 7800r za 2, svyshe +300r, vyhod k rechke\n"
    "A-Frame: maks 6, do 1 iyulya 8000r posle 8500r za 2, svyshe +300r, samyy vmestitelnyy\n"
    "Везде: krovat-transformer divan, tualet dush fen chaynik posuda WiFi kholodilnik (krome Nomer Standart). Deti do 5 let besplatno.\n"
    "\n"
    "NIKOGDA ne nazyvay kolichestvo svobodnykh nomerov klientu.\n"
    "Predlagay tip esli v [BNOVO_DATA] napisano 'est'.\n"
    "\n"
    "=== USLUGI ===\n"
    "Banya 1500r/chas min 2ch. Kafe 08-21. Besedki u rechki, mangaly, parking, WiFi. Zhivotnye 500r/den + pasport.\n"
    "\n"
    "=== BRONIROVANIE ===\n"
    "Zaezd 14:00 vyezd 12:00. Predoplata 50%. Otmena 7+ dney shtraf 10%, menshe - predoplata ne vozvrashchaetsya.\n"
    "Dlya broni: FIO, telefon, daty.\n"
    "\n"
    "=== MESTO ===\n"
    "Resp Altay, s Aktash, ul Lesnaya 1B. Pervaya liniya reki Chuya, gory vokrug.\n"
    "\n"

    "=== PRIMERY DIALOGOV (STILЬ ASEL) ===\n"
    "Uchis na etikh primerakh - eto realynye perepiski.\n"
    "\n"
    "PRIMER 1:\n"
    "Klient: Dobryy den! Hotela by zabronirovat nomer. S nami sobachka malenkaya, vozmozhno takoe?\n"
    "Asel: Dobryy den! Da mozhno s pasportom zdorovya. Daty napishite kakie vas interesuet?\n"
    "\n"
    "PRIMER 2:\n"
    "Klient: S 9 po 13 iyunya semya 4 cheloveka. Kakie est varianty?\n"
    "Asel: Zdravstvuyte\n"
    "Asel: K sozhaleniyu vse zanyato\n"
    "(kogda klient nastaivaet)\n"
    "Asel: Est s 12-13 svobodnye\n"
    "Asel: S 9-12 polnaya baza zanyata gruppoy\n"
    "Asel: Mogu predlozhit loft nomera\n"
    "\n"
    "PRIMER 3:\n"
    "Klient: Komnata na 6 chelovek, iyul, kakaya tsena?\n"
    "Asel: Zdravstvuyte! Vse vmeste hoteli 1 domik?\n"
    "Asel: Esli da mogu predlozhit Afreym 8500 stoit\n"
    "Asel: Libo mozhete kottedzh s terrasoy 2 nomera vzyat, stoimost nomera 6500+6500=13 000\n"
    "\n"
    "PRIMER 4 (bronirovanie):\n"
    "Asel: Dlya bronirovaniya nado oplatit 50% ot obshchey summy\n"
    "Asel: Na nomer 89833275585 Begenov Bekzhan M. Sberbank\n"
    "Asel: 7500+7500=15 000. Predoplata 7500\n"
    "Asel: Chek mne na vatsap otpravite i ya vam podtverzhdenie otpravlyu\n"
    "\n"
    "PRIMER 5 (kratkiy otvet):\n"
    "Klient: Stoyanka est?\n"
    "Asel: Da na territorii\n"
    "Klient: Banya est?\n"  
    "Asel: Da est banya i mangalnye zony ryadom s besedkami\n"
    "\n"
    "VAZHNOE IZ PRIMEROV:\n"
    "- Tseny pishy formuloy: 7500+7500=15 000\n"
    "- Korotko: 'Da est', 'Da mozhno', 'K sozhaleniyu vse zanyato'\n"
    "- Ne obyasnyay mnogo - predlagay konkretnoe\n"
    "- Na vopros pro bronirovanie srazu dayosh rekvizity i summu\n"
    "- Emoji redko: maksimum odno na soobshchenie\n"
    "=== EKSKURSII (min 4 chel) ===\n"
    "Retranslyator 3000r, Ozero Duhov 3000r, Meandry 2500r, Madzhoy 2000r, Ulaganskiy 2000r,\n"
    "Katu-Yaryk 5000-5500r, Kurkure 5500r, Uchar 7000r, Griby 6250r, Mars 4000-4500r. Transfer GA 35000r.\n"
)

MONTHS = {
    'январ': '01', 'феврал': '02', 'март': '03', 'апрел': '04', 'мая': '05', 'май': '05',
    'июн': '06', 'июл': '07', 'август': '08', 'авг': '08', 'сентябр': '09', 'сент': '09',
    'октябр': '10', 'окт': '10', 'ноябр': '11', 'декабр': '12'
}

def extract_dates(text):
    text_low = text.lower()
    dates = []
    for m in re.findall(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})', text):
        dates.append(f"{m[2]}-{int(m[1]):02d}-{int(m[0]):02d}")
    if dates:
        return dates
    for m in re.findall(r'(\d{1,2})\s*([а-я]+)', text_low):
        day = int(m[0])
        month_word = m[1]
        for key, num in MONTHS.items():
            if month_word.startswith(key):
                dates.append(f"2026-{num}-{day:02d}")
                break
    return dates

def analyze_image(image_url):
    """Skachivat i raspoznat izobrazheniye cherez Claude Vision"""
    try:
        img_response = requests.get(image_url, timeout=10, verify=False)
        if img_response.status_code != 200:
            return None
        import base64
        image_data = base64.b64encode(img_response.content).decode('utf-8')
        content_type = img_response.headers.get('content-type', 'image/jpeg').split(';')[0]
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=(
                "Ty menedzher ekotelya Aktash Villadzh. Klient prislal foto.\n"
                "Opredeli chto na foto:\n"
                "1. Esli chek ob oplate - izvleki summu i podtverzhdenie\n"
                "2. Esli pasport - izvleki FIO i nomer\n"
                "3. Esli skrinshot s voprosom - otvet na vopros\n"
                "4. Lyuboye drugoye foto - opishi kratko i otvet\n"
                "Otvet korotko, po delu, na russkom."
            ),
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": content_type,
                            "data": image_data
                        }
                    },
                    {"type": "text", "text": "Chto na etom foto? Otvet po kontekstu otelia."}
                ]
            }]
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
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    return response.content[0].text


def send_wazzup_photo(chat_id, channel_id, image_url, caption=""):
    url = "https://api.wazzup24.com/v3/message"
    headers = {"Authorization": f"Bearer {WAZZUP_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "channelId": channel_id,
        "chatId": chat_id,
        "chatType": "whatsapp",
        "contentUri": image_url,
    }
    if caption:
        payload["text"] = caption
    r = requests.post(url, json=payload, headers=headers)
    sys.stderr.write(f"PHOTO sent: {r.status_code} {image_url[-30:]}\n"); sys.stderr.flush()
    return r.status_code

def send_room_photos(chat_id, channel_id, room_type):
    photos = ROOM_PHOTOS.get(room_type, [])
    for url in photos:
        time.sleep(0.5)
        send_wazzup_photo(chat_id, channel_id, url)

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
    sys.stderr.write(f"Wazzup: otpravleno {len(parts)} soobshcheniy\n"); sys.stderr.flush()

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
    sys.stderr.write(f"WEBHOOK: {str(data)[:300]}\n"); sys.stderr.flush()
    if "messages" in data:
        for msg in data.get("messages", []):
            sys.stderr.write(f"MSG status={msg.get('status')} text={msg.get('text','')[:50]}\n"); sys.stderr.flush()
            if msg.get("status") != "inbound":
                continue
            # Otvechaem TOLKO na lichnye chaty, ne gruppy
            if msg.get("chatType") == "whatsgroup":
                continue
            chat_id = msg.get("chatId", "")
            channel_id = msg.get("channelId", "")
            if not chat_id:
                continue

            # Esli audio - prosim napisat tekstom
            msg_type = msg.get("type", "text")
            if msg_type in ("audio", "voice", "ptt"):
                send_wazzup_message(chat_id, channel_id, "Голосовые пока не принимаю — напишите текстом, отвечу быстро 🙂")
                continue

            # Esli foto - raspoznayom cherez Claude Vision
            if msg_type in ("image", "photo"):
                image_url = msg.get("fileUrl") or msg.get("url") or msg.get("imageUrl", "")
                if image_url:
                    sys.stderr.write(f"IMAGE: {image_url[:100]}\n"); sys.stderr.flush()
                    image_reply = analyze_image(image_url)
                    if image_reply:
                        caption = msg.get("text", "") or msg.get("caption", "")
                        if caption:
                            full_reply = get_ai_response(f"[Klient prislal foto. Soderzhimoe: {image_reply}. Podpis: {caption}]", chat_id)
                        else:
                            full_reply = get_ai_response(f"[Klient prislal foto. Soderzhimoe: {image_reply}]", chat_id)
                        save_message(chat_id, "user", f"[foto: {image_reply[:100]}]")
                        save_message(chat_id, "assistant", full_reply)
                        send_wazzup_multi(chat_id, channel_id, full_reply)
                    else:
                        send_wazzup_message(chat_id, channel_id, "Фото получила, но не смогла открыть. Попробуйте ещё раз")
                continue

            text = msg.get("text", "")
            if not text:
                continue

            # Esli sprosili foto nomera - otpravlyaem foto
            room_type = detect_room_type(text)
            foto_keywords = ["фото", "покажи", "фотки", "посмотреть", "как выглядит", "видео", "покажите", "фотографии"]
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
