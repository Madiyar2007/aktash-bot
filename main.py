import sys
from flask import Flask, request
import requests
import anthropic
import os
import sqlite3
import time
import re
import threading
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

# Модуль бронирования (UUID объекта в reservationsteps)
BOOKING_MODULE_ID = "5c80b571-1fa1-4282-a76f-8ef53b8da612"

# Канал Wazzup (для исходящих сообщений из вебхука Bnovo, где канала в запросе нет)
WAZZUP_CHANNEL_ID = os.getenv("WAZZUP_CHANNEL_ID", "f2fb13af-f426-40ef-a3f0-f7c5f5bb3310")

# Путь к БД. На Render укажи DB_PATH=/var/data/chat_history.db (примонтированный диск),
# чтобы история и брони не терялись при деплое. Без переменной — локальный файл (эфемерный на Render).
DB_PATH = os.getenv("DB_PATH", "chat_history.db")
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)



GITHUB_PHOTOS = "https://raw.githubusercontent.com/Madiyar2007/aktash-bot/main/photos"

ROOM_PHOTOS = {
    "loft":     [f"{GITHUB_PHOTOS}/loft/{i}.jpg" for i in range(1, 5)],
    "aframe":   [f"{GITHUB_PHOTOS}/aframe/{i}.jpg" for i in range(1, 9)],
    "kottedzh": [f"{GITHUB_PHOTOS}/kottedzh/{i}.jpg" for i in range(1, 7)],
    "modul":    [f"{GITHUB_PHOTOS}/modul/{i}.jpg" for i in range(1, 5)],
    "domik":    [f"{GITHUB_PHOTOS}/domik/{i}.jpg" for i in range(1, 6)],
    "standart": [f"{GITHUB_PHOTOS}/nomer_standart/{i}.jpg" for i in range(1, 4)],
}

