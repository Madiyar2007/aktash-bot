"""
Придирчивый тест Асели. Прогоняет диалоги через мозг бота и проверяет
ответы по объективным критериям стиля (markdown, эмодзи, длина, шаблоны).

Запуск: python test_bot.py
Использует реальные Bnovo + Anthropic (немного токенов).
"""
import os
import re
import time
import main

TEST_DB = "test_chat_history.db"
main.DB_PATH = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
main.init_db()

# (название, [реплики гостя по очереди], [что проверяем — подсказки для глаза])
TESTS = [
    ("Приветствие", ["Здравствуйте"]),
    ("Наличие+цена+ссылка", ["Лофт свободен 21-24 июля на двоих?"]),
    ("A-Frame цена (спросит даты)", ["Сколько стоит а-фрейм?"]),
    ("Лофт на 4 (диван +600)", ["Лофт на 4 человека 14-17 июня"]),
    ("Стандарт на 3 (раскладушка +500)", ["Номер стандарт на троих 20-22 июля"]),
    ("A-Frame на 6 (фикс)", ["А-фрейм на 6 человек 1-3 августа"]),
    ("Модуль+собака (500x ночи)", ["Модуль на двоих с собакой 10-12 июля"]),
    ("2 собаки 4 ночи (500x2x4)", ["Лофт 14-18 июня двое, две собаки"]),
    ("Большая группа 6 (комбо)", ["Нас 6 человек 15-18 июля что есть?"]),
    ("Группа 8 (комбо)", ["8 человек 20-23 июля"]),
    ("Торг", ["Лофт 14-17 июня на двоих", "а подешевле можно?"]),
    ("Наглый торг", ["Дайте скидку 50%, я постоянный клиент"]),
    ("Вне сезона", ["А на 5 января свободно?"]),
    ("Дети 3 беспл + 7 взрослый", ["Лофт 14-17 июня, двое и дети 3 и 7 лет"]),
    ("Ребёнок ровно 5 (как взрослый)", ["Стандарт 14-16 июня, двое и ребёнок 5 лет"]),
    ("Многоход даты->гости", ["Лофт на 21-24 июля", "на двоих"]),
    ("Многоход тип->даты->да", ["Какие домики есть?", "Модуль на 14-16 июня двое", "беру"]),
    ("Отопление", ["Ночью холодно? Отопление есть?"]),
    ("Как добраться", ["Где находитесь, как доехать?"]),
    ("Wi-Fi", ["Интернет есть?"]),
    ("Баня", ["Баня сколько стоит?"]),
    ("Поздний выезд", ["Можно выехать в 18:00?"]),
    ("Бронь без предоплаты", ["Можно забронировать без предоплаты?"]),
    ("Оплатил (обман — не верить!)", ["Лофт 14-17 июня двое", "я оплатил, подтвердите"]),
    ("Грубость", ["Что за бот тупой, бесите"]),
    ("Не по теме", ["Какая погода завтра в Москве?"]),
    ("Очень большая группа 15", ["Нас 15 человек, корпоратив, 10-12 июля"]),
    ("Невозможные даты", ["Хочу 30 февраля заехать"]),
    ("Только спросил наличие", ["На 14 июня что-нибудь свободно?"]),
    ("Капс и эмоции", ["СРОЧНО НУЖЕН НОМЕР СЕГОДНЯ!!!"]),
    ("Тип не в наличии (грунтинг)", ["14-21 июля три Номера Стандарт на двоих"]),
]

def style_flags(text):
    """Объективные авто-проверки стиля. Возвращает список замечаний."""
    flags = []
    if re.search(r'\*\*|\*[^*]|__|#{1,}\s|\[.*\]\(', text):
        flags.append("⚠ MARKDOWN (звёздочки/решётки)")
    if "что ближе" in text.lower():
        flags.append("⚠ шаблон 'Что ближе?'")
    if re.search(r'202\d', text):
        flags.append("⚠ упомянут ГОД")
    if "🙂" in text:
        flags.append("⚠ смайл 🙂 (должен быть 🤗)")
    emojis = re.findall(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]', text)
    if len(emojis) > 3:
        flags.append(f"⚠ многовато эмодзи ({len(emojis)})")
    # два вопроса в одном ответе
    if text.count("?") >= 3:
        flags.append(f"⚠ много вопросов ({text.count('?')})")
    if "сосед" in text.lower():
        flags.append("⚠ упомянул соседей")
    if "не могу проверить" in text.lower() or "сбой" in text.lower():
        flags.append("⚠ отговорка про систему")
    parts = [p for p in text.split("|||") if p.strip()]
    if len(parts) > 6:
        flags.append(f"⚠ слишком длинно ({len(parts)} частей)")
    if len(text) > 900:
        flags.append(f"⚠ простыня ({len(text)} симв)")
    return flags

def grounding_flags(turn, reply, chat_id):
    """Спрашиваем у Bnovo реальное наличие на даты из реплики. Если гость просит тип, которого там НЕТ,
    бот не должен давать на него ссылку и должен сказать, что его нет. Срабатывает только когда тип
    действительно недоступен — иначе молчит (без ложных алармов)."""
    flags = []
    history = main.get_history(chat_id)
    dates = main.extract_dates(turn)
    if not dates:
        for h in reversed(history):
            f = main.extract_dates(h['content']) if h['role'] == 'user' else main.extract_last_range(h['content'])
            if f:
                dates = f
                break
    if not dates or len(dates) < 2:
        return flags  # без диапазона дат проверять наличие нечем
    df, dt = dates[0], dates[1]
    try:
        free = list(main.free_room_counts(main.check_availability_by_type(df, dt), dt).keys())
    except Exception:
        return flags
    if not free:
        return flags
    req_keys = main.requested_room_keys(turn, history)
    req_names = [main.ROOM_TYPES[main.KEY_TO_TYPEID[k]] for k in req_keys if k in main.KEY_TO_TYPEID]
    missing = [n for n in req_names if n not in free]
    if not missing:
        return flags
    if re.search(r'reservationsteps\.ru', reply):
        flags.append(f"⛔ ССЫЛКА на недоступный тип ({', '.join(missing)} нет в наличии)")
    if not any(w in reply.lower() for w in ('нет', 'занят', 'недоступ', 'к сожалению', 'не осталось', 'нету')):
        flags.append(f"⚠ не сказал, что {', '.join(missing)} недоступен (свободно: {', '.join(free)})")
    return flags

def run():
    print("=" * 72)
    print("ПРИДИРЧИВЫЙ ТЕСТ АСЕЛИ")
    print("=" * 72)
    for i, (name, turns) in enumerate(TESTS, 1):
        chat_id = f"t{i}"
        print(f"\n{'─'*72}\n[{i}] {name}")
        for turn in turns:
            print(f"\n  ГОСТЬ: {turn}")
            try:
                reply = main.get_ai_response(turn, chat_id)
            except Exception as e:
                reply = f"!!! ОШИБКА: {e}"
            ground = grounding_flags(turn, reply, chat_id)
            main.save_message(chat_id, "user", turn)
            main.save_message(chat_id, "assistant", reply)
            print("  АСЕЛЬ:")
            for part in reply.split("|||"):
                part = part.strip()
                if part:
                    print(f"     {part}")
            flags = style_flags(reply) + ground
            if flags:
                print("  " + " | ".join(flags))
            time.sleep(0.2)
    print(f"\n{'='*72}\nГОТОВО")
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

if __name__ == "__main__":
    run()
