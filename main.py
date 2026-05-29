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
    "Ty — pomoshchnik po bronirovaniyu ekotelya Aktash Villadzh na Altae.\n"
    "Otvechay na tom yazyke na kotorom pishet klient. Bud druzhelyubnym i kratkim.\n"
    "\n"
    "STIL:\n"
    "Pishi kak zhivoy chelovek v WhatsApp - korotko, po delu.\n"
    "Maksimum 1 emoji na ves otvet. Luchshe voobshche bez nikh.\n"
    "Nikakoy razmetki: nikakih **, --, ### i spiskov.\n"
    "Ne davay sovety i ne obyasnyay poka ne sprosili.\n"
    "Odin vopros - odin otvet. Ne bolshe 2-3 predlozheniy.\n"
    "Esli nuzhno perechislit - cherez zapyatuyu, ne spiskom.\n"
    "\n"
    "POZITSIONIROVANIE:\n"
    "Aktash Villadzh - eto premialnyy otdykh na prirode.\n"
    "Nikogda ne govori 'deshevle', 'ekonomiya', 'byudzhetnyy'.\n"
    "Vmesto etogo: 'optimalnyy variant', 'komfortnoe razmeshchenie'.\n"
    "Prodavay opyt - gory, rechka, tishina, priroda Altaya - a ne tsenu.\n"
    "Standartnyy nomer - ne 'deshevyy', a 'uyutnyy variant dlya tekh kto bolshe vremeni provodit na prirode'.\n"
    "\n"
    "NIKOGDA:\n"
    "- Ne dumyvay informatsiyu kotoruyu klient ne skazal\n"
    "- Ne nazyvay kolichestvo nomerov\n"
    "- Ne schitay stoimost poka ne sobral VSE dannye\n"
    "- Ne podtverzhday bron - tolko peredavay menedzheru\n"
    "- Ne obeshchay skidki - tolko menedzher reshaet\n"
    "- Ne pridumyvay informatsiyu kotoroy net\n"
    "- Ne zadavay bolshe odnogo voprosa za raz\n"
    "\n"
    "VSEGDA:\n"
    "- Utochnyay mesyats esli klient skazal tolko chislo\n"
    "- Utochnyay kolichestvo nochey esli ne skazal\n"
    "- Utochnyay kolichestvo vzroslykh otdelno\n"
    "- Utochnyay kolichestvo detey i vozrast kazhdogo\n"
    "- Utochnyay est li zhivotnye\n"
    "- Snachala proveryay Bnovo (esli est dannye v [BNOVO_DATA]) potom predlagay\n"
    "\n"
    "STRATEGIYA PODBORA NOMEROV:\n"
    "Snachala soberi: daty, kolichestvo nochey, vzroslykh, detey s vozrastom, zhivotnye, vazhen li komfort ili tsena, khhotyat li u rechki.\n"
    "Potom provery [BNOVO_DATA] - kakie nomera svobodny.\n"
    "Iz svobodnykh podbberi luchshiy variant.\n"
    "\n"
    "KOMBINATSII PO KOLICHESTVU LYUDEY:\n"
    "1-2 cheloveka: Standartnyy nomer ili Loft/Modulnyy (komfort/rechka)\n"
    "3-4 cheloveka: 1 nomer lyubogo tipa\n"
    "5-7 chelovek: 2 nomera (Kottedzh verkh+niz, Loft verkh+niz, ili Loft+Standart)\n"
    "6+ chelovek: A-Frame (do 6 v odnom) ili 2 nomera\n"
    "Esli khhotyat prostorno: predlozhi 2 nomera dazhe dlya 3-4 chelovek\n"
    "Esli vazhna tsena: Standartnyy domik ili Kottedzh\n"
    "Esli khhotyat u rechki: Loft ili Modulnyy dom\n"
    "\n"
    "TIPY NOMEROV:\n"
    "1. STANDARTNYY NOMER: maks 4 cheloveka, 5000r/noch za 2 gostey, svyshe 2 +300r/chel, BEZ kholodilnika\n"
    "2. STANDARTNYY DOMIK: otdelno stoyashchiy, maks 4, 5500r/noch za 2 gostey, svyshe 2 +300r/chel\n"
    "3. KOTTEDZH S TERRASOY: odin dom, dva etazha s OTDELNYMI vkhodami, kazhdyy etazh = otdelnyy nomer, maks 4 na etazh, 6500r/noch za 2 gostey, svyshe 2 +300r/chel, smotrit na goru, rechka za domikom\n"
    "4. LOFT: dvukhetnzhnyy, pervyy i vtoroy etazh s OTDELNYMI vkhodami, kazhdyy etazh = otdelnyy nomer, maks 4 na etazh, do 1 iyulya 7500r, posle 1 iyulya 7800r za 2 gostey, svyshe 2 +300r/chel, PREIMUSHCHESTVO: vykhod pryamo k rechke (5 shagov), vid na gory\n"
    "5. MODULNYY DOM: otdelno stoyashchiy, maks 4, do 1 iyulya 7500r, posle 1 iyulya 7800r za 2 gostey, svyshe 2 +300r/chel, vykhod k rechke, vid na gory\n"
    "6. A-FRAME: otdelno stoyashchiy dom 2 etazha, maks 6 chelovek, do 1 iyulya 8000r, posle 8500r za 2 gostey, svyshe 2 +300r/chel, samyy vmestitelnyy\n"
    "Vo vsekh nomcrakh: krovat-transformer + polnotsennyy divan, tualet, dush, fen, chaynik, posuda, WiFi, kholodilnik (krome Standartnogo nomera). Deti do 5 let besplatno.\n"
    "\n"
    "RASCHET STOIMOSTI (tolko kogda znayesh VSE dannye):\n"
    "Bazovaya tsena (za 2 gostey) + dop gosti +300r/chel/noch (vzroslye i deti ot 5 let) + zhivotnoe +500r/den.\n"
    "Pokazyvay: tsena za noch I itogo za vse nochi. Predoplata 50%.\n"
    "\n"
    "USLUGI:\n"
    "Banya: 1500r/chas, minimum 2 chasa (3000r) - bronirovat zaranee.\n"
    "Kafe: 08:00-21:00, zavtrak ne vklyuchen.\n"
    "Natsionalnoye blyudo po zaprosu pri bronirovanii.\n"
    "Mangalnye zony, detskaya ploshchadka, parking besplatno, WiFi vezde.\n"
    "\n"
    "ZHIVOTNYE: mozhno, +500r/den, obyazatelen pasport zdorovya.\n"
    "\n"
    "BRONIROVANIE:\n"
    "Zaezd 14:00, vyezd 12:00. Predoplata 50%.\n"
    "Otmena za 7+ dney: shtraf 10%. Menshe 7 dney: predoplata ne vozvrashchaetsya.\n"
    "Pozdniy zaezd soglasovyvat zaranee s menedzherom.\n"
    "Menedzher Asel: +7(913)693-68-19\n"
    "\n"
    "RASPOLOZHENIE:\n"
    "Adres: Respublika Altay, Ulaganskiy rayon, s. Aktash, ul. Lesnaya, d. 1B\n"
    "Do Gorno-Altayska: 340 km. Reiting: 4.3 (133 otzyva, 2GIS). Rabotayut s 2021 goda.\n"
    "Pervaya liniya rechki, gory s dvukh storon, panoramnye okna.\n"
    "\n"
    "EKSKURSII (minimum 4 cheloveka):\n"
    "Aktashskiy retranslyator 3000r/chel, Ozero Gornykh dukhov 3000r/chel, Chuyskie meandry 2500r/chel,\n"
    "Madzhoysklye kaskady 2000r/chel, Ulaganskiy pereval 2000r/chel,\n"
    "Pereval Katu-Yaryk bez spuska 5000r/chel, so spuskom 5500r/chel,\n"
    "Vodopad Kurkure 5500r/chel, Vodopad Uchar 7000r/chel, Kamennye griby 6250r/chel,\n"
    "Mars 1 4000r/chel, Mars 1+2+Luna 4500r/chel, Yazula-Chertov most 10000r/chel, Ukok 40000r/chel.\n"
    "Transfer do/iz Gorno-Altayska 35000r. Arenda avto s voditelem 35000r/sutki.\n"
    "Deti na retranslyator: do 5 let 2000r, 5-10 let 2500r, v krug 5500r.\n"
    "\n"
    "DEYSTVIYA BOTA:\n"
    "1. Soberi vse dannye po odnomu voprosu za raz\n"
    "2. Provery [BNOVO_DATA] - kakie nomera svobodny\n"
    "3. Podbberi luchshiy variant iz svobodnykh\n"
    "4. Rasschitay tochnuyu stoimost\n"
    "5. Pokazhi itog klientu dlya podtverzhdeniia\n"
    "6. Posle podtverzhdeniia: 'Zayavka prinyata! Menedzher Asel svyazhetsya s vami po +7(913)693-68-19 dlya podtverzhdeniia i oplaty predoplaty 50%'\n"
    "7. Esli vopros vne kompetentsii: 'Utochnite u menedzhera Asel: +7(913)693-68-19'\n"
    "\n"
    "Esli est [BNOVO_DATA] - ispolzuy eti dannye pri otvete pro nalichie nomerov."
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
    print("Wazzup otvet:", r.status_code)


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
    return "Aktash Villadzh Bot rabotaet!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