ROOM_KEYWORDS = {
    "loft":     ["лофт"],
    "aframe":   ["a-frame", "афрейм", "а-фрейм", "aframe", "эй-фрейм"],
    "kottedzh": ["коттедж"],
    "modul":    ["модул"],
    "domik":    ["домик стандарт", "стандартный домик", "стандарт домик"],
    "standart": ["номер стандарт", "стандартный номер", "стандарт номер"],
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
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('PRAGMA journal_mode=WAL')  # лучше переносит одновременную запись из потоков
    # FIX #4: явный автоинкрементный id — сортируем по нему, а не по timestamp (точность до секунды ломала порядок ролей)
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT, role TEXT, content TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    # FIX #5: дедуп по реальному id сообщения, а не "любое сообщение от чата за 5 сек"
    c.execute('''CREATE TABLE IF NOT EXISTS seen_messages
                 (message_id TEXT PRIMARY KEY, ts REAL)''')
    # Подтверждённые оплаты броней — чтобы не слать гостю подтверждение повторно
    c.execute('''CREATE TABLE IF NOT EXISTS confirmed_bookings
                 (booking_id TEXT PRIMARY KEY, ts REAL)''')
    # Связь messageId -> текст (для reply: понять, на какое сообщение ответил гость)
    c.execute('''CREATE TABLE IF NOT EXISTS msg_texts
                 (message_id TEXT PRIMARY KEY, text TEXT, ts REAL)''')
    conn.commit()
    conn.close()

def remember_msg_text(message_id, text):
    """Сохраняем текст сообщения по его messageId (для reply-цитат)."""
    if not message_id or not text:
        return
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM msg_texts WHERE ts < ?', (time.time() - 7 * 86400,))  # чистим старше недели
    c.execute('INSERT OR REPLACE INTO msg_texts (message_id, text, ts) VALUES (?, ?, ?)',
              (message_id, text[:500], time.time()))
    conn.commit()
    conn.close()

def get_msg_text(message_id):
    """Текст сообщения по messageId, или None."""
    if not message_id:
        return None
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('SELECT text FROM msg_texts WHERE message_id = ?', (message_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_history(chat_id):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    # FIX #4: ORDER BY id (а не timestamp) — стабильный порядок вопрос/ответ
    c.execute('SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 20', (chat_id,))
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": row[0], "content": row[1]} for row in rows]

def save_message(chat_id, role, content):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)', (chat_id, role, content))
    conn.commit()
    conn.close()

def already_processed(message_id):
    """FIX #5: True если это сообщение уже обрабатывали. Атомарно — решаем по факту INSERT, а не SELECT+INSERT."""
    now = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM seen_messages WHERE ts < ?', (now - 3600,))  # лёгкая чистка старого
    try:
        c.execute('INSERT INTO seen_messages (message_id, ts) VALUES (?, ?)', (message_id, now))
        conn.commit()
        seen = False
    except sqlite3.IntegrityError:
        seen = True  # такой message_id уже есть — это дубликат
    conn.close()
    return seen

init_db()

_bnovo_token = {"token": None, "exp": 0}

def get_bnovo_token():
    now = time.time()
    if _bnovo_token["token"] and now < _bnovo_token["exp"]:
        return _bnovo_token["token"]
    try:
        auth = requests.post(
            f'{BNOVO_BASE_URL}/api/v1/auth',
            json={'id': BNOVO_USER_ID, 'password': BNOVO_PASSWORD},
            verify=False, timeout=10
        )
        if auth.status_code == 200:
            tok = auth.json()['data']['access_token']
            _bnovo_token["token"] = tok
            _bnovo_token["exp"] = now + 300  # кэш на 5 минут
            return tok
        sys.stderr.write(f"BNOVO auth status={auth.status_code} {auth.text[:200]}\n"); sys.stderr.flush()
    except Exception as e:
        # FIX #7: не глотаем ошибку молча
        sys.stderr.write(f"BNOVO auth error: {e}\n"); sys.stderr.flush()
    return None

def check_availability_by_type(date_from, date_to, _retry=True):
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
        if r.status_code == 401 and _retry:
            _bnovo_token["token"] = None  # токен протух — сбрасываем кэш и пробуем ещё раз
            return check_availability_by_type(date_from, date_to, _retry=False)
        sys.stderr.write(f"BNOVO availability status={r.status_code} {r.text[:200]}\n"); sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"BNOVO availability error: {e}\n"); sys.stderr.flush()
    return None

# Модули у речки (room_id из Bnovo): 17, 18, 19. Модуль 20 (797501) — НЕ у речки.
RIVER_MODUL_IDS = {790546, 790547, 790548}   # 17, 18, 19 — у речки
MODUL_20_ID = 797501                          # 20 — отдельно, не у речки

def modul_river_status(date_from, date_to, _retry=True):
    """Для Модуля: есть ли свободный речной модуль (17-19) на весь период.
    Возвращает 'river' (есть у речки), 'far' (свободен только 20), None (нет данных/занято всё)."""
    token = get_bnovo_token()
    if not token:
        return None
    try:
        r = requests.get(
            f'{BNOVO_BASE_URL}/api/v1/availability/rooms',
            params={'date_from': date_from, 'date_to': date_to},
            headers={'Authorization': f'Bearer {token}'}, verify=False, timeout=10
        )
        if r.status_code == 401 and _retry:
            _bnovo_token["token"] = None
            return modul_river_status(date_from, date_to, _retry=False)
        if r.status_code != 200:
            sys.stderr.write(f"BNOVO rooms status={r.status_code} {r.text[:150]}\n"); sys.stderr.flush()
            return None
        rooms = r.json().get('data') or {}
        if isinstance(rooms, dict) and "rooms" in rooms:  # формат {"rooms":[...]}
            rooms = {str(x.get("room_id") or x.get("id")): x for x in rooms["rooms"]}
        def free_all(rid):
            info = rooms.get(str(rid)) or rooms.get(rid)
            return bool(info) and info.get("all_period") is True
        if any(free_all(rid) for rid in RIVER_MODUL_IDS):
            return "river"
        if free_all(MODUL_20_ID):
            return "far"
        return None
    except Exception as e:
        sys.stderr.write(f"modul_river_status error: {e}\n"); sys.stderr.flush()
        return None

def free_room_types(data, date_to):
    """Названия типов, свободных на ВЕСЬ запрошенный период (использует проверенную min-логику)."""
    free = []
    if not data:
        return free
    # Bnovo может вернуть data как dict {type_id: info} или как list [info, ...]
    items = data.items() if isinstance(data, dict) else enumerate(data)
    for type_id_str, info in items:
        if not isinstance(info, dict):
            continue
        try:
            type_id = int(info.get('id', type_id_str))
        except (ValueError, TypeError):
            continue
        name = ROOM_TYPES.get(type_id)
        if not name:
            continue
        if info.get('full_quantity', 0) == 0:
            continue
        avail = info.get('availability', {})
        # availability тоже бывает dict {дата: число} или list [число, ...]
        if isinstance(avail, dict):
            vals = [v for k, v in avail.items() if k != date_to]
            if not vals:
                vals = list(avail.values())
        elif isinstance(avail, list):
            vals = avail[:-1] if len(avail) > 1 else avail  # исключаем последнюю (дата выезда)
        else:
            vals = []
        # оставляем только числовые значения
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals and min(vals) > 0:
            free.append(name)
    return free

def in_season(d):
    """Сезон базы: 28 апреля — 28 сентября, ежегодно (проверка по месяцу-дню, год не важен)."""
    after_start = (d.month, d.day) >= (4, 28)
    before_end = (d.month, d.day) <= (9, 28)
    return after_start and before_end

def build_booking_link(date_from, date_to, adults=2, phone=None):
    """Ссылка на модуль бронирования с предзаполненными датами и гостями.
    date_from/date_to приходят как ГГГГ-ММ-ДД, модуль ждёт ДД-ММ-ГГГГ."""
    try:
        df = datetime.strptime(date_from, '%Y-%m-%d').strftime('%d-%m-%Y')
        dt = datetime.strptime(date_to, '%Y-%m-%d').strftime('%d-%m-%Y')
    except (ValueError, TypeError):
        return None
    adults = max(1, int(adults)) if str(adults).isdigit() else 2
    url = (f"https://reservationsteps.ru/rooms/index/{BOOKING_MODULE_ID}"
           f"?lang=ru&scroll_to_rooms=1&is_auto_search=1"
           f"&dfrom={df}&dto={dt}&adults={adults}")
    if phone:  # предзаполняем телефон гостя — так бронь сматчится с его WhatsApp
        url += f"&phone={phone}"
    return url

def bnovo_get(path, _retry=True):
    """GET к Bnovo с токеном и ретраем на 401. Возвращает поле data или None."""
    token = get_bnovo_token()
    if not token:
        return None
    try:
        r = requests.get(f'{BNOVO_BASE_URL}{path}',
                         headers={'Authorization': f'Bearer {token}'},
                         verify=False, timeout=10)
        if r.status_code == 200:
            return r.json().get('data')
        if r.status_code == 401 and _retry:
            _bnovo_token["token"] = None
            return bnovo_get(path, _retry=False)
        sys.stderr.write(f"BNOVO GET {path} status={r.status_code} {r.text[:150]}\n"); sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"BNOVO GET {path} error: {e}\n"); sys.stderr.flush()
    return None

