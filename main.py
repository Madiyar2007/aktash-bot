import sys
from flask import Flask, request
import requests
import anthropic
import os
import sqlite3
import time
import re
import threading
import json
from urllib.parse import quote
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()

load_dotenv()

app = Flask(__name__)

MAX_TOKEN = os.getenv("MAX_TOKEN")
WAZZUP_API_KEY = os.getenv("WAZZUP_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # для распознавания голосовых (Whisper)
BNOVO_PASSWORD = os.getenv("BNOVO_PASSWORD")
BNOVO_USER_ID = 118966
BNOVO_BASE_URL = 'https://api.pms.bnovo.ru'

# Модуль бронирования (UUID объекта в reservationsteps)
BOOKING_MODULE_ID = "5c80b571-1fa1-4282-a76f-8ef53b8da612"

# Канал Wazzup (для исходящих сообщений из вебхука Bnovo, где канала в запросе нет)
WAZZUP_CHANNEL_ID = os.getenv("WAZZUP_CHANNEL_ID", "f2fb13af-f426-40ef-a3f0-f7c5f5bb3310")

# Ручной режим / пауза бота по чатам
HANDOVER_PAUSE_HOURS = float(os.getenv("HANDOVER_PAUSE_HOURS", "24"))  # на сколько бот замолкает после ручного сообщения оператора (скользящая пауза)
BOT_DISABLED_CHATS = {c.strip() for c in os.getenv("BOT_DISABLED_CHATS", "").split(",") if c.strip()}  # чаты, где бот не работает никогда
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")  # токен для ручки /admin/bot (без него ручка выключена)

# Уведомления Асели в Telegram: когда бот направляет гостя к ней (поздний выезд, скидка, оплата
# переводом, группа 10+, жалоба) — шлём ей пинг «зайдите в этот чат». Без этих env уведомления выключены.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ASEL_PHONE_TAIL = "9136936819"  # хвост телефона Асели (+7-913-693-68-19) — по нему ловим «направил к Асели»

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

# Ключ типа (как в ROOM_KEYWORDS) -> id типа в Bnovo. Нужно, чтобы сверять запрошенный гостем тип со свободными.
KEY_TO_TYPEID = {
    "modul": 428964, "loft": 428965, "aframe": 428966,
    "domik": 428967, "kottedzh": 428969, "standart": 747057,
}

# ---- Цены берём НАПРЯМУЮ из Bnovo (тариф «Стандартный»), чтобы они совпадали со страницей оплаты ----
# В этом отеле цена за номер фиксированная (не зависит от числа гостей) — так настроено в Bnovo.
NAME_TO_TYPEID = {v: k for k, v in ROOM_TYPES.items()}   # имя типа -> id типа
_bnovo_plan = {"id": None}

# Доплата за доп. место (за каждую ночь, за каждого гостя свыше 2). Проверено по странице оформления Bnovo:
# у «Стандарт»-номеров доплата есть, у премиум-номеров (Лофт/Модуль/A-Frame/Коттедж) цена за номер фиксированная.
EXTRA_PER_NIGHT = {
    428967: 500,   # Домик Стандарт: 2 чел база, +500/ночь за 3-го
    747057: 500,   # Номер Стандарт: 2 чел база, +500/ночь за 3-го
}
# Вместимость по типам (для показа цен по числу гостей и логики «влезут ли»)
ROOM_MAX = {428964: 4, 428965: 4, 428966: 6, 428967: 3, 428969: 4, 747057: 3}

def _rub(n):
    return f"{int(round(n)):,}".replace(",", " ") + "₽"

def get_tariff_plan_id():
    """ID тарифного плана Bnovo. Берём из env BNOVO_PLAN_ID или из /api/v1/tariffs (родительский/первый). Кэшируем."""
    if _bnovo_plan["id"] is not None:
        return _bnovo_plan["id"]
    env_id = os.getenv("BNOVO_PLAN_ID")
    if env_id:
        try:
            _bnovo_plan["id"] = int(env_id)
            return _bnovo_plan["id"]
        except ValueError:
            pass
    data = bnovo_get("/api/v1/tariffs")
    plans = (data or {}).get("plans") or []
    if plans:
        chosen = next((p for p in plans if p.get("parent_id") == 0), plans[0])
        _bnovo_plan["id"] = chosen.get("id")
    return _bnovo_plan["id"]

def fetch_room_prices(date_from, date_to):
    """Реальная цена за номер за период из Bnovo. Возвращает {type_id(int): сумма за все ночи} или {}.
    Суммируем только ночи (даты с заезда по выезд, не включая дату выезда)."""
    plan = get_tariff_plan_id()
    if not plan:
        return {}
    try:
        d0 = datetime.strptime(date_from, "%Y-%m-%d").date()
        d1 = datetime.strptime(date_to, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {}
    night_dates = set()
    cur = d0
    while cur < d1:
        night_dates.add(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    if not night_dates:
        return {}
    data = bnovo_get(f"/api/v1/tariffs/prices/{plan}?date_from={date_from}&date_to={date_to}")
    prices = (data or {}).get("prices") or {}
    totals = {}
    for tid_str, per_date in prices.items():
        try:
            tid = int(tid_str)
        except (ValueError, TypeError):
            continue
        s, ok = 0, False
        for dstr, pinfo in (per_date or {}).items():
            if dstr in night_dates and isinstance(pinfo, dict) and isinstance(pinfo.get("price"), (int, float)):
                s += pinfo["price"]
                ok = True
        if ok:
            totals[tid] = s
    return totals

def build_price_block(date_from, date_to, free_names):
    """Блок [PRICES] по свободным номерам: живая базовая цена из Bnovo (за 2 чел) + доплата за доп.
    место для Стандарт-номеров. Для номеров с доплатой показываем цену по числу гостей.
    Возвращает текст или '' (если цены недоступны — тогда бот не называет сумму, а уточняет)."""
    base = fetch_room_prices(date_from, date_to)
    if not base:
        return ""
    try:
        nights = (datetime.strptime(date_to, "%Y-%m-%d").date()
                  - datetime.strptime(date_from, "%Y-%m-%d").date()).days
    except (ValueError, TypeError):
        return ""
    seen, lines = set(), []
    for nm in free_names:
        tid = NAME_TO_TYPEID.get(nm)
        if tid is None or tid in seen or tid not in base:
            continue
        seen.add(tid)
        extra = EXTRA_PER_NIGHT.get(tid, 0)
        mx = ROOM_MAX.get(tid, 2)
        if extra and mx > 2:
            # цена зависит от числа гостей: показываем по вместимости
            parts = []
            for o in range(2, mx + 1):
                total = base[tid] + extra * nights * (o - 2)
                label = "1-2 чел" if o == 2 else f"{o} чел"
                parts.append(f"{label} {_rub(total)}")
            lines.append(f"{nm}: " + ", ".join(parts))
        else:
            lines.append(f"{nm}: {_rub(base[tid])} за номер (любая вместимость до {mx})")
    if not lines:
        return ""
    return ("[PRICES] реальные цены Bnovo за " + str(nights) + " ночей. НЕ считай сам.\n"
            "Где цена за номер — она фиксированная (число гостей не влияет). Где указано по числу гостей "
            "(1-2/3 чел) — бери строку под нужное число в этом номере.\n"
            "Один номер: назови сумму. Несколько номеров: сложи суммы выбранных номеров с учётом числа "
            "гостей в каждом:\n" + "\n".join(lines))


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
    # Пауза бота по чату (ручной режим, когда диалог ведёт Асель сама)
    c.execute('''CREATE TABLE IF NOT EXISTS chat_pause
                 (chat_id TEXT PRIMARY KEY, paused_until REAL, ts REAL)''')
    # Навсегда отключённые чаты (личные контакты/друзья) — бот туда не пишет никогда
    c.execute('''CREATE TABLE IF NOT EXISTS disabled_chats
                 (chat_id TEXT PRIMARY KEY, ts REAL)''')
    # Маршрут гостя: на каком канале и в каком мессенджере он писал + его телефон.
    # Нужно, чтобы проактивное «Оплата получена» уходило туда же, где гость общался (WhatsApp ИЛИ MAX).
    c.execute('''CREATE TABLE IF NOT EXISTS chat_routes
                 (chat_id TEXT PRIMARY KEY, channel_id TEXT, chat_type TEXT, phone TEXT, ts REAL)''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_chat_routes_phone ON chat_routes(phone)')
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

def _altai_now():
    """Текущее время по Алтаю (UTC+7) — по нему считаем 'новый день' для приветствия."""
    return datetime.utcnow() + timedelta(hours=7)

def should_greet(chat_id):
    """True, если это первое обращение гостя за сегодня (новый чат или наступил новый день). Тогда здороваемся."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute("SELECT timestamp FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return True  # новый чат — здороваемся
    try:
        last = datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=7)
    except (ValueError, TypeError):
        return False
    return last.date() != _altai_now().date()  # последнее сообщение было в другой день

def greeting_word():
    """Приветствие по времени суток (по Алтаю)."""
    h = _altai_now().hour
    if 5 <= h < 12:
        return "Доброе утро"
    if 12 <= h < 18:
        return "Добрый день"
    if 18 <= h < 23:
        return "Добрый вечер"
    return "Здравствуйте"

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

# --- Ручной режим / пауза бота по чату ---
_bot_sent_texts = {}            # нормализованный текст -> ts: что бот отправил сам (чтобы отличить от ручных сообщений Асели)
_bot_sent_lock = threading.Lock()

def _norm_text(t):
    return re.sub(r'\s+', ' ', (t or '')).strip().lower()

def remember_bot_sent(text):
    """Бот отправил этот текст сам. Помним 15 минут, чтобы потом отличить эхо бота от ручного сообщения оператора."""
    n = _norm_text(text)
    if not n:
        return
    now = time.time()
    with _bot_sent_lock:
        _bot_sent_texts[n] = now
        for k in [k for k, ts in _bot_sent_texts.items() if now - ts > 900]:
            _bot_sent_texts.pop(k, None)

def was_sent_by_bot(text):
    """True если этот текст недавно слал сам бот — значит это его эхо, а не Асель написала вручную."""
    n = _norm_text(text)
    if not n:
        return False
    with _bot_sent_lock:
        return n in _bot_sent_texts

_disabled_db = set()   # навсегда отключённые чаты из БД (кэш в памяти)

def load_disabled_chats():
    """Загружаем постоянно отключённые чаты из БД в память (при старте)."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        c = conn.cursor()
        c.execute('SELECT chat_id FROM disabled_chats')
        _disabled_db.clear()
        _disabled_db.update(r[0] for r in c.fetchall())
        conn.close()
    except Exception as e:
        sys.stderr.write(f"load_disabled_chats error: {e}\n"); sys.stderr.flush()

def is_bot_disabled(chat_id):
    """Чат, где бот не пишет НИКОГДА: либо в env-списке BOT_DISABLED_CHATS, либо отключён вручную (БД).
    Сюда вносим личные контакты и друзей, чтобы бот не лез в личную переписку."""
    return chat_id in BOT_DISABLED_CHATS or chat_id in _disabled_db

def disable_chat(chat_id):
    """Навсегда отключить бота в этом чате (личный контакт/друг)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO disabled_chats (chat_id, ts) VALUES (?, ?)', (chat_id, time.time()))
    conn.commit()
    conn.close()
    _disabled_db.add(chat_id)

def enable_chat(chat_id):
    """Вернуть бота в чат, ранее отключённый вручную."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM disabled_chats WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()
    _disabled_db.discard(chat_id)

load_disabled_chats()

def pause_chat(chat_id, hours=None):
    """Ставим чат на паузу: бот молчит, диалог ведёт человек. Пауза скользящая — каждое сообщение оператора её продлевает."""
    hours = HANDOVER_PAUSE_HOURS if hours is None else hours
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO chat_pause (chat_id, paused_until, ts) VALUES (?, ?, ?)',
              (chat_id, time.time() + hours * 3600, time.time()))
    conn.commit()
    conn.close()

def resume_chat(chat_id):
    """Снимаем паузу — бот снова отвечает в этом чате."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM chat_pause WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()

def is_bot_paused(chat_id):
    """True если бот сейчас НЕ должен писать в чат: либо он в стоп-листе, либо активна пауза после оператора."""
    if is_bot_disabled(chat_id):
        return True
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('SELECT paused_until FROM chat_pause WHERE chat_id = ?', (chat_id,))
    row = c.fetchone()
    conn.close()
    return bool(row) and time.time() < (row[0] or 0)

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

def free_room_counts(data, date_to):
    """Сколько номеров КАЖДОГО типа свободно на ВЕСЬ период = минимум свободных по ночам.
    Возвращает {название_типа: число}. Это позволяет не предлагать больше номеров, чем реально есть."""
    counts = {}
    if not data:
        return counts
    items = data.items() if isinstance(data, dict) else enumerate(data)
    for type_id_str, info in items:
        if not isinstance(info, dict):
            continue
        try:
            type_id = int(info.get('id', type_id_str))
        except (ValueError, TypeError):
            continue
        name = ROOM_TYPES.get(type_id)
        if not name or info.get('full_quantity', 0) == 0:
            continue
        avail = info.get('availability', {})
        if isinstance(avail, dict):
            vals = [v for k, v in avail.items() if k != date_to]
            if not vals:
                vals = list(avail.values())
        elif isinstance(avail, list):
            vals = avail[:-1] if len(avail) > 1 else avail  # исключаем дату выезда
        else:
            vals = []
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals and min(vals) > 0:
            counts[name] = int(min(vals))
    return counts

def free_room_types(data, date_to):
    """Названия типов, свободных на ВЕСЬ запрошенный период."""
    return list(free_room_counts(data, date_to).keys())

def in_season(d):
    """Сезон базы: 28 апреля — 28 сентября, ежегодно (проверка по месяцу-дню, год не важен)."""
    after_start = (d.month, d.day) >= (4, 28)
    before_end = (d.month, d.day) <= (9, 28)
    return after_start and before_end

# Айди типов в МОДУЛЕ бронирования (reservationsteps) — ОТЛИЧАЮТСЯ от id типов в API Bnovo!
MODULE_ROOM_IDS = {
    "modul": 345455, "loft": 345456, "aframe": 345457,
    "domik": 345458, "kottedzh": 345459, "standart": 627604,
}
MODULE_PLAN_ID = 150127   # planId модуля бронирования (из рабочих ссылок оформления)
_MODULE_EXVAL = ("dev5765_A|dev10318_B|dev13185_B|dev13555_B|getPricesBooster_A"
                 "|dev17860_A|dev19855_A|dev19856_B")

def _room_key_from_name(name):
    """Название номера (как пишет модель) -> ключ типа. None если не распознан (тогда откат на обычную ссылку)."""
    t = (name or "").lower()
    if "коттедж" in t:
        return "kottedzh"
    if "frame" in t or "фрейм" in t or "афрейм" in t:
        return "aframe"
    if "лофт" in t:
        return "loft"
    if "модул" in t:
        return "modul"
    if "номер" in t and "стандарт" in t:
        return "standart"
    if "домик" in t:
        return "domik"
    return None

def parse_room_spec(spec):
    """'Лофт:4, Номер Стандарт:2' -> [(345456,4),(627604,2)] (один пункт = один номер).
    None, если хоть один номер не распознан — тогда откатываемся на обычную ссылку."""
    rooms = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'(.+?):\s*(\d{1,2})\s*$', part)
        if not m:
            return None
        key = _room_key_from_name(m.group(1))
        mid = MODULE_ROOM_IDS.get(key) if key else None
        if not mid:
            return None
        rooms.append((mid, int(m.group(2))))
    return rooms or None

def build_prefilled_link(date_from, date_to, rooms, phone=None):
    """Ссылка на ОФОРМЛЕНИЕ модуля с уже выбранными номерами. rooms=[(module_id, гостей_в_номере), ...].
    Хвост _0.1.N при доп. местах (N = гости-2). ЭКСПЕРИМЕНТАЛЬНО: формат roomTypes недокументирован."""
    try:
        df = datetime.strptime(date_from, '%Y-%m-%d').strftime('%d-%m-%Y')
        dt = datetime.strptime(date_to, '%Y-%m-%d').strftime('%d-%m-%Y')
    except (ValueError, TypeError):
        return None
    if not rooms:
        return None
    groups, total = {}, 0
    for mid, g in rooms:
        g = max(1, int(g))
        total += g
        suffix = "" if g <= 2 else f"_0.1.{g - 2}"   # доп. места кодируются хвостом _0.1.(гости-2)
        key = f"{mid}{suffix}"
        if key in groups:
            groups[key]["c"] += 1                     # c = сколько номеров такого типа и занятости
        else:
            groups[key] = {"c": 1, "bv": 3}           # bv всегда 3 (проверено на рабочих ссылках)
    # Несколько ОДИНАКОВЫХ номеров (c>1) модуль через этот параметр не открывает (проверено: сбрасывает
    # на список). Пока не знаем формат для повторов — откатываемся на обычную ссылку (гость добавит сам).
    if any(v["c"] > 1 for v in groups.values()):
        return None
    # roomTypes — JSON. Кодируем ЦЕЛИКОМ (фигурные скобки, кавычки, двоеточия, запятые), иначе на телефоне
    # мессенджер обрезает ссылку на первом сыром символе { или | и тап не открывает страницу.
    # Модуль url-декодирует параметр (раз %22 у него работает), поэтому на десктопе ничего не меняется.
    rt_obj = {k: {"c": v["c"], "bv": v["bv"]} for k, v in groups.items()}
    room_types = quote(json.dumps(rt_obj, separators=(",", ":")), safe="")
    exval = quote(_MODULE_EXVAL, safe="")   # сырые | тоже ломают ссылку на мобильном
    phone = _valid_prefill_phone(phone) or ""   # MAX chatId — не телефон, не подставляем
    return (f"https://reservationsteps.ru/bookings/index/{BOOKING_MODULE_ID}"
            f"?&dfrom={df}&dto={dt}&lang=ru&servicemode=0&adults={total}"
            f"&colorSchemePreview=0&firstroom=0&onlyrooms=&name=&surname=&email=&phone={phone}"
            f"&orderid=&roomTypes={room_types}&planId={MODULE_PLAN_ID}"
            f"&is_auto_search=0&vkapp=0&insidePopup=0&exval={exval}&mobile_id=0")

def build_booking_link(date_from, date_to, adults=2, phone=None, children=None):
    """Ссылка на модуль бронирования с предзаполненными датами и гостями.
    date_from/date_to приходят как ГГГГ-ММ-ДД, модуль ждёт ДД-ММ-ГГГГ.
    children — список возрастов детей, например [4, 6]."""
    try:
        df = datetime.strptime(date_from, '%Y-%m-%d').strftime('%d-%m-%Y')
        dt = datetime.strptime(date_to, '%Y-%m-%d').strftime('%d-%m-%Y')
    except (ValueError, TypeError):
        return None
    adults = max(1, int(adults)) if str(adults).isdigit() else 2
    url = (f"https://reservationsteps.ru/rooms/index/{BOOKING_MODULE_ID}"
           f"?lang=ru&scroll_to_rooms=1&is_auto_search=1"
           f"&dfrom={df}&dto={dt}&adults={adults}")
    if children:  # дети: children=[4,6] в URL-кодировке -> %5B4%2C6%5D
        ages = ",".join(str(int(a)) for a in children)
        url += f"&children=%5B{ages.replace(',', '%2C')}%5D"
    phone = _valid_prefill_phone(phone)  # MAX chatId — не телефон, не подставляем
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

def _valid_prefill_phone(value):
    """Подставлять телефон в форму брони можно ТОЛЬКО если он похож на реальный номер.
    В WhatsApp chatId = телефон (11 цифр, 7…) — годится. В MAX chatId — внутренний id мессенджера
    (например 124503453), НЕ телефон: модуль примет его за номер и нарисует мусор (+1 245 034-53).
    Возвращает нормализованный телефон или None (тогда поле оставляем пустым, гость впишет сам)."""
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 11 and digits[0] == '8':
        digits = '7' + digits[1:]
    if len(digits) == 11 and digits[0] == '7':
        return digits
    return None

def _norm_phone(value):
    """Любой телефон -> 11 цифр в формате 7XXXXXXXXXX или None. Для матчинга брони с чатом гостя."""
    return _valid_prefill_phone(value)

def _guest_phone_from_msg(msg):
    """Телефон гостя из входящего сообщения: из contact.phone, либо (для WhatsApp) сам chatId = телефон."""
    c = msg.get("contact") or {}
    ph = c.get("phone") or c.get("phoneNumber") or c.get("phone_number")
    if not ph and (msg.get("chatType") or "whatsapp") == "whatsapp":
        ph = msg.get("chatId")
    return _norm_phone(ph)

def save_chat_route(chat_id, channel_id, chat_type, phone=None):
    """Запоминаем, где общался гость (канал, мессенджер) и его телефон. Телефон не затираем None-ом."""
    if not chat_id:
        return
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    try:
        row = c.execute("SELECT phone FROM chat_routes WHERE chat_id=?", (chat_id,)).fetchone()
        phone = phone or (row[0] if row else None)   # не теряем ранее сохранённый телефон
        c.execute("""INSERT INTO chat_routes (chat_id, channel_id, chat_type, phone, ts)
                     VALUES (?,?,?,?,?)
                     ON CONFLICT(chat_id) DO UPDATE SET
                       channel_id=excluded.channel_id, chat_type=excluded.chat_type,
                       phone=excluded.phone, ts=excluded.ts""",
                  (chat_id, channel_id or "", chat_type or "whatsapp", phone, time.time()))
        conn.commit()
    finally:
        conn.close()

def find_chat_by_phone(phone):
    """По телефону из брони -> (chat_id, channel_id, chat_type) самого свежего чата гостя. None если нет."""
    norm = _norm_phone(phone)
    if not norm:
        return None
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    try:
        row = c.execute("""SELECT chat_id, channel_id, chat_type FROM chat_routes
                           WHERE phone=? ORDER BY ts DESC LIMIT 1""", (norm,)).fetchone()
        return (row[0], row[1], row[2]) if row else None
    finally:
        conn.close()

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
        phone = customer.get('phone')
        confirm_text = "Оплата получена ✅ ||| Бронь подтверждена ||| Ждём вас, заезд с 14:00 🏔"
        # Куда слать: сначала по маршруту гостя (туда, где он общался — WhatsApp ИЛИ MAX),
        # иначе фоллбэк на WhatsApp по телефону = chatId. Определяем цель НЕ помечая бронь.
        route = find_chat_by_phone(phone)
        if route and get_history(route[0]):
            target = (route[0], route[1] or WAZZUP_CHANNEL_ID, route[2] or "whatsapp")
        else:
            chat_id = phone_to_chat_id(phone)
            # Пишем только тем, кто реально общался с ботом (не чужие брони из Booking.com и т.п.)
            if not chat_id or not get_history(chat_id):
                sys.stderr.write(f"BNOVO booking {booking_id}: чат гостя не найден — пропуск\n"); sys.stderr.flush()
                return
            target = (chat_id, WAZZUP_CHANNEL_ID, "whatsapp")
        # Пометку «подтверждено» ставим АТОМАРНО строго перед отправкой — иначе при отсутствии чата
        # бронь бы помечалась зря и подтверждение никогда не ушло бы при повторном вебхуке.
        if booking_already_confirmed(booking_id):
            return  # уже подтверждали
        rc_id, rc_channel, rc_type = target
        send_wazzup_multi(rc_id, rc_channel, confirm_text, rc_type)
        save_message(rc_id, "assistant", "Оплата получена, бронь подтверждена")
        sys.stderr.write(f"BNOVO booking {booking_id} подтверждена гостю {rc_id} ({rc_type})\n"); sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"process_bnovo_booking error: {e}\n"); sys.stderr.flush()

def _counts_capacity(counts):
    """Сколько всего гостей вмещают свободные номера из {название: число}."""
    cap = 0
    for name, cnt in counts.items():
        tid = NAME_TO_TYPEID.get(name)
        cap += ROOM_MAX.get(tid, 2) * cnt
    return cap

_NUM_WORDS = {'один': 1, 'одна': 1, 'одно': 1, 'два': 2, 'две': 2, 'три': 3, 'четыре': 4,
              'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9, 'десять': 10}

def requested_room_count(user_message, history):
    """Сколько НОМЕРОВ просит гость: 'три Номера Стандарт' -> 3, '2 лофта' -> 2. None если не указано.
    Число гостей ('два человека') сюда НЕ попадает — нужно слово-номер после числа."""
    num_alt = '|'.join(_NUM_WORDS)
    rx = re.compile(r'\b(\d{1,2}|' + num_alt + r')\s+(?:[а-яё]+\s+)?(номер|лофт|модул|домик|коттедж|frame|фрейм)')
    texts = [user_message] + [h['content'] for h in reversed(history) if h['role'] == 'user']
    for t in texts:
        m = rx.search((t or '').lower())
        if m:
            tok = m.group(1)
            n = int(tok) if tok.isdigit() else _NUM_WORDS.get(tok)
            if n and 1 <= n <= 15:
                return n
    return None

# Распознавание ИМЕННО запрошенного типа по стемам (ловит склонения: «Номера Стандарт», «два Лофта»).
# Двухсловные (стандарт-номер / стандарт-домик) проверяем первыми, чтобы голый «стандарт» не путал их.
_REQ_TYPE_PATTERNS = [
    ("standart", r"номер\w*\s+стандарт|стандартн\w*\s+номер"),
    ("domik",    r"домик\w*\s+стандарт|стандарт\w*\s+домик|стандартн\w*\s+домик"),
    ("loft",     r"лофт"),
    ("aframe",   r"a-?frame|афрейм|а-фрейм|эй-фрейм"),
    ("kottedzh", r"коттедж"),
    ("modul",    r"модул"),
]

def requested_room_keys(user_message, history):
    """Какие КОНКРЕТНЫЕ типы номеров гость назвал (в текущем сообщении и своих прошлых). Множество ключей.
    Нужно, чтобы поймать случай «гость просит тип, которого нет в наличии» и явно это подсветить модели."""
    texts = [user_message] + [h['content'] for h in history if h['role'] == 'user']
    found = set()
    for t in texts:
        low = (t or '').lower()
        for key, pat in _REQ_TYPE_PATTERNS:
            if re.search(pat, low):
                found.add(key)
    return found

def find_alternatives(date_from, nights, today, need_guests=None, need_rooms=None, max_days=10, max_options=2):
    """Ищет ближайшие свободные даты той же длины (и до, и после), только в сезоне.
    Отбирает только даты, куда реально помещается запрос: хватает номеров (need_rooms) и
    мест по вместимости (need_guests). Возвращает [(date_from, date_to, {название: число}), ...]."""
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
            counts = free_room_counts(check_availability_by_type(sf, st), st)
            if not counts:
                continue
            if need_rooms and sum(counts.values()) < need_rooms:
                continue  # номеров не хватает на запрошенное количество
            if need_guests and _counts_capacity(counts) < need_guests:
                continue  # по вместимости компания не помещается
            options.append((sf, st, counts))
            if len(options) >= max_options:
                return options
        if options:  # нашли на ближайшем сдвиге — дальше не ищем
            return options
    return options

def build_availability_context(date_from, date_to, today, need_guests=None, need_rooms=None):
    # Сезонный гейт — вне 28.04–28.09 в Bnovo не лезем. Возвращаем (текст, список свободных типов).
    if not in_season(datetime.strptime(date_from, '%Y-%m-%d')):
        return f"Даты {date_from} вне сезона. База работает с 28 апреля по 28 сентября (ежегодно). Заселение вне этого окна невозможно.", []
    data = check_availability_by_type(date_from, date_to)
    if data is None:
        return f"Данные о наличии недоступны на {date_from} - {date_to}.", []
    counts = free_room_counts(data, date_to)
    free = list(counts.keys())
    total_rooms = sum(counts.values())
    cap = _counts_capacity(counts)
    # Хватает ли свободного на запрос на ЭТИ даты (по числу номеров и по вместимости)
    enough = bool(free) and (not need_rooms or total_rooms >= need_rooms) \
             and (not need_guests or cap >= need_guests)
    if enough:
        parts = [f"{nm} ({cnt} шт)" for nm, cnt in counts.items()]
        ctx = (f"Наличие на {date_from} - {date_to}: свободно — {', '.join(parts)}. "
               f"Число в скобках — сколько таких номеров свободно, больше этого не предлагай.")
        if "Модуль" in free:  # внутри типа есть и речные, и №20 в стороне
            status = modul_river_status(date_from, date_to)
            if status == "river":
                ctx += " Модуль: есть свободный у речки."
            elif status == "far":
                ctx += " Модуль: у речки занято, свободен только модуль №20 — он в стороне от речки, не обещай гостю речку для него."
        return ctx, free
    # Свободного на запрос не хватает (всё занято ИЛИ номеров/мест мало) — САМИ ищем подходящие даты,
    # чтобы модель не выдумывала. Альтернативы отбираются под нужное число номеров и гостей.
    nights = max(1, (datetime.strptime(date_to, '%Y-%m-%d') - datetime.strptime(date_from, '%Y-%m-%d')).days)
    alts = find_alternatives(date_from, nights, today, need_guests=need_guests, need_rooms=need_rooms)
    if free:
        fp = ", ".join(f"{nm} ({cnt} шт)" for nm, cnt in counts.items())
        head = f"На {date_from} - {date_to} свободно только: {fp}. На запрос не хватает. "
    else:
        head = f"На {date_from} - {date_to} всё занято. "
    if not alts:
        return head + ("Подходящих дат в ближайшие 10 дней тоже нет. Предложи гостю назвать другой "
                       "период или изменить состав. НЕ придумывай даты сам."), []
    parts = []
    for af, at, cnts in alts:
        ct = ", ".join(f"{nm} {c} шт" for nm, c in cnts.items())
        parts.append(f"{af} - {at} (свободно: {ct})")
    return (head + "Ближайшие ПОДХОДЯЩИЕ даты (проверены, мест хватает): " + "; ".join(parts)
            + ". Предлагай ТОЛЬКО эти даты и ТОЛЬКО из этих количеств. НЕ придумывай других дат и не обещай больше номеров, чем указано."), []

SYSTEM_PROMPT = """Ты — Асель, менеджер эко-отеля «Акташ Вилладж» на Алтае. Общаешься в WhatsApp как живой тёплый человек.

ГЛАВНЫЕ ЗАПРЕТЫ (нарушать нельзя никогда):
- НИКАКОГО MARKDOWN. Запрещены символы * и ** и _ и # вокруг слов. Пиши «Вариант 1: Лофт», а НЕ «**Вариант 1:** Лофт». Пиши «Лофт», а НЕ «**Лофт**». Сообщения идут в WhatsApp и Макс — там звёздочки показываются буквально и выглядят уродливо. Если хочешь выделить — просто текст с двоеточием.
- НИКАКИХ ТИРЕ (знаки — и –). Живой человек с телефона их не печатает, тире сразу выдаёт робота. Пиши короткими предложениями через точку или запятую, длинную мысль разбивай на отдельные сообщения через |||. Дефис в датах (14-17 июня) — это нормально, его оставляй.
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
- Цены называй готовой суммой из блока [PRICES]. Не показывай формул и расчётов.
- Эмодзи РЕДКО. Не ставь эмодзи в каждое сообщение и не повторяй один и тот же — одинаковый смайлик в каждом ответе сразу выдаёт бота. На весь диалог одно-два эмодзи максимум, чаще вообще без них. НИКОГДА не ставь эмодзи в сообщениях про занятость, отказ, отмену, проблему или плохие новости — там он неуместен. В позитивном контексте можно изредка: 🤗 🙌 🌞 🌿, без фанатизма и каждый раз разные.
- После показа/описания номера не спрашивай «подскажу стоимость» и не сыпь вопросами. Заверши мягко и коротко, например: «На какие даты смотрите?»
- НИКОГДА не используй markdown-форматирование: никаких **жирный**, *курсив*, __подчёркивание__. Только обычный текст — сообщения читаются в WhatsApp и Макс, там markdown не работает или выглядит уродливо.
- НЕ пиши «Что ближе?» в конце — это звучит шаблонно. Если нужно подтолкнуть к выбору, спроси конкретно: «Какой вариант берём?» или просто замолчи и жди.
- Когда предлагаешь несколько вариантов — название + цена в ОДНОМ сообщении на каждый вариант, не разбивай на два отдельных пузыря.

ПЕРВОЕ СООБЩЕНИЕ И ПРИВЕТСТВИЕ:
Здоровайся, если в данных есть пометка [GREET] (это первое обращение гостя за сегодня) ИЛИ если история пустая. Приветствие бери из [GREET]: Доброе утро / Добрый день / Добрый вечер по времени суток. Если истории нет и нет вопроса — «Здравствуйте! ||| Чем могу помочь?». Пиши именно «Чем могу помочь?», а НЕ «Чем я вам помочь?» (это неграмотно).
Если [GREET] НЕТ и идёт диалог — НЕ здоровайся повторно, просто отвечай.
Если в сообщении гостя уже есть вопрос — поздоровайся (только если положено по правилу выше) и сразу ответь.

НАЛИЧИЕ:
Используй [BNOVO_DATA] — это реальные данные. Предлагай ТОЛЬКО типы из списка «свободно». Если типа нет в этом списке — его НЕТ, даже если гость прямо называет его по имени. Не повторяй название несвободного типа как доступное, не называй по нему цену, не давай на него ссылку.
Если гость просит конкретный тип, которого нет в «свободно» (или придёт пометка [НЕТ В НАЛИЧИИ]) — честно скажи, что именно его на эти даты нет, и предложи то, что реально свободно. Например: «Номер Стандарт на эти даты занят ||| Свободно Лофт и Коттедж ||| Какой посмотрим?». Назвать или посчитать тип, которого нет в наличии — грубая ошибка, как и выдуманная цена.
КРИТИЧНО: любые ДАТЫ и КОЛИЧЕСТВА номеров бери ТОЛЬКО из [BNOVO_DATA]. НИКОГДА не придумывай свободные даты и не обещай число номеров, которого там нет. Если гость просит «другие даты», а в [BNOVO_DATA] предложены конкретные подходящие даты — назови ИХ. Если подходящих дат там нет — честно скажи, что на ближайшее время не нашлось, и попроси гостя назвать период или изменить состав. Выдумать дату и потом отказать — грубая ошибка.
Если на даты занято, а есть «ближайшие свободные даты» — мягко предложи их. Например: «На эти даты занято ||| Но свободно с 14 по 17 июня: Лофт, A-Frame ||| Подойдёт?»
Если в [BNOVO_DATA] сказано «свободно только: … на запрос не хватает» — значит на эти даты компанию/нужное число номеров не разместить. Не предлагай неполный набор как решение, сразу переходи к подходящим датам из [BNOVO_DATA].
Если исходные даты заняты И гость с компанией 5+ человек — НЕ вываливай просто список свободных типов. Скажи, что на альтернативные даты свободно, и спроси, посчитать ли вариант под их компанию, потом жди ответа. Например: «На 17-26 занято ||| На 12-21 июля свободно Лофт и Стандарт домик ||| Посчитать под вашу компанию?»
Если в [BNOVO_DATA] «вне сезона» — тепло объясни: база работает с 28 апреля по 28 сентября, предложи летние даты. Например: «Мы открыты с конца апреля по конец сентября ||| На январь не заселяем ||| Подобрать даты на лето?»

ТИПЫ РАЗМЕЩЕНИЯ:
НОМЕРА (в общем доме): Лофт, Коттедж с террасой, Номер Стандарт.
ДОМИКИ (отдельно стоящие): Модуль, A-Frame, Стандарт домик.
Про соседей и про «живёте одни» НЕ пиши сам — см. раздел СОСЕДИ. Это просто деление на типы, не пересказывай его гостю.

ЦЕНЫ — БЕРИ ТОЛЬКО ИЗ БЛОКА [PRICES]:
КРИТИЧНО: НИКОГДА не придумывай, не прикидывай и не вспоминай цену из головы. ЛЮБАЯ названная сумма обязана быть из блока [PRICES] в данных. Если блока [PRICES] нет — ты НЕ ЗНАЕШЬ цену: ответь «Сейчас уточню стоимость» и НЕ называй никаких чисел. Назвать выдуманную сумму — грубая ошибка.
В данных приходит блок [PRICES] с реальными ценами Bnovo по свободным номерам на нужные даты.
- У части номеров цена за НОМЕР фиксированная (число гостей не влияет) — там так и написано.
- У Стандарт-номеров (Домик Стандарт, Номер Стандарт) цена зависит от числа гостей: в [PRICES] для них даны строки «1-2 чел» и «3 чел». Бери строку под число гостей, которое селишь в ЭТОТ номер.
- НИКОГДА не считай базу/ночи/доплаты сам. Все суммы уже готовы в [PRICES].
- Один номер: назови его сумму из [PRICES].
- Несколько номеров (группа): для каждого номера возьми сумму под число гостей в нём и сложи. Пример: три Номера Стандарт на 3+3+2 — это «3 чел» + «3 чел» + «1-2 чел» из строки Номера Стандарт, сложи три суммы.
- Если блока [PRICES] нет — скажи «сейчас уточню стоимость», чисел не называй.
- Никаких формул гостю. Только итоговая сумма.

ОСОБЕННОСТИ НОМЕРОВ (не про цену): Номер Стандарт без холодильника. Коттедж и Лофт двухэтажные. A-Frame самый вместительный, до 6 человек.

ДЕТИ:
Ребёнок до 5 лет — бесплатно, не занимает место. НЕ считай его в число гостей номера и в цене.
Ребёнок от 5 до 17 лет — считается КАК доп. взрослый: занимает место И идёт в цену как доп. место (для Стандарт-номеров та же доплата, для остальных цена за номер фиксированная). В числе гостей номера считай его наравне со взрослым.
Сколько всего детей влезает в номер (всего людей в номере, считая взрослых и ВСЕХ детей, максимум 5):
- 1 взрослый — до 4 детей
- 2 взрослых — до 3 детей
- 3 взрослых — до 2 детей
При этом детей ОТ 5 не больше, чем позволяет вместимость по местам: взрослые плюс дети от 5 должны влезть в вместимость номера (Стандарт до 3, Лофт/Модуль/Коттедж до 4, A-Frame до 6). Малыши до 5 бесплатно добивают номер до пятёрки сверх мест.
Пример: 2 взрослых и ребёнок 10 лет в Номер Стандарт — 3 гостя в номере, цена по строке «3 чел» из [PRICES]. 2 взрослых и малыш 3 лет — 2 гостя, малыш бесплатно, цена как на двоих.
В токене ссылки число гостей в номере = взрослые плюс дети от 5. Малышей до 5 в токен НЕ добавляй.

ВМЕСТИМОСТЬ И БОЛЬШИЕ ГРУППЫ (важно — не ленись с вариантами):
Вместимость: Стандарты — до 3, Лофт/Коттедж/Модуль — до 4, A-Frame — до 6.
Если гостей помещает один номер — предложи подходящие свободные типы.
Если гостей БОЛЬШЕ, чем влезает в один номер (5+ человек) — НЕ предлагай один вариант и НЕ говори «мест нет». Всегда собирай несколько решений из РЕАЛЬНО СВОБОДНЫХ типов в [BNOVO_DATA]:
- Это работает с ЛЮБЫМИ типами, не только A-Frame. Комбинируй что угодно из свободного: два Лофта, Лофт + Модуль, два Модуля, Коттедж + Стандарт, Модуль + Стандарт и т.д.
- ВНИМАНИЕ к количеству: в [BNOVO_DATA] после типа в скобках указано, сколько таких номеров свободно, например «Лофт (1 шт), Номер Стандарт (3 шт)». НИКОГДА не предлагай больше номеров одного типа, чем это число. Если Лофт (1 шт) — максимум один Лофт, «два Лофта» предлагать НЕЛЬЗЯ. Собирай комбинацию только из реально доступных количеств.
- Если из свободных количеств компанию никак не собрать (не хватает номеров) — честно скажи, что на эти даты на всю компанию мест не хватает, и предложи другие даты. Не выдумывай номера сверх свободных.
- Если A-Frame свободен — добавь его как удобный вариант «один домик на всех» (до 6). Если A-Frame занят — спокойно предлагай комбо из других типов, этого достаточно.
- Дай 2-3 варианта на выбор. Бери ТОЛЬКО свободные типы, занятые не предлагай.
- Распределяй людей логично по вместимости (6 = 3+3 или 4+2; 5 = 3+2; 7 = 4+3). Следи, чтобы в каждый номер влезало (Стандарт максимум 3, остальные 4, A-Frame 6).
- Для каждого варианта возьми готовые суммы номеров из [PRICES] и сложи. Не считай сам. В конце можно мягко спросить «Какой вариант берём?» или просто перечислить варианты и замолчать.
Примеры (суммы в ответе бери из [PRICES], здесь они опущены; не превышай количество из [BNOVO_DATA]):
6 чел, свободно A-Frame (1 шт), Лофт (2 шт), Модуль (1 шт): «На шестерых есть варианты 🤗 ||| A-Frame, один домик на всех, <сумма> ||| Или два Лофта по 3, <сумма> ||| Или Лофт + Модуль ||| Какой вариант берём?»
5 чел, A-Frame занят, свободно Лофт (1 шт), Модуль (1 шт), Стандарт (2 шт): «На пятерых можно так 🤗 ||| Лофт (3) + Стандарт (2), <сумма> ||| Или Модуль (3) + Стандарт (2), <сумма> ||| Какой вариант берём?»

СОСЕДИ:
Никогда не упоминай соседей сам — ни «будут соседи», ни «могут быть соседи», ни «живёте одни», ни «без соседей». Ни при каком выборе. Если гость прямо спросит «там есть соседи?» — тогда ответь честно. Иначе — молчи про эту тему полностью.

РАСПОЛОЖЕНИЕ:
У речки: Лофт, Коттедж. A-Frame и Стандарт домик — НЕ у речки.
Модуль — особый случай: часть модулей у речки, а модуль №20 в стороне. НЕ обещай речку для модуля сам по себе. Ориентируйся на пометку в [BNOVO_DATA]: если там «есть свободный у речки» — можешь сказать «модуль у речки ✅»; если «свободен только модуль №20 / в стороне» — честно скажи, что свободный модуль чуть в стороне от речки, речку не обещай. Номер №20 гостю не называй.
Про вид на горы и тишину НЕ пиши сам — только если гость прямо спросит (у всех вид на горы, везде тихо).

ССЫЛКА НА БРОНИРОВАНИЕ:
Когда предлагаешь ОДИН конкретный номер и он свободен — дай ссылку [BOOKING_LINK rooms=ТИП:гостей] СРАЗУ вместе с ценой, в том же ответе. Гость по ней сам введёт данные и оплатит.
Когда предлагаешь НЕСКОЛЬКО вариантов на выбор — ссылку НЕ давай. Сначала назови варианты с ценами, спроси какой берёт, и только когда гость выбрал один — дай ссылку на него. Ссылка в списке из нескольких вариантов выглядит грязно и путает.
ФИО, телефон, оплату гость вводит в форме — отдельно не спрашивай. Реквизиты карты не давай.
Когда нужна ссылка на бронь, ставь токен с ВЫБРАННЫМИ номерами: [BOOKING_LINK rooms=ТИП:гостей, ТИП:гостей] — по одному пункту на каждый физический номер, через запятую, латиницей слово rooms. Настоящую ссылку подставит система, она откроет оформление с уже выбранными номерами. Сам URL не пиши и не выдумывай.
Названия типов пиши точно: Модуль, Лофт, A-Frame, Домик Стандарт, Коттедж с террасой, Номер Стандарт. После двоеточия — сколько гостей в ЭТОМ номере.
- Один номер: [BOOKING_LINK rooms=Лофт:3]
- Комбинация: каждый номер отдельным пунктом. Три Номера Стандарт на 3+3+2 -> [BOOKING_LINK rooms=Номер Стандарт:3, Номер Стандарт:3, Номер Стандарт:2]. Лофт на 4 плюс Номер Стандарт на 2 -> [BOOKING_LINK rooms=Лофт:4, Номер Стандарт:2]
- Если конкретные номера ещё не выбраны (гость не определился) — ссылку не давай.
Назвав цену по одному номеру и дав ссылку, остановись. Не добавляй вопрос-дожим вроде «оформляем?», «готовы забронировать?», «будете брать?». Гость сам решит и оплатит по ссылке.
Пример (один номер): «Лофт свободен 🤗 ||| 14-17 июня, 3 ночи, 22 500₽ ||| Можете оплатить по ссылке, бронь закрепится сразу после оплаты: ||| [BOOKING_LINK rooms=Лофт:2]»
Если дат нет — ссылку не давай, сначала уточни даты.
Если гость выбрал комбинацию из нескольких номеров — перечисли ВСЕ номера в токене, по пункту на каждый с числом гостей в нём, например [BOOKING_LINK rooms=Номер Стандарт:3, Номер Стандарт:3, Номер Стандарт:2]. Система соберёт ссылку с уже выбранными номерами.
ОПЛАТА — НИКОГДА НЕ ПОДТВЕРЖДАЙ НА СЛОВО:
Ты НЕ подтверждаешь оплату сам. Подтверждение приходит АВТОМАТИЧЕСКИ от системы, когда деньги реально поступили — отдельным сообщением «Оплата получена ✅».
Если гость пишет «я оплатил», «оплата прошла», «подтвердите» или присылает чек/скриншот — НЕ говори «бронь подтверждена», «оплата получена», «всё готово». Гость может ошибиться или обмануть, а ты денег не видишь.
Отвечай мягко и нейтрально: «Спасибо! ||| Как оплата пройдёт в системе — сразу подтвержу бронь 🤗» или «Принято, проверю поступление и подтвержу». Без утверждений, что деньги уже пришли.
Только система (по факту реальной оплаты) присылает «Оплата получена ✅ бронь подтверждена». Ты этого сам не пишешь никогда.

ОПЛАТА НЕ ПО ССЫЛКЕ:
Если гость хочет оплатить другим способом (перевод, по реквизитам, наличными, выставить счёт) — НЕ давай реквизиты карты и не придумывай их сам. Скажи, что удобнее закрепить бронь по ссылке, а по другому способу оплаты вопрос решает администратор Асель. Например: «Удобнее всего закрепить бронь по ссылке ||| По другому способу оплаты позвоните нашему администратору Асель: +7-913-693-68-19 ||| Или дождитесь, она ответит вам прямо здесь». Подтвердить такую оплату ты не можешь — поступление проверит и подтвердит администратор.

СОБАКА / ЖИВОТНЫЕ:
Можно. 500₽ в сутки за каждую собаку. Оплата на месте при заезде, паспорт здоровья показать на месте.
Собака в сумму проживания и в ссылку НЕ входит — это отдельно, платится на ресепшене. Говори просто «собака 500₽ в сутки, оплата на месте», итоговую сумму за собаку считать и называть не нужно.

ФОТО:
Когда гость просит фото — фото отправит система сама, тебе ничего слать не нужно. Твой текст при этом — МИНИМАЛЬНЫЙ: одна тёплая строка про номер + короткий вопрос про даты. Без перечисления удобств, без цены, без вместимости и доплат.
Хороший пример ответа на «покажи фото модуля»: «Это наш Модуль 🤗 ||| Отдельный домик у речки ||| На какие даты смотрите?»
НИКОГДА не пиши служебных фраз вроде «фотографии автоматически отправлены», «отправляю фото», «сейчас отправлю фото». Это внутренняя механика.

АДМИНИСТРАТОР АСЕЛЬ +7-913-693-68-19:
Когда вопрос вне твоей компетенции (группа 10+ человек, жалоба/конфликт, договор/счёт для организации, проблема при заезде, скидка, поздний выезд, нестандартная оплата) — направляй к администратору. Называй её именно «администратор Асель» и проси ПОЗВОНИТЬ. Гость уже в чате, поэтому НЕ проси его «написать здесь» ещё раз — это нелогично. Предложи позвонить ИЛИ дождаться ответа в этом же чате. Шаблон: «Позвоните нашему администратору Асель: +7-913-693-68-19 ||| Или дождитесь, она ответит вам прямо здесь». Телефон указывай всегда, когда направляешь к ней.

СКИДКИ:
Цены в системе фиксированные. Сам скидку не обещай и не торгуйся. Скидки и особые условия решает администратор Асель — если гость просит скидку, коротко перенаправь: «Цены фиксированные ||| По индивидуальным условиям позвоните нашему администратору Асель: +7-913-693-68-19 ||| Или дождитесь, она ответит вам здесь». Не отказывай резко и не спорь.

ГРУППА 10+ ЧЕЛОВЕК (важно): НЕ считай комбинации сам и НЕ выдумывай вместимость. Для таких групп размещение подбирает администратор Асель. Ответь тепло: «На такую большую компанию размещение подберёт наш администратор Асель 🤗 ||| Позвоните ей: +7-913-693-68-19 ||| Или дождитесь, она ответит вам прямо здесь». Не перечисляй номера, не считай цены — иначе ошибёшься во вместимости.

УСЛУГИ:
Баня 1500₽/час, минимум 2 часа. Кафе 08:00-21:00. Беседки у речки, мангалы, костровище, детская площадка, парковка бесплатно.
Везде: кровать-трансформер, диван, туалет, душ, фен, чайник, посуда, WiFi, холодильник (кроме Номер Стандарт).

ПРАВИЛА:
Заезд 14:00, выезд 12:00. Предоплата 50%. Документы, паспорта, оплата за животных — всё на месте при заезде.
Поздний выезд (после 12:00) — по возможности и за доплату, решает администратор Асель. Если спрашивают: «Поздний выезд по возможности и за доплату ||| Позвоните нашему администратору Асель: +7-913-693-68-19 ||| Или дождитесь, она ответит вам здесь». Пиши именно «поздний выезд», без других слов.
Отмена: 7+ дней — штраф 10%, менее 7 дней — предоплата не возвращается.

МЕСТО: Республика Алтай, Улаганский район, с. Акташ, ул. Лесная 1Б. Первая линия реки Чуя, горы вокруг.

ЭКСКУРСИИ (мин 4 чел): Ретранслятор 3000₽, Озеро Горных духов 3000₽, Чуйские меандры 2500₽, Мажойские каскады 2000₽, Улаганский перевал 2000₽, Кату-Ярык 5000-5500₽, Куркуре 5500₽, Учар 7000₽, Каменные грибы 6250₽, Марс 4000-4500₽. Трансфер Горно-Алтайск 35000₽.

ПРИМЕРЫ:
Клиент: Скинь фото модуля
Асель: Это наш Модуль 🤗 ||| Отдельный домик у речки ||| На какие даты смотрите?

Клиент: С нами собачка
Асель: Можно 🐕 ||| 500₽/день, оплата при заезде ||| Возьмите паспорт здоровья ||| Какие даты?

Клиент: Лофт на двоих 14-17 июня
Асель: Лофт свободен 🤗 ||| 14-17 июня, 3 ночи, 22 500₽ ||| Можете оплатить по ссылке, бронь закрепится сразу после оплаты: ||| [BOOKING_LINK rooms=Лофт:2]

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

_RANGE_MONTH = re.compile(r'(\d{1,2})\s*(?:по|до|[-–—])\s*(\d{1,2})\s*([а-я]+)')      # "20-27 июля"
_MONTH_RANGE = re.compile(r'([а-я]+)\s*,?\s*(\d{1,2})\s*(?:по|до|[-–—])\s*(\d{1,2})')  # "июль 20-27", "Июль, 20-27 числа"

def _ranges_in(text_low, today):
    """Все диапазоны дат в тексте в ОБОИХ порядках: 'ДД-ДД месяц' и 'месяц ДД-ДД'.
    Возвращает список [d1, d2]. Нужно, чтобы понимать и '20-27 июля', и 'Июль, 20-27 числа'."""
    found = []
    for m in _RANGE_MONTH.finditer(text_low):
        month = _month_from_word(m.group(3))
        if month:
            d1 = _make_date(int(m.group(1)), month, today)
            d2 = _make_date(int(m.group(2)), month, today)
            if d1 and d2:
                found.append([d1, d2])
    for m in _MONTH_RANGE.finditer(text_low):
        month = _month_from_word(m.group(1))
        if month:
            d1 = _make_date(int(m.group(2)), month, today)
            d2 = _make_date(int(m.group(3)), month, today)
            if d1 and d2:
                found.append([d1, d2])
    return found

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

    # 2) Диапазон с месяцем в любом порядке: "9-13 июня", "июнь 9-13", "Июль, 20-27 числа"
    ranges = _ranges_in(text_low, today)
    if ranges:
        return ranges[0]

    # 3) Одиночные пары "число месяц" — "13 июня", "с 9 июня по 13 июля"
    for m in re.finditer(r'(\d{1,2})\s*([а-я]+)', text_low):
        month = _month_from_word(m.group(2))
        if month:
            d = _make_date(int(m.group(1)), month, today)
            if d:
                dates.append(d)
    return dates

def extract_last_range(text):
    """Последний диапазон дат в тексте (оба порядка написания). Для сообщений бота
    '21-24 занято, но свободно 23-26 июля' вернёт альтернативу (23-26)."""
    ranges = _ranges_in(text.lower(), datetime.now())
    return ranges[-1] if ranges else None

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
    if any(w in tl for w in ['пятерых', 'пятеро']):
        return 5
    if any(w in tl for w in ['шестерых', 'шестеро']):
        return 6
    if any(w in tl for w in ['семерых', 'семеро']):
        return 7
    if any(w in tl for w in ['восьмерых', 'восьмеро']):
        return 8
    m = re.search(r'(\d{1,2})\s*(?:чел|человек|взросл|гост)', tl)
    if m:
        return max(1, int(m.group(1)))
    return None

def extract_children(text):
    """Возрасты детей из сообщения -> список чисел. 'дети 4 и 6 лет' -> [4, 6].
    Отсекает даты (14-17 июня), берёт только числа рядом со словом про детей."""
    tl = text.lower()
    m = re.search(r'(ребён|ребен|дет|малыш|сын|доч)', tl)
    if not m:
        return []
    # Берём кусок текста ОТ слова про детей (даты обычно идут до/после отдельно)
    seg = tl[m.start(): m.start() + 60]
    # Обрезаем на названии месяца — чтобы не схватить дату вида "14-17 июня"
    seg = re.split(r'(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)', seg)[0]
    # Убираем диапазоны-даты вида 14-17
    seg = re.sub(r'\d{1,2}\s*[-–—]\s*\d{1,2}', ' ', seg)
    ages = [int(n) for n in re.findall(r'\d{1,2}', seg)]
    return [a for a in ages if 0 <= a <= 17]

def analyze_image(image_url):
    """Скачивает присланное фото и описывает его через Claude Vision. Возвращает строку-описание или None."""
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

def transcribe_audio(audio_url):
    """Скачивает голосовое и распознаёт речь через OpenAI Whisper. Возвращает текст или None."""
    if not OPENAI_API_KEY:
        sys.stderr.write("transcribe: нет OPENAI_API_KEY\n"); sys.stderr.flush()
        return None
    try:
        audio = requests.get(audio_url, timeout=20, verify=False)
        if audio.status_code != 200:
            sys.stderr.write(f"transcribe: аудио не скачалось {audio.status_code}\n"); sys.stderr.flush()
            return None
        # Имя файла с расширением — Whisper определяет формат по нему (ogg для WhatsApp)
        fname = audio_url.split("?")[0].split("/")[-1] or "voice.ogg"
        if "." not in fname:
            fname += ".ogg"
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (fname, audio.content)},
            data={"model": "whisper-1", "language": "ru"},
            timeout=60
        )
        if r.status_code == 200:
            text = (r.json().get("text") or "").strip()
            sys.stderr.write(f"transcribe OK: {text[:80]}\n"); sys.stderr.flush()
            return text or None
        sys.stderr.write(f"transcribe error {r.status_code}: {r.text[:150]}\n"); sys.stderr.flush()
        return None
    except Exception as e:
        sys.stderr.write(f"transcribe error: {e}\n"); sys.stderr.flush()
        return None

AVAIL_KW = ["свобод", "есть ли", "можно", "можете", "забронир", "брон", "заезд",
            "засел", "размест", "ноч", "чел", "человек", "мест", "приед",
            "остановит", "номер", "дата", "числ", "взросл", "детей", "ребён",
            "ребенк", "семь", "собак", "нас двое", "нас трое", "нас четыр",
            "цена", "цену", "цены", "стоимост", "стоит", "почём", "почем", "сколько"]

# Короткие ответы-согласия/выбор в идущем диалоге — чтобы продолжить бронь и подтянуть даты из истории
CONTINUATION_SET = {"да", "давай", "давайте", "ага", "угу", "хорошо", "ок", "окей", "конечно",
                    "посчитай", "посчитайте", "подойдет", "подойдёт", "подходит", "берем", "берём",
                    "беру", "оформляй", "оформляйте", "вариант", "первый", "второй", "третий",
                    "четвертый", "четвёртый", "это", "этот", "согласен", "согласна", "ладно", "годится"}

def is_continuation(text):
    """Короткий утвердительный/выбирающий ответ ('да', 'давай', 'вариант 2', '2'), чтобы
    не оборвать бронь: тогда даты берём из истории и считаем цену по реальным данным."""
    t = (text or "").strip().lower().strip("?.!,")
    if not t:
        return False
    if t in {"1", "2", "3", "4", "5"}:   # выбор варианта одной цифрой
        return True
    words = re.findall(r'[а-яё]+', t)
    return any(w in CONTINUATION_SET for w in words)

FOTO_KEYWORDS = ["фото", "покажи", "фотки", "посмотреть", "как выглядит", "покажите",
                 "фотографии", "скинь", "скиньте", "пришли", "прислать", "загляни", "посмотри"]

# Мат и грубые оскорбления (стемы, чтобы ловить склонения). Только однозначно бранное —
# чтобы не зацепить раздражённого, но реального гостя.
ABUSE_STEMS = ["хуй", "хуе", "хуё", "хую", "пизд", "ебал", "ебан", "ёбан", "ебло", "ебуч",
               "заеба", "наеба", "уеба", "разъеб", "долбоёб", "долбоеб", "бляд", "блят",
               "сука", "суки", "сучк", "мудак", "мудил", "гандон", "гондон", "пидор", "пидар",
               "членосос", "залуп", "уёбищ", "уебищ", "ублюд", "выродок", "дерьмо", "говно",
               "сдохни", "мраз", "нахуй", "нахер", "пошёл ты", "пошел ты", "пошла ты"]

def looks_abusive(text):
    """Грубый мат/оскорбление. Используется только в связке с проверкой на отсутствие намерения брони."""
    tl = (text or "").lower()
    return any(s in tl for s in ABUSE_STEMS)

def has_booking_intent(text):
    """Есть ли признаки реального обращения: даты, тип номера, число гостей или ключевые слова брони."""
    tl = (text or "").lower()
    return (bool(extract_dates(text)) or bool(detect_room_type(text))
            or (extract_adults(text) is not None) or any(kw in tl for kw in AVAIL_KW))

def resolve_booking_dates(user_message, history):
    """Даты для ссылки на бронь: из текущего сообщения, иначе из сообщений гостя, иначе из последнего предложения бота.
    Возвращает (date_from, date_to) в формате ГГГГ-ММ-ДД или (None, None)."""
    d = extract_dates(user_message)
    if not d:
        for h in reversed(history):
            f = extract_dates(h['content']) if h['role'] == 'user' else extract_last_range(h['content'])
            if f:
                d = f
                break
    if not d:
        return None, None
    df = d[0]
    if len(d) >= 2:
        return df, d[1]
    nights = extract_nights(user_message)
    if nights is None:
        for h in reversed(history):
            if h['role'] == 'user':
                n = extract_nights(h['content'])
                if n:
                    nights = n
                    break
    nights = nights or 1
    dt = (datetime.strptime(df, '%Y-%m-%d') + timedelta(days=nights)).strftime('%Y-%m-%d')
    return df, dt

def resolve_guest_count(user_message, history):
    """Сколько всего гостей. Порядок: явная форма в текущем сообщении -> явная в истории гостя ->
    число, которым гость ответил на вопрос бота 'сколько человек'. None если нигде не указано."""
    a = extract_adults(user_message)
    if a:
        return a
    for h in reversed(history):
        if h['role'] == 'user':
            a = extract_adults(h['content'])
            if a:
                return a
    # Гость мог описать состав суммой: "3+3+2" -> 8
    for src in [user_message] + [h['content'] for h in reversed(history) if h['role'] == 'user']:
        m = re.search(r'\b\d+(?:\s*\+\s*\d+)+', src)
        if m:
            total = sum(int(x) for x in re.findall(r'\d+', m.group(0)))
            if 2 <= total <= 30:
                return total
    # Число-ответ сразу после вопроса бота про количество гостей (например бот: «Сколько человек?» гость: «7»)
    guest_q = ('сколько человек', 'сколько вас', 'сколько гостей', 'на скольких', 'сколько людей', 'сколько будет')
    seq = history + [{'role': 'user', 'content': user_message}]
    for i in range(1, len(seq)):
        if seq[i]['role'] == 'user' and seq[i - 1]['role'] == 'assistant':
            if any(q in seq[i - 1]['content'].lower() for q in guest_q):
                m = re.search(r'\b(\d{1,2})\b', seq[i]['content'])
                if m and 1 <= int(m.group(1)) <= 20:
                    return int(m.group(1))
    return None

def resolve_guests(user_message, history):
    """Число гостей всей группы и возрасты детей (из текущего сообщения или из истории)."""
    adults = resolve_guest_count(user_message, history)
    children = extract_children(user_message)
    if not children:
        for h in reversed(history):
            if h['role'] == 'user':
                c = extract_children(h['content'])
                if c:
                    children = c
                    break
    return adults, children

def get_ai_response(user_message, chat_id):
    history = get_history(chat_id)
    bnovo_context = ""
    if should_greet(chat_id):
        bnovo_context += f"\n[GREET]: первое обращение гостя за сегодня — поздоровайся естественно (например «{greeting_word()}»), потом ответь."
    msg_low = user_message.lower()
    dates = extract_dates(user_message)
    # Гейт: наличие подтягиваем только если сообщение про даты/номера/бронь,
    # а не на "баня есть?" / "да" — иначе цепляем стейл-даты из истории.
    intent = bool(dates) or detect_room_type(user_message) or (extract_adults(user_message) is not None) or any(kw in msg_low for kw in AVAIL_KW) or is_continuation(user_message)
    if intent and not dates:
        # Берём самые свежие даты из диалога: от гостя (extract_dates) или из последнего предложения бота
        # (extract_last_range) — что встретится раньше при обходе с конца. Так согласие гостя после
        # альтернативы от бота («свободно 12-21») цепляется к ЭТИМ датам, а не к старым занятым.
        for h in reversed(history):
            found = extract_dates(h['content']) if h['role'] == 'user' else extract_last_range(h['content'])
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
        need_guests = resolve_guest_count(user_message, history)
        need_rooms = requested_room_count(user_message, history)
        data_ctx, free_names = build_availability_context(date_from, date_to, datetime.now(), need_guests, need_rooms)
        bnovo_context += f"\n[BNOVO_DATA]: {data_ctx}"
        price_block = build_price_block(date_from, date_to, free_names)
        if price_block:
            bnovo_context += f"\n{price_block}"
        # ГРУНТИНГ: если гость назвал конкретный тип, которого НЕТ среди свободных — подсвечиваем явно,
        # чтобы модель не повторяла слова гостя и не предлагала/не считала несуществующий вариант.
        if free_names:
            req_keys = requested_room_keys(user_message, history)
            req_names = [ROOM_TYPES[KEY_TO_TYPEID[k]] for k in req_keys if k in KEY_TO_TYPEID]
            missing = [nm for nm in req_names if nm not in free_names]
            if missing:
                bnovo_context += (
                    f"\n[НЕТ В НАЛИЧИИ]: на эти даты НЕТ: {', '.join(missing)}. "
                    "Не предлагай эти типы, не называй по ним цену и не давай на них ссылку, даже если "
                    "гость их просит. Честно скажи, что на эти даты их нет, и предложи только из свободного выше."
                )
        # Гость назвал число номеров — не урезай его молча.
        if need_rooms:
            bnovo_context += (
                f"\n[ЧИСЛО НОМЕРОВ]: гость просит {need_rooms} номера(ов). Подбери ровно столько, "
                "комбинируя из свободных типов. Не предлагай меньше номеров, чем просит. Если столько "
                "собрать нельзя — честно скажи и предложи максимум возможного из свободного."
            )
    sys.stderr.write(f"DEBUG dates={dates} | bnovo={bnovo_context[:150]}\n"); sys.stderr.flush()
    messages = history + [{"role": "user", "content": user_message + bnovo_context}]
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    reply = response.content[0].text
    # Ссылку на бронь строит и подставляет КОД — только когда модель попросила токеном [BOOKING_LINK].
    # Модель в числе гостей номера уже учла детей от 5 как доп. место, а малышей до 5 не считала,
    # поэтому предзаполняем оформление по занятости из токена (и одиночные, и группы).
    tok = re.search(r'\[BOOKING_LINK(?:\s+rooms=([^\]]*))?\]', reply)
    if tok:
        # Даты для ссылки берём СНАЧАЛА из самого ответа бота: если он предложил альтернативу
        # («занято, но свободно 19-21»), ссылка должна вести на ЭТИ даты, а не на исходные занятые
        # из сообщения гостя. Только если в ответе диапазона нет — откатываемся на историю/сообщение.
        rng = extract_last_range(reply)
        if rng and len(rng) >= 2:
            df, dt = rng[0], rng[1]
        else:
            df, dt = resolve_booking_dates(user_message, history)
        link = None
        if df and dt and in_season(datetime.strptime(df, '%Y-%m-%d')):
            rooms = parse_room_spec(tok.group(1)) if tok.group(1) else None
            if rooms:
                link = build_prefilled_link(df, dt, rooms, phone=chat_id)
            if not link:  # номера не распознаны — ссылка на список, гость выберет сам
                ad, ch = resolve_guests(user_message, history)
                link = build_booking_link(df, dt, ad or 2, phone=chat_id, children=ch)
        if link:
            reply = re.sub(r'\[BOOKING_LINK(?:\s+rooms=[^\]]*)?\]', lambda _: link, reply)
        else:
            # Ссылку не собрать (чаще всего нет дат — например после сброса истории). Не оставляем
            # пустого обещания «вот ссылка»: выкидываем токен и сегменты-обещания про ссылку/оплату.
            segs = [ln.strip() for ln in re.split(r'\s*\|\|\|\s*|\n', reply)
                    if ln.strip() and "[BOOKING_LINK" not in ln]
            kept = [s for s in segs if not re.search(r'ссылк|оплат', s.lower())]
            reply = "\n".join(kept).strip() if kept else \
                "Уточните, пожалуйста, даты заезда и выезда, пришлю ссылку на бронирование"
    return reply

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

_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF]")
_last_emoji = {}  # chat_id -> множество эмодзи последнего отправленного сообщения

def dedup_emoji(chat_id, text):
    """Не повторяем тот же эмодзи: помним последний использованный в чате и вырезаем его повтор,
    пока не появится ДРУГОЙ эмодзи. Сообщения без эмодзи память не сбрасывают. Другой смайлик можно."""
    if not text:
        return text
    prev = _last_emoji.get(chat_id) or set()
    for e in set(_EMOJI_RE.findall(text)):
        if e in prev:
            text = text.replace(e, "")
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'\s+([,.:!?])', r'\1', text).strip()
    now = set(_EMOJI_RE.findall(text))
    if now:                       # обновляем память только когда в сообщении реально есть эмодзи
        _last_emoji[chat_id] = now
    return text

def sanitize_outgoing(text):
    """Чистим текст перед отправкой в WhatsApp/MAX:
    - убираем тире (— и –) — живой человек их не печатает; дефис в датах 14-17 не трогаем
    - глушим markdown-ссылки и голые ссылки на фото-домен (фото шлёт система отдельным файлом)
    - снимаем markdown-звёздочки на всякий случай (в WhatsApp/MAX они видны буквально)
    Это страховка на случай, если модель проскочит мимо правил промпта."""
    if not text:
        return text
    # markdown-ссылки [текст](url) -> убрать (фото уходит отдельным файлом, не ссылкой)
    text = re.sub(r'\[[^\]]*\]\(https?://[^)]+\)', '', text)
    # голые ссылки на фото-домен (раздаём фото через Wazzup contentUri, не текстом)
    text = re.sub(r'https?://\S*githubusercontent\.com/\S+', '', text)
    # тире — и – и ― заменяем на запятую (дефис-минус в датах остаётся как есть)
    text = re.sub(r'\s*[—–―]\s*', ', ', text)
    # markdown-выделение *жирный* / **bold** -> просто текст
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    # подчистка двойных пробелов и висящих знаков после вырезаний
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'\s+([,.:])', r'\1', text)
    return text.strip()

_notified_handoff = {}            # chat_id -> ts: антиспам уведомлений Асели (не чаще раза в 10 мин на чат)
_notify_lock = threading.Lock()

def notify_asel(text, button=None):
    """Шлём Асели уведомление в Telegram. Тихо ничего не делаем, если Telegram не настроен (нет env).
    button — опциональная инлайн-кнопка {'text':..., 'url':...} («Открыть чат»)."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        sys.stderr.write("notify_asel: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — уведомление пропущено\n"); sys.stderr.flush()
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if button and button.get("url"):
        payload["reply_markup"] = {"inline_keyboard": [[{"text": button["text"], "url": button["url"]}]]}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json=payload, timeout=10)
        sys.stderr.write(f"notify_asel: status={r.status_code} body={r.text[:150]}\n"); sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"notify_asel error: {e}\n"); sys.stderr.flush()

def maybe_notify_handoff(chat_id, chat_type, guest_name, guest_phone, guest_msg, reply):
    """Если бот направил гостя к Асели (в ответе её телефон) — пингуем её в Telegram с ТЕЛЕФОНОМ гостя,
    чтобы она могла перезвонить или ответить в чате. Дедуп: не чаще одного уведомления в 10 минут на чат."""
    if ASEL_PHONE_TAIL not in re.sub(r"\D", "", reply or ""):
        return
    now = time.time()
    with _notify_lock:
        if now - _notified_handoff.get(chat_id, 0) < 600:
            return
        _notified_handoff[chat_id] = now
        for k in [k for k, ts in _notified_handoff.items() if now - ts > 3600]:
            _notified_handoff.pop(k, None)
    phone_line = f"+{guest_phone}" if guest_phone else "не передан (только чат)"
    # Кнопка «Открыть чат» только для WhatsApp: там есть публичная ссылка wa.me/<номер>.
    # У MAX публичной ссылки на чат нет — Асель ищет по имени или звонит.
    button = None
    if chat_type == "whatsapp" and guest_phone:
        button = {"text": "💬 Открыть чат", "url": f"https://wa.me/{guest_phone}"}
        find_hint = "Нажмите «Открыть чат» или перезвоните гостю."
    else:
        find_hint = "Найдите чат по имени в приложении или перезвоните гостю."
    notify_asel(
        "🔔 Гость ждёт ответа администратора\n"
        f"Имя в {chat_type}: {guest_name or 'не указано'}\n"
        f"Телефон: {phone_line}\n"
        f"Спросил: {(guest_msg or '')[:200]}\n"
        + find_hint,
        button=button
    )

def send_wazzup_message(chat_id, channel_id, text, chat_type="whatsapp"):
    text = sanitize_outgoing(text)
    text = dedup_emoji(chat_id, text)
    if not text or not text.strip():
        return None
    remember_bot_sent(text)
    url = "https://api.wazzup24.com/v3/message"
    headers = {"Authorization": f"Bearer {WAZZUP_API_KEY}", "Content-Type": "application/json"}
    payload = {"channelId": channel_id, "chatId": chat_id, "chatType": chat_type, "text": text}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code not in (200, 201):
            # Невалидный/просроченный ключ или неверный channelId — иначе отправка падает молча
            sys.stderr.write(f"Wazzup send FAILED status={r.status_code} body={r.text[:200]}\n"); sys.stderr.flush()
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

def split_into_messages(full_text):
    """Режем ответ на отдельные сообщения по ||| ИЛИ по пустым строкам.
    Модель не всегда ставит |||, на длинных ответах разделяет переносами — учитываем оба варианта,
    чтобы сообщения шли пузырями, а не одной простынёй."""
    chunks = re.split(r'\s*\|\|\|\s*|\n\s*\n', full_text)
    return [c.strip() for c in chunks if c.strip()]

def send_wazzup_multi(chat_id, channel_id, full_text, chat_type="whatsapp"):
    # Защита: если модель оставила неподставленный токен ссылки — убираем сегменты с ним
    if "[BOOKING_LINK" in full_text:
        full_text = "|||".join(
            p for p in full_text.split("|||") if "[BOOKING_LINK" not in p
        )
    parts = split_into_messages(full_text)
    for i, part in enumerate(parts):
        if i:
            time.sleep(0.25)  # маленький зазор только для сохранения порядка сообщений
        send_wazzup_message(chat_id, channel_id, part, chat_type)
    sys.stderr.write(f"Wazzup: отправлено {len(parts)} сообщений\n"); sys.stderr.flush()

def send_max_message(chat_id, text):
    text = sanitize_outgoing(text)
    text = dedup_emoji(chat_id, text)
    if not text or not text.strip():
        return
    remember_bot_sent(text)
    url = "https://botapi.max.ru/messages"
    params = {"access_token": MAX_TOKEN}
    payload = {"recipient": {"chat_id": chat_id}, "text": text}
    try:
        requests.post(url, params=params, json=payload, timeout=15)  # FIX #7: timeout
    except Exception as e:
        sys.stderr.write(f"Max send error: {e}\n"); sys.stderr.flush()

def send_max_multi(chat_id, full_text):
    parts = split_into_messages(full_text)
    for i, part in enumerate(parts):
        if i:
            time.sleep(0.25)
        send_max_message(chat_id, part)

DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "3"))   # ждём, пока гость допишет серию сообщений
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
                     "chat_type": msg.get("chatType", "whatsapp"), "phone": None, "timer": None}
            _pending[chat_id] = entry
        entry["msgs"].append(msg)
        if msg.get("channelId"):
            entry["channel_id"] = msg.get("channelId")
        if msg.get("chatType"):
            entry["chat_type"] = msg.get("chatType")
        ph = _guest_phone_from_msg(msg)
        if ph:
            entry["phone"] = ph
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
    # Запоминаем маршрут гостя (канал/мессенджер/телефон), чтобы подтверждение оплаты ушло туда же
    save_chat_route(chat_id, channel_id, chat_type, entry.get("phone"))
    # Ручной режим: бот на паузе (Асель ведёт диалог) или чат в стоп-листе — не отвечаем,
    # но сохраняем текст гостя в историю, чтобы при возврате бот видел контекст.
    if is_bot_paused(chat_id):
        plain = " ".join(m.get("text", "") for m in msgs
                         if m.get("type", "text") == "text" and m.get("text")).strip()
        if plain:
            save_message(chat_id, "user", plain)
        sys.stderr.write(f"PAUSED: гость написал в {chat_id}, бот молчит (ручной режим)\n"); sys.stderr.flush()
        return
    try:
        texts = []
        photo_descs = []
        room_from_photo = None
        quoted_text = None
        quoted_unresolved = False
        has_audio = False
        has_image = False
        for msg in msgs:
            mt = msg.get("type", "text")
            if mt in ("audio", "voice", "ptt"):
                has_audio = True
                url = msg.get("contentUri") or msg.get("fileUrl") or msg.get("url", "")
                if url:
                    spoken = transcribe_audio(url)
                    if spoken:
                        texts.append(spoken)  # распознанная речь идёт как обычный текст
            elif mt in ("image", "photo"):
                has_image = True
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
                if q:
                    # 1) текст из нашей базы по messageId; 2) текст прямо из вебхука (если мессенджер его кладёт) —
                    # так резолвятся даже старые сообщения и сообщения, отправленные до внедрения бота
                    qt = get_msg_text(q.get("messageId")) or q.get("text") or (q.get("message") or {}).get("text")
                    if qt:
                        quoted_text = qt
                    else:
                        quoted_unresolved = True  # цитата есть, но текст недоступен — попросим уточнить

        combined = " ".join(texts).strip()

        # Тролль/мат без намерения брони — не реагируем (не кормим тролля и не тратим ответ модели)
        if combined and looks_abusive(combined) and not has_booking_intent(combined):
            save_message(chat_id, "user", combined)
            sys.stderr.write(f"IGNORED abuse from {chat_id}: {combined[:60]}\n"); sys.stderr.flush()
            return

        # Медиа пришло, но распознать не удалось (пустой текст и нет описания фото)
        if not combined and not photo_descs:
            if has_image:
                send_wazzup_multi(chat_id, channel_id, "Спасибо за фото 🤗 ||| Подскажите, по какому номеру вопрос?", chat_type)
            elif has_audio:
                send_wazzup_multi(chat_id, channel_id, "Ой, не расслышала голосовое 🙏 ||| Повторите или напишите текстом, сразу помогу", chat_type)
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
            ai_input = f"(гость пишет в ответ на сообщение: «{quoted_text}») {ai_input}".strip()
        elif quoted_unresolved:
            ai_input = ("(гость ответил цитатой на старое сообщение, его текст недоступен — "
                        "если из его слов непонятно, о чём речь, мягко уточни, а не угадывай) " + ai_input).strip()

        ai_reply = get_ai_response(ai_input, chat_id)
        save_user = combined if combined else f"[фото: {'; '.join(photo_descs)[:100]}]"
        save_message(chat_id, "user", save_user)
        save_message(chat_id, "assistant", ai_reply)
        # Запоминаем тексты входящих гостя по их messageId — чтобы он мог потом ответить reply на своё же сообщение
        for m in msgs:
            if m.get("type", "text") == "text" and m.get("text") and m.get("messageId"):
                remember_msg_text(m["messageId"], m["text"])
        send_wazzup_multi(chat_id, channel_id, ai_reply, chat_type)
        # Бот направил гостя к Асели? Пингуем её в Telegram с телефоном гостя
        guest_name = next((m.get("contact", {}).get("name") for m in msgs
                           if m.get("contact", {}).get("name")), None)
        maybe_notify_handoff(chat_id, chat_type, guest_name, entry.get("phone"), combined, ai_reply)
    except Exception as e:
        sys.stderr.write(f"flush_chat error: {e}\n"); sys.stderr.flush()

def process_max_message(chat_id, text):
    """FIX #6: фоновая обработка сообщения из MAX."""
    if is_bot_paused(chat_id):
        save_message(chat_id, "user", text)
        sys.stderr.write(f"PAUSED MAX: бот молчит в {chat_id} (ручной режим)\n"); sys.stderr.flush()
        return
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
            # Исходящее сообщение. Если его отправил НЕ бот — значит Асель написала вручную:
            # ставим чат на паузу и сохраняем её реплику в историю (контекст для возврата бота).
            if msg.get("status") != "inbound":
                txt = msg.get("text", "")
                cid = msg.get("chatId", "")
                if txt and cid and not was_sent_by_bot(txt):
                    pause_chat(cid)
                    save_message(cid, "assistant", txt)
                    sys.stderr.write(f"HANDOVER: оператор написал в {cid}, бот на паузе {HANDOVER_PAUSE_HOURS}ч\n"); sys.stderr.flush()
                continue
            if msg.get("chatType") == "whatsgroup":
                continue
            chat_id = msg.get("chatId", "")
            if not chat_id:
                continue
            # Постоянный стоп-лист: бот вообще не трогает эти чаты
            if is_bot_disabled(chat_id):
                sys.stderr.write(f"SKIP disabled chat {chat_id}\n"); sys.stderr.flush()
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

@app.route("/admin/bot", methods=["GET", "POST"])
def admin_bot():
    """Ручное управление ботом по чату (нужен ADMIN_TOKEN в env).
    Примеры:
      /admin/bot?token=XXX&chat_id=79991234567&action=disable   (навсегда заглушить — друзья/личка)
      /admin/bot?token=XXX&chat_id=79991234567&action=enable    (вернуть бота в чат)
      /admin/bot?token=XXX&chat_id=79991234567&action=pause&hours=24   (временная пауза)
      /admin/bot?token=XXX&chat_id=79991234567&action=resume
      /admin/bot?token=XXX&action=list                          (список отключённых чатов)
      /admin/bot?token=XXX&chat_id=79991234567                  (статус чата)"""
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return {"error": "unauthorized"}, 401
    action = request.args.get("action", "status")
    if action == "list":
        return {"disabled_env": sorted(BOT_DISABLED_CHATS), "disabled_manual": sorted(_disabled_db)}
    chat_id = request.args.get("chat_id", "")
    if not chat_id:
        return {"error": "chat_id required"}, 400
    if action == "disable":
        disable_chat(chat_id)
        return {"chat_id": chat_id, "disabled": True}
    if action == "enable":
        enable_chat(chat_id)
        return {"chat_id": chat_id, "disabled": False}
    if action == "pause":
        hours = float(request.args.get("hours", HANDOVER_PAUSE_HOURS))
        pause_chat(chat_id, hours)
        return {"chat_id": chat_id, "paused": True, "hours": hours}
    if action == "resume":
        resume_chat(chat_id)
        return {"chat_id": chat_id, "paused": False}
    return {"chat_id": chat_id, "paused": is_bot_paused(chat_id), "disabled": is_bot_disabled(chat_id)}

def ensure_wazzup_webhook():
    """Проставляем адрес вебхука Wazzup текущим ключом. Возвращает HTTP-статус или None.
    ВАЖНО: Wazzup при установке делает тестовый запрос на сам URL и ждёт ответа, поэтому вызывать
    это нужно, когда сервер уже поднят и доступен снаружи (см. фоновый ретрай ниже)."""
    if not WAZZUP_API_KEY:
        return None
    base = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
            or "https://aktash-bot.onrender.com").rstrip("/")
    uri = base + "/webhook"
    try:
        r = requests.patch(
            "https://api.wazzup24.com/v3/webhooks",
            headers={"Authorization": f"Bearer {WAZZUP_API_KEY}", "Content-Type": "application/json"},
            json={"webhooksUri": uri, "subscriptions": {"messagesAndStatuses": True}},
            timeout=15)
        sys.stderr.write(f"Webhook autoregister: status={r.status_code} uri={uri} body={r.text[:150]}\n")
        sys.stderr.flush()
        return r.status_code
    except Exception as e:
        sys.stderr.write(f"Webhook autoregister error: {e}\n"); sys.stderr.flush()
        return None

def _webhook_autoregister_loop():
    """На старте сервис ещё не 'live' во внешней сети Render, и тестовый запрос Wazzup на URL падает
    (400 WEBHOOKS_REQUEST_NOT_VALID). Поэтому ретраим с задержками, пока не получим 200."""
    for delay in (15, 20, 40, 60, 120):
        time.sleep(delay)
        if ensure_wazzup_webhook() == 200:
            return

if __name__ == "__main__":
    threading.Thread(target=_webhook_autoregister_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
