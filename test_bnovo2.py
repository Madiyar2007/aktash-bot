import requests
import os
from dotenv import load_dotenv
import json
import urllib3
urllib3.disable_warnings()

load_dotenv()

BASE_URL = 'https://api.pms.bnovo.ru'

# Avtorizatsiya
auth = requests.post(
    f'{BASE_URL}/api/v1/auth',
    json={'id': 118966, 'password': os.getenv('BNOVO_PASSWORD')},
    verify=False
)
print("Status avtorizatsii:", auth.status_code)
print("Otvet:", auth.text[:300])
print()

if auth.status_code != 200:
    print("AVTORIZATSIYA NE PROSHLA. Proverte BNOVO_PASSWORD v .env")
    exit()

token = auth.json()['data']['access_token']
print("Token poluchen\n")
headers = {'Authorization': f'Bearer {token}'}

endpoints = [
    ('/api/v1/rooms', {}),
    ('/api/v1/room-types', {}),
    ('/api/v1/availability', {'date_from': '2026-06-21', 'date_to': '2026-06-24', 'property_id': 118966}),
    ('/api/v1/rates', {'date_from': '2026-06-21', 'date_to': '2026-06-24', 'property_id': 118966}),
    ('/api/v1/properties', {}),
]

for endpoint, params in endpoints:
    try:
        r = requests.get(f'{BASE_URL}{endpoint}', params=params, headers=headers, verify=False, timeout=10)
        print(f"{endpoint} -> {r.status_code}")
        if r.status_code == 200:
            print("   DOSTUPNO:", r.text[:400])
        print()
    except Exception as e:
        print(f"{endpoint} -> oshibka: {e}\n")

print("\n=== BOOKINGS PODROBNO ===")
r = requests.get(
    f'{BASE_URL}/api/v1/bookings',
    params={'date_from': '2026-06-14', 'date_to': '2026-06-25', 'property_id': 118966, 'limit': 3, 'offset': 0},
    headers=headers, verify=False
)
if r.status_code == 200:
    print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:2500])