def phone_to_chat_id(phone):
    """Телефон из брони -> WhatsApp chatId (только цифры, РФ-нормализация)."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 11 and digits[0] == '8':
        digits = '7' + digits[1:]
    elif len(digits) == 10:
        digits = '7' + digits
    return digits if len(digits) == 11 else None

def booking_already_confirmed(booking_id):
    """Атомарно: True если этой брони уже слали подтверждение оплаты."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO confirmed_bookings (booking_id, ts) VALUES (?, ?)', (str(booking_id), time.time()))
        conn.commit()
        done = False
    except sqlite3.IntegrityError:
        done = True
    conn.close()
    return done

def process_bnovo_booking(booking_id):
    """Пришёл вебхук об изменении брони — проверяем оплату и подтверждаем гостю."""
    try:
        booking = bnovo_get(f'/api/v1/bookings/{booking_id}')
        if not booking:
            return
        status_id = (booking.get('status') or {}).get('id')
        if status_id == 2:  # отменён — отмену ведёт человек, бот не вмешивается
            return
        paid = float(booking.get('payments_total') or 0)
        if paid <= 0:
            return  # оплаты ещё нет — ждём следующего вебхука
        customer = booking.get('customer') or {}
        chat_id = phone_to_chat_id(customer.get('phone'))
        if not chat_id:
            return
        # Пишем только тем, кто реально общался с ботом (не чужие брони из Booking.com и т.п.)
        if not get_history(chat_id):
            sys.stderr.write(f"BNOVO booking {booking_id}: chat {chat_id} не наш — пропуск\n"); sys.stderr.flush()
            return
        if booking_already_confirmed(booking_id):
            return  # уже подтверждали
        send_wazzup_multi(chat_id, WAZZUP_CHANNEL_ID,
                          "Оплата получена ✅ ||| Бронь подтверждена ||| Ждём вас, заезд с 14:00 🏔")
        save_message(chat_id, "assistant", "Оплата получена, бронь подтверждена")
        sys.stderr.write(f"BNOVO booking {booking_id} подтверждена гостю {chat_id}\n"); sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"process_bnovo_booking error: {e}\n"); sys.stderr.flush()

def find_alternatives(date_from, nights, today, max_days=10, max_options=2):
    """Ищет ближайшие свободные даты той же длины (и до, и после запрошенных), только в сезоне."""
    options = []
    seen = set()
    base = datetime.strptime(date_from, '%Y-%m-%d')
    for shift in range(1, max_days + 1):
        for direction in (1, -1):
            start = base + timedelta(days=shift * direction)
            if start.date() < today.date():
                continue
            if not in_season(start):  # не предлагаем даты вне сезона
                continue
            sf = start.strftime('%Y-%m-%d')
            if sf in seen:
                continue
            seen.add(sf)
            st = (start + timedelta(days=nights)).strftime('%Y-%m-%d')
            free = free_room_types(check_availability_by_type(sf, st), st)
            if free:
                options.append((sf, st, free))
                if len(options) >= max_options:
                    return options
        if options:  # нашли на ближайшем сдвиге — дальше не ищем
            return options
    return options

def build_availability_context(date_from, date_to, today):
    # Сезонный гейт — вне 28.04–28.09 в Bnovo не лезем
    if not in_season(datetime.strptime(date_from, '%Y-%m-%d')):
        return f"Даты {date_from} вне сезона. База работает с 28 апреля по 28 сентября (ежегодно). Заселение вне этого окна невозможно."
    data = check_availability_by_type(date_from, date_to)
    if data is None:
        return f"Данные о наличии недоступны на {date_from} - {date_to}."
    free = free_room_types(data, date_to)
    if free:
        ctx = f"Наличие на {date_from} - {date_to}: свободно — {', '.join(free)}."
        # Для Модуля уточняем у речки или нет (внутри типа есть и речные, и №20 в стороне)
        if "Модуль" in free:
            status = modul_river_status(date_from, date_to)
            if status == "river":
                ctx += " Модуль: есть свободный у речки."
            elif status == "far":
                ctx += " Модуль: у речки занято, свободен только модуль №20 — он в стороне от речки, не обещай гостю речку для него."
        return ctx
    # Всё занято — ищем ближайшие свободные даты
    nights = max(1, (datetime.strptime(date_to, '%Y-%m-%d') - datetime.strptime(date_from, '%Y-%m-%d')).days)
    alts = find_alternatives(date_from, nights, today)
    if not alts:
        return f"На {date_from} - {date_to} всё занято. Свободных дат в ближайшие 10 дней нет."
    parts = [f"{af} - {at} ({', '.join(fr)})" for af, at, fr in alts]
    return f"На {date_from} - {date_to} всё занято. Ближайшие свободные даты: " + "; ".join(parts) + "."

