import requests
import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = 'https://api.pms.bnovo.ru'

auth = requests.post(
    f'{BASE_URL}/api/v1/auth',
    json={'id': 118966, 'password': os.getenv('BNOVO_PASSWORD')},
    verify=False
)

token = auth.json()['data']['access_token']
print("Токен получен!")

headers = {'Authorization': f'Bearer {token}'}

r = requests.get(
    f'{BASE_URL}/api/v1/bookings',
    params={
        'date_from': '2026-06-01',
        'date_to': '2026-06-30',
        'property_id': 118966,
        'limit': 50,
        'offset': 0
    },
    headers=headers,
    verify=False
)
print("Бронирования:", r.status_code)
print(r.text[:1000])