import requests
import os
from dotenv import load_dotenv
import json
import urllib3
urllib3.disable_warnings()

load_dotenv()
BASE_URL = 'https://api.pms.bnovo.ru'

auth = requests.post(
    f'{BASE_URL}/api/v1/auth',
    json={'id': 118966, 'password': os.getenv('BNOVO_PASSWORD')},
    verify=False
)
token = auth.json()['data']['access_token']
headers = {'Authorization': f'Bearer {token}'}

# Roomtypes s limit 30
print("=== TIPY KOMNAT (ID -> nazvanie) ===")
r = requests.get(f'{BASE_URL}/api/v1/roomtypes', params={'limit': 30, 'offset': 0}, headers=headers, verify=False)
print("Status:", r.status_code)
if r.status_code == 200:
    data = r.json()['data']
    types = data.get('roomtypes', data) if isinstance(data, dict) else data
    print(json.dumps(types, ensure_ascii=False, indent=2))

# Sobiraem unikalnye tipy iz rooms
print("\n=== SVODKA TIP -> NAZVANIE (iz rooms) ===")
r = requests.get(f'{BASE_URL}/api/v1/rooms', params={'limit': 30, 'offset': 0}, headers=headers, verify=False)
if r.status_code == 200:
    rooms = r.json()['data']['rooms']
    type_map = {}
    for room in rooms:
        tid = room['room_type_id']
        tname = room['room_type']
        if tid not in type_map:
            type_map[tid] = {'name': tname, 'count': 0}
        type_map[tid]['count'] += 1
    for tid, info in type_map.items():
        print(f"  {tid} = {info['name']} ({info['count']} nomerov)")