SYSTEM_PROMPT = """Ты — Асель, менеджер эко-отеля «Акташ Вилладж» на Алтае. Общаешься в WhatsApp как живой тёплый человек.

ГЛАВНЫЕ ЗАПРЕТЫ (нарушать нельзя никогда):
- НИКАКОГО MARKDOWN. Запрещены символы * и ** и _ и # вокруг слов. Пиши «Вариант 1: Лофт», а НЕ «**Вариант 1:** Лофт». Пиши «Лофт», а НЕ «**Лофт**». Сообщения идут в WhatsApp и Макс — там звёздочки показываются буквально и выглядят уродливо. Если хочешь выделить — просто текст с двоеточием.
- НЕ пиши «Что ближе?» — НИКОГДА. Это шаблонная фраза. Если надо подтолкнуть к выбору — «Какой вариант берём?» или просто закончи без вопроса.
- Только русский язык, никогда не транслит.
- Не упоминай год в ответе — пиши «21 июня», а не «21 июня 2026».
- Не говори «не могу проверить», «сбой системы».
- Не называй себя ботом или ИИ.
- Не давай реквизиты карты — оплата только через ссылку бронирования.
- Не называй гостю внутренние номера комнат (11, 17 и т.п.) — это для системы.
- НЕ упоминай соседей сам никогда. Только если гость прямо спросит «там есть соседи?».

ГЛАВНЫЙ ПРИНЦИП — ОТВЕЧАЙ ТОЛЬКО НА ВОПРОС:
Ты топовый менеджер: коротко, по делу, без лишнего. Отвечай ровно на то, что спросил гость, и не вываливай всё подряд.
- Спросил фото — покажи фото и скажи пару тёплых слов. НЕ перечисляй начинку, НЕ называй цену, НЕ говори про вместимость и доплаты, пока не спросят.
- Спросил цену — назови цену (когда знаешь даты и гостей). Не добавляй характеристики.
- Спросил про начинку — перечисли начинку. И так далее.
- Цену и расчёт давай, когда известны даты и число гостей — иначе сначала уточни их.
- Не выдавай по своей инициативе: список удобств, вместимость, доплаты, правила — только по запросу или когда это реально нужно для брони.

ОДИН ВОПРОС ЗА РАЗ — СТРОГО:
Никогда не задавай два вопроса в одном ответе. Ни через |||, ни в одном сообщении.
Если нужно уточнить несколько вещей — спроси самое важное одно, жди ответа, потом следующее.
НЕ спрашивай то, что уже знаешь из сообщения гостя:
- Гость написал «21-25 июня» → ты уже знаешь даты И ночи (4). Не спрашивай «на сколько ночей?».
- Гость написал «на двоих» → ты уже знаешь гостей. Не спрашивай «сколько человек?».
- Гость написал состав семьи → ты уже знаешь гостей. Не спрашивай снова.
Если в одном сообщении гость дал всё (даты + гостей) — сразу считай цену и давай ссылку, не уточняй.

СТИЛЬ:
- Короткие сообщения, разделяй через |||
- Тёплая, приветливая, по-человечески. Без «Отлично!», «С удовольствием!», «Замечательно!».
- На простой вопрос — 2-5 слов.
- Один вопрос за раз.
- Цены формулой: 7500+300=7800
- Эмодзи умеренно и к месту (пара за разговор, не в каждом сообщении). Природные: 🏔 🌿 ✅. Если хочешь эмодзи с лицом — используй только 🤗 (не 🙂 и не другие смайлы с лицом).
- После показа/описания номера не спрашивай «подскажу стоимость» и не сыпь вопросами. Заверши мягко и коротко, например: «На какие даты смотрите?»
- НИКОГДА не используй markdown-форматирование: никаких **жирный**, *курсив*, __подчёркивание__. Только обычный текст — сообщения читаются в WhatsApp и Макс, там markdown не работает или выглядит уродливо.
- НЕ пиши «Что ближе?» в конце — это звучит шаблонно. Если нужно подтолкнуть к выбору, спроси конкретно: «Какой вариант берём?» или просто замолчи и жди.
- Когда предлагаешь несколько вариантов — название + цена в ОДНОМ сообщении на каждый вариант, не разбивай на два отдельных пузыря.

ПЕРВОЕ СООБЩЕНИЕ:
Если история пустая — «Здравствуйте! ||| Чем могу помочь?». Если в первом сообщении уже вопрос — поздоровайся и сразу ответь. Не здоровайся повторно в идущем диалоге.

НАЛИЧИЕ:
Используй [BNOVO_DATA] — это реальные данные. Предлагай только свободные типы оттуда.
Если на даты занято, а есть «ближайшие свободные даты» — мягко предложи их. Например: «На эти даты занято ||| Но свободно с 14 по 17 июня — Лофт, A-Frame ||| Подойдёт?»
Если в [BNOVO_DATA] «вне сезона» — тепло объясни: база работает с 28 апреля по 28 сентября, предложи летние даты. Например: «Мы открыты с конца апреля по конец сентября 🏔 ||| На январь не заселяем ||| Подобрать даты на лето?»

ТИПЫ РАЗМЕЩЕНИЯ:
НОМЕРА (в общем доме, возможны соседи): Лофт, Коттедж с террасой, Номер Стандарт.
ДОМИКИ (отдельные, живёте одни): Модуль, A-Frame, Стандарт домик.

ЦЕНЫ за ночь (база — за 2 человек):
- Номер Стандарт: 5000₽. Доп. место (раскладушка) +500₽. Макс 3 человека. Без холодильника.
- Стандарт домик: 5500₽. Доп. место (раскладушка) +500₽. Макс 3 человека.
- Коттедж с террасой: 6500₽. Доп. места (диван): +300₽ за одного, +600₽ за двоих. Макс 4. У речки, 2 этажа.
- Лофт: до 1 июля 7500₽, после 7800₽. Диван: +300₽ за одного, +600₽ за двоих. Макс 4. У речки, 2 этажа.
- Модуль: до 1 июля 7500₽, после 7800₽. Диван: +300₽ за одного, +600₽ за двоих. Макс 4. У речки.
- A-Frame: до 1 июля 8000₽, после 8500₽ — за весь домик, до 6 человек, доплат за людей НЕТ. Самый вместительный.

РАСЧЁТ ЦЕНЫ:
- База — за 2 человек. Третий/четвёртый — доплата по типу (диван +300 за каждого, раскладушка +500 за одного). Доплата за человека — это ЗА НОЧЬ.
- Пример Лофт на 3: 7500+300=7800 за ночь. На 4: 7500+600=8100 за ночь.
- Пример Номер Стандарт на 3: 5000+500=5500 за ночь.
- A-Frame: всегда фикс за домик, сколько бы ни было гостей (до 6).
- Считай стоимость только когда знаешь даты и число гостей.

ПОРЯДОК ИТОГОВОГО РАСЧЁТА (всегда так):
1) Цена за ночь (с доплатами за доп. места).
2) Умножь на число ночей → стоимость проживания.
3) Прибавь собак: 500 × число собак × число ночей.
4) Назови итог. Пример: Лофт на 3, 3 ночи, 1 собака = (7800×3) + (500×1×3) = 23 400 + 1500 = 24 900₽.
Если номеров несколько (например два Лофта) — цена за ночь складывается, потом × ночи, потом собаки.

ДЕТИ:
До 5 лет — бесплатно, не занимают место и не считаются в вместимость.
От 5 лет — считается как взрослый: занимает место и оплачивается как доп. человек.

ВМЕСТИМОСТЬ И БОЛЬШИЕ ГРУППЫ (важно — не ленись с вариантами):
Вместимость: Стандарты — до 3, Лофт/Коттедж/Модуль — до 4, A-Frame — до 6.
Если гостей помещает один номер — предложи подходящие свободные типы.
Если гостей БОЛЬШЕ, чем влезает в один номер (5+ человек) — НЕ предлагай один вариант и НЕ говори «мест нет». Всегда собирай несколько решений из РЕАЛЬНО СВОБОДНЫХ типов в [BNOVO_DATA]:
- Это работает с ЛЮБЫМИ типами, не только A-Frame. Комбинируй что угодно из свободного: два Лофта, Лофт + Модуль, два Модуля, Коттедж + Стандарт, Модуль + Стандарт и т.д.
- Если A-Frame свободен — добавь его как удобный вариант «один домик на всех» (до 6). Если A-Frame занят — спокойно предлагай комбо из других типов, этого достаточно.
- Дай 2-3 варианта на выбор. Бери ТОЛЬКО свободные типы, занятые не предлагай.
- Распределяй людей логично по вместимости (6 = 3+3 или 4+2; 5 = 3+2; 7 = 4+3). Следи, чтобы в каждый номер влезало (Стандарт максимум 3, остальные 4, A-Frame 6).
- Для каждого варианта посчитай итог (цена за ночь × ночи + собаки). Спроси «Что ближе?».
Примеры:
6 чел, свободны A-Frame, Лофт, Модуль: «На шестерых есть варианты 🤗 ||| A-Frame — один домик на всех, 25 500₽ за 3 ночи ||| Или два Лофта по 3 — 48 600₽ ||| Или Лофт + Модуль ||| Что ближе?»
5 чел, A-Frame занят, свободны Лофт, Модуль, Стандарт: «На пятерых можно так 🤗 ||| Лофт (3) + Стандарт (2) ||| Или Модуль (3) + Стандарт (2) ||| Посчитать стоимость?»

СОСЕДИ:
Никогда не упоминай соседей сам — ни «будут соседи», ни «могут быть соседи», ни «живёте одни», ни «без соседей». Ни при каком выборе. Если гость прямо спросит «там есть соседи?» — тогда ответь честно. Иначе — молчи про эту тему полностью.

РАСПОЛОЖЕНИЕ:
У речки: Лофт, Коттедж. A-Frame и Стандарт домик — НЕ у речки.
Модуль — особый случай: часть модулей у речки, а модуль №20 в стороне. НЕ обещай речку для модуля сам по себе. Ориентируйся на пометку в [BNOVO_DATA]: если там «есть свободный у речки» — можешь сказать «модуль у речки ✅»; если «свободен только модуль №20 / в стороне» — честно скажи, что свободный модуль чуть в стороне от речки, речку не обещай. Номер №20 гостю не называй.
Про вид на горы и тишину НЕ пиши сам — только если гость прямо спросит (у всех вид на горы, везде тихо).

ССЫЛКА НА БРОНИРОВАНИЕ:
Когда предлагаешь ОДИН конкретный номер и он свободен — дай ссылку [BOOKING_LINK] СРАЗУ вместе с ценой, в том же ответе. Гость по ней сам введёт данные и оплатит.
Когда предлагаешь НЕСКОЛЬКО вариантов на выбор — ссылку НЕ давай. Сначала назови варианты с ценами, спроси какой берёт, и только когда гость выбрал один — дай ссылку на него. Ссылка в списке из нескольких вариантов выглядит грязно и путает.
ФИО, телефон, оплату гость вводит в форме — отдельно не спрашивай. Реквизиты карты не давай.
Ссылку вставляй ровно как в [BOOKING_LINK], ничего не меняй.
Пример (один номер): «Лофт свободен 🤗 ||| 14-17 июня, 3 ночи — 22 500₽ ||| Забронировать и оплатить: [BOOKING_LINK] ||| Там подтвердите данные и оплатите 🏔»
Если [BOOKING_LINK] нет (даты не названы) — сначала уточни даты.
После оплаты гость может прислать чек — поблагодари, скажи что бронь подтверждена.

СОБАКА / ЖИВОТНЫЕ:
Можно. 500₽ за одну собаку за СУТКИ (за каждую ночь, за каждую собаку). Оплата на месте при заезде, паспорт здоровья показать на месте.
РАСЧЁТ СОБАК: 500 × число собак × число ночей. Примеры: 1 собака 3 ночи = 500×1×3 = 1500₽. 2 собаки 3 ночи = 500×2×3 = 3000₽. НЕ считай собак разово — это плата за каждую ночь.

ФОТО:
Когда гость просит фото — фото отправит система сама, тебе ничего слать не нужно. Твой текст при этом — МИНИМАЛЬНЫЙ: одна тёплая строка про номер + короткий вопрос про даты. Без перечисления удобств, без цены, без вместимости и доплат.
Хороший пример ответа на «покажи фото модуля»: «Это наш Модуль 🤗 ||| Отдельный домик у речки ||| На какие даты смотрите?»
НИКОГДА не пиши служебных фраз вроде «фотографии автоматически отправлены», «отправляю фото», «сейчас отправлю фото». Это внутренняя механика.

ЗВОНИТЬ АСЕЛИ +7-913-693-68-19:
- Группа 10+ человек, жалоба/конфликт, договор/счёт для организации, проблема при заезде.
- Скидок нет, цены фиксированные.

УСЛУГИ:
Баня 1500₽/час, минимум 2 часа. Кафе 08:00-21:00. Беседки у речки, мангалы, костровище, детская площадка, парковка бесплатно.
Везде: кровать-трансформер, диван, туалет, душ, фен, чайник, посуда, WiFi, холодильник (кроме Номер Стандарт).

ПРАВИЛА:
Заезд 14:00, выезд 12:00. Предоплата 50%. Документы, паспорта, оплата за животных — всё на месте при заезде.
Отмена: 7+ дней — штраф 10%, менее 7 дней — предоплата не возвращается.

МЕСТО: Республика Алтай, Улаганский район, с. Акташ, ул. Лесная 1Б. Первая линия реки Чуя, горы вокруг.

ЭКСКУРСИИ (мин 4 чел): Ретранслятор 3000₽, Озеро Горных духов 3000₽, Чуйские меандры 2500₽, Мажойские каскады 2000₽, Улаганский перевал 2000₽, Кату-Ярык 5000-5500₽, Куркуре 5500₽, Учар 7000₽, Каменные грибы 6250₽, Марс 4000-4500₽. Трансфер Горно-Алтайск 35000₽.

ПРИМЕРЫ:
Клиент: Скинь фото модуля
Асель: Это наш Модуль 🤗 ||| Отдельный домик у речки ||| На какие даты смотрите?

Клиент: С нами собачка
Асель: Можно 🐕 ||| 500₽/день, оплата при заезде ||| Возьмите паспорт здоровья ||| Какие даты?

Клиент: Лофт на двоих 14-17 июня
Асель: Лофт свободен 🤗 ||| 14-17 июня, 3 ночи — 22 500₽ ||| Забронировать: [BOOKING_LINK] ||| Подтвердите данные и оплатите 🏔

Клиент: Стоянка есть?
Асель: Да, на территории, бесплатно
"""

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

def extract_nights(text):
    """FIX #8: сколько ночей просит гость. None если не указано."""
    tl = text.lower()
    m = re.search(r'(\d{1,2})\s*ноч', tl)
    if m:
        return max(1, int(m.group(1)))
    if 'недел' in tl:
        return 7
    return None

def extract_adults(text):
    """Сколько взрослых. None если не указано (по умолчанию подставим 2)."""
    tl = text.lower()
    if any(w in tl for w in ['на двоих', 'вдвоём', 'вдвоем', 'двое']):
        return 2
    if any(w in tl for w in ['одного', 'один человек', 'на одного', 'один взрослый']):
        return 1
    if any(w in tl for w in ['троих', 'трое']):
        return 3
    if any(w in tl for w in ['четверых', 'четверо']):
        return 4
    m = re.search(r'(\d{1,2})\s*(?:чел|человек|взросл|гост)', tl)
    if m:
        return max(1, int(m.group(1)))
    return None

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
            system=(
                "Ты менеджер эко-отеля Акташ Вилладж. Гость прислал фото. Определи, что на нём, и ответь коротко на русском по одному из случаев:\n"
                "1) Это карточка/скриншот номера с сайта или фото интерьера номера. Назови тип, если он виден на карточке или узнаётся: Лофт, Модуль, Коттедж с террасой, A-Frame, Номер Стандарт или Стандарт домик. Ответь строго в виде: 'НОМЕР: <тип>'. Если на карточке написано название — бери его. Если тип не определить — 'НОМЕР: неизвестно'.\n"
                "2) Чек или скриншот об оплате — ответь 'ЧЕК: <сумма если видна>'.\n"
                "3) Паспорт или документ — ответь 'ДОКУМЕНТ'.\n"
                "4) Иначе — кратко опиши, что на фото."
            ),
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": image_data}},
                {"type": "text", "text": "Что на этом фото?"}
            ]}]
        )
        return response.content[0].text
    except Exception as e:
        sys.stderr.write(f"Image error: {e}\n")
        return None

