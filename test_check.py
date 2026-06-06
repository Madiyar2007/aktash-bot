import requests
import os
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()
import re

load_dotenv()
BASE_URL = 'https://api.pms.bnovo.ru'

ROOM_TYPES = {
    428964: "Modul", 428965: "Loft", 428966: "A-Frame",
    428967: "Domik Standart", 428969: "Kottedzh s terrasoy", 747057: "Nomer Standart",
}

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

# Test raspoznavaniya daty
test = "Svoboden loft na 21 iyunya?"
test2 = "Свободен лофт на 21 июня?"
print("extract '21 iyunya' (translit):", extract_dates(test))
print("extract '21 июня' (cyrillic):", extract_dates(test2))
print()

# Avtorizatsiya i proverka
auth = requests.post(f'{BASE_URL}/api/v1/auth', json={'id': 118966, 'password': os.getenv('BNOVO_PASSWORD')}, verify=False)
token = auth.json()['data']['access_token']
headers = {'Authorization': f'Bearer {token}'}

dates = extract_dates(test2)
if dates:
    df = dates[0]
    from datetime import datetime, timedelta
    dt = (datetime.strptime(df, '%Y-%m-%d') + timedelta(days=3)).strftime('%Y-%m-%d')
    print(f"Proveryaem {df} - {dt}")
    r = requests.get(f'{BASE_URL}/api/v1/availability/roomtypes', params={'date_from': df, 'date_to': dt}, headers=headers, verify=False)
    print("Status:", r.status_code)
    if r.status_code == 200:
        data = r.json()['data']
        for tid_str, info in data.items():
            tid = int(tid_str)
            name = ROOM_TYPES.get(tid, str(tid))
            full = info.get('full_quantity', 0)
            if full == 0:
                continue
            vals = list(info.get('availability', {}).values())
            min_a = min(vals) if vals else 0
            print(f"  {name}: svobodno {min_a} iz {full}")
    else:
        print(r.text[:300])
else:
    print("DATA NE RASPOZNANA!")