AVAIL_KW = ["свобод", "есть ли", "можно", "можете", "забронир", "брон", "заезд",
            "засел", "размест", "ноч", "чел", "человек", "мест", "приед",
            "остановит", "номер", "дата", "числ", "взросл", "детей", "ребён",
            "ребенк", "семь", "собак", "нас двое", "нас трое", "нас четыр"]

FOTO_KEYWORDS = ["фото", "покажи", "фотки", "посмотреть", "как выглядит", "покажите",
                 "фотографии", "скинь", "скиньте", "пришли", "прислать", "загляни", "посмотри"]

def get_ai_response(user_message, chat_id):
    history = get_history(chat_id)
    bnovo_context = ""
    msg_low = user_message.lower()
    dates = extract_dates(user_message)
    # Гейт: наличие подтягиваем только если сообщение про даты/номера/бронь,
    # а не на "баня есть?" / "да" — иначе цепляем стейл-даты из истории.
    intent = bool(dates) or detect_room_type(user_message) or any(kw in msg_low for kw in AVAIL_KW)
    if intent and not dates:
        for h in reversed(history):
            if h['role'] == 'user':
                found = extract_dates(h['content'])
                if found:
                    dates = found
                    break
    if intent and dates:
        date_from = dates[0]
        if len(dates) >= 2:
            date_to = dates[1]
        else:
            # FIX #8: дефолт 1 ночь (а не 3), либо число из "на N ночей" / "на неделю"
            nights = extract_nights(user_message)
            if nights is None:
                for h in reversed(history):
                    if h['role'] == 'user':
                        n = extract_nights(h['content'])
                        if n:
                            nights = n
                            break
            nights = nights or 1
            date_to = (datetime.strptime(date_from, '%Y-%m-%d') + timedelta(days=nights)).strftime('%Y-%m-%d')
        data_ctx = build_availability_context(date_from, date_to, datetime.now())
        bnovo_context = f"\n[BNOVO_DATA]: {data_ctx}"
        # Ссылка на модуль бронирования (только в сезоне) — даём её, когда гость готов бронировать
        if in_season(datetime.strptime(date_from, '%Y-%m-%d')):
            adults = extract_adults(user_message)
            if adults is None:
                for h in reversed(history):
                    if h['role'] == 'user':
                        a = extract_adults(h['content'])
                        if a:
                            adults = a
                            break
            link = build_booking_link(date_from, date_to, adults or 2, phone=chat_id)
            if link:
                bnovo_context += f"\n[BOOKING_LINK]: {link}"
    sys.stderr.write(f"DEBUG dates={dates} | bnovo={bnovo_context[:150]}\n"); sys.stderr.flush()
    messages = history + [{"role": "user", "content": user_message + bnovo_context}]
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    return response.content[0].text

ROOM_LABELS = {
    "loft": "Лофт", "aframe": "A-Frame", "kottedzh": "Коттедж с террасой",
    "modul": "Модуль", "domik": "Стандарт домик", "standart": "Номер Стандарт",
}

def _wazzup_msg_id(resp):
    """Достаём messageId из ответа Wazzup на отправку (формат может отличаться)."""
    try:
        j = resp.json()
        return j.get("messageId") or j.get("message_id") or (j.get("data") or {}).get("messageId")
    except Exception:
        return None

def send_wazzup_message(chat_id, channel_id, text, chat_type="whatsapp"):
    url = "https://api.wazzup24.com/v3/message"
    headers = {"Authorization": f"Bearer {WAZZUP_API_KEY}", "Content-Type": "application/json"}
    payload = {"channelId": channel_id, "chatId": chat_id, "chatType": chat_type, "text": text}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        mid = _wazzup_msg_id(r)
        if mid:
            remember_msg_text(mid, text)
        return r.status_code
    except Exception as e:
        sys.stderr.write(f"Wazzup send error: {e}\n"); sys.stderr.flush()
        return None

def send_wazzup_photo(chat_id, channel_id, image_url, label=None, chat_type="whatsapp"):
    url = "https://api.wazzup24.com/v3/message"
    headers = {"Authorization": f"Bearer {WAZZUP_API_KEY}", "Content-Type": "application/json"}
    payload = {"channelId": channel_id, "chatId": chat_id, "chatType": chat_type, "contentUri": image_url}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        mid = _wazzup_msg_id(r)
        if mid and label:
            remember_msg_text(mid, f"[фото: {label}]")
        sys.stderr.write(f"PHOTO: {r.status_code} {image_url[-30:]}\n"); sys.stderr.flush()
        return r.status_code
    except Exception as e:
        sys.stderr.write(f"Wazzup photo error: {e}\n"); sys.stderr.flush()
        return None

def send_room_photos(chat_id, channel_id, room_type, chat_type="whatsapp"):
    photos = ROOM_PHOTOS.get(room_type, [])
    label = ROOM_LABELS.get(room_type)
    for photo_url in photos:
        time.sleep(0.5)
        send_wazzup_photo(chat_id, channel_id, photo_url, label=label, chat_type=chat_type)

def send_wazzup_multi(chat_id, channel_id, full_text, chat_type="whatsapp"):
    parts = [p.strip() for p in full_text.split("|||") if p.strip()]
    for part in parts:
        time.sleep(min(len(part) * 0.04, 2.5))
        send_wazzup_message(chat_id, channel_id, part, chat_type)
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

DEBOUNCE_SECONDS = 6.0          # ждём, пока гость допишет серию сообщений
_pending = {}                    # chat_id -> {"msgs":[...], "channel_id":..., "timer":Timer}
_pending_lock = threading.Lock()

def enqueue_message(msg):
    """Кладём сообщение в буфер чата и (пере)заводим таймер. Ответим, когда гость замолчит."""
    chat_id = msg.get("chatId", "")
    if not chat_id:
        return
    with _pending_lock:
        entry = _pending.get(chat_id)
        if entry is None:
            entry = {"msgs": [], "channel_id": msg.get("channelId", ""),
                     "chat_type": msg.get("chatType", "whatsapp"), "timer": None}
            _pending[chat_id] = entry
        entry["msgs"].append(msg)
        if msg.get("channelId"):
            entry["channel_id"] = msg.get("channelId")
        if msg.get("chatType"):
            entry["chat_type"] = msg.get("chatType")
        if entry["timer"]:
            entry["timer"].cancel()
        t = threading.Timer(DEBOUNCE_SECONDS, flush_chat, args=(chat_id,))
        t.daemon = True
        entry["timer"] = t
        t.start()

def flush_chat(chat_id):
    """Гость замолчал — обрабатываем всю накопленную пачку как одно обращение."""
    with _pending_lock:
        entry = _pending.pop(chat_id, None)
    if not entry:
        return
    msgs = entry["msgs"]
    channel_id = entry["channel_id"]
    chat_type = entry.get("chat_type", "whatsapp")
    try:
        texts = []
        photo_descs = []
        room_from_photo = None
        quoted_text = None
        has_audio = False
        for msg in msgs:
            mt = msg.get("type", "text")
            if mt in ("audio", "voice", "ptt"):
                has_audio = True
            elif mt in ("image", "photo"):
                url = msg.get("fileUrl") or msg.get("url") or msg.get("imageUrl", "")
                if url:
                    desc = analyze_image(url)
                    if desc:
                        photo_descs.append(desc)
                        rt = detect_room_type(desc)
                        if rt:
                            room_from_photo = rt
                cap = msg.get("text") or msg.get("caption")
                if cap:
                    texts.append(cap)
            elif mt in ("document", "file", "video", "sticker", "location", "contact"):
                pass  # не пускаем в флоу брони, но и не отвечаем отдельно — обработаем вместе с текстом
            elif mt == "text":
                t = msg.get("text", "")
                if t:
                    texts.append(t)
                q = msg.get("quotedMessage") or {}
                qt = get_msg_text(q.get("messageId")) if q else None
                if qt:
                    quoted_text = qt

        combined = " ".join(texts).strip()

        # Только голос и больше ничего
        if not combined and not photo_descs:
            if has_audio:
                send_wazzup_multi(chat_id, channel_id, "Ой, голосовые пока не могу прослушать 🙏 ||| Напишите, пожалуйста, текстом — сразу помогу", chat_type)
            return

        # Тип номера: из текста, из цитаты, или с присланной карточки/фото
        room_type = detect_room_type(combined) or (detect_room_type(quoted_text) if quoted_text else None) or room_from_photo
        wants_photo = any(kw in combined.lower() for kw in FOTO_KEYWORDS)
        if room_type and wants_photo:
            send_room_photos(chat_id, channel_id, room_type, chat_type)
            time.sleep(0.5)

        # Вход для модели: фото-описания + цитата + весь текст пачки
        ai_input = combined
        if photo_descs:
            ai_input = f"[Гость прислал фото, на нём: {'; '.join(photo_descs)}] {ai_input}".strip()
        if quoted_text:
            ai_input = f"(гость отвечает на ваше сообщение: «{quoted_text}») {ai_input}".strip()

        ai_reply = get_ai_response(ai_input, chat_id)
        save_user = combined if combined else f"[фото: {'; '.join(photo_descs)[:100]}]"
        save_message(chat_id, "user", save_user)
        save_message(chat_id, "assistant", ai_reply)
        send_wazzup_multi(chat_id, channel_id, ai_reply, chat_type)
    except Exception as e:
        sys.stderr.write(f"flush_chat error: {e}\n"); sys.stderr.flush()

def process_max_message(chat_id, text):
    """FIX #6: фоновая обработка сообщения из MAX."""
    try:
        ai_reply = get_ai_response(text, chat_id)
        save_message(chat_id, "user", text)
        save_message(chat_id, "assistant", ai_reply)
        send_max_multi(chat_id, ai_reply)
    except Exception as e:
        sys.stderr.write(f"process_max_message error: {e}\n"); sys.stderr.flush()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)  # FIX #7: не падаем на кривом payload
    if not data:
        return "OK"
    sys.stderr.write(f"WEBHOOK: {str(data)[:300]}\n"); sys.stderr.flush()

    # FIX #6: дедуп делаем синхронно (быстро), тяжёлую обработку — в фон, и сразу отдаём 200 OK.
    if "messages" in data:
        for msg in data.get("messages", []):
            sys.stderr.write(f"MSG status={msg.get('status')} type={msg.get('type')} text={msg.get('text','')[:50]}\n"); sys.stderr.flush()
            # Эхо своих исходящих текстов — запоминаем messageId->текст для reply (запасной путь)
            if msg.get("isEcho") and msg.get("type") == "text" and msg.get("text") and msg.get("messageId"):
                remember_msg_text(msg.get("messageId"), msg.get("text"))
            if msg.get("status") != "inbound":
                continue
            if msg.get("chatType") == "whatsgroup":
                continue
            chat_id = msg.get("chatId", "")
            if not chat_id:
                continue

            # FIX #5: дедуп по id сообщения
            msg_id = msg.get("messageId") or f"{chat_id}:{msg.get('text','')[:30]}:{int(time.time())}"
            if already_processed(msg_id):
                sys.stderr.write(f"SKIP duplicate msg_id={msg_id}\n"); sys.stderr.flush()
                continue

            # Дебаунс: копим сообщения и ответим, когда гость допишет серию
            enqueue_message(msg)

    if data.get("type") == "message_created":
        message = data.get("body", {})
        text = message.get("text", "")
        chat_id = data.get("recipient", {}).get("chat_id")
        if text and chat_id:
            threading.Thread(target=process_max_message, args=(chat_id, text), daemon=True).start()

    return "OK"

@app.route("/bnovo-webhook", methods=["POST"])
def bnovo_webhook():
    data = request.get_json(silent=True) or {}
    sys.stderr.write(f"BNOVO_WEBHOOK: {str(data)[:300]}\n"); sys.stderr.flush()
    booking_ids = (data.get("data") or {}).get("booking_ids") or []
    for bid in booking_ids:
        threading.Thread(target=process_bnovo_booking, args=(bid,), daemon=True).start()
    return "OK"

@app.route("/", methods=["GET"])
def index():
    return "Aktash Villadzh Bot rabotaet!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
