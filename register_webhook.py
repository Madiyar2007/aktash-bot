"""
Разовая регистрация вебхука Bnovo на изменение броней.
Запустить ОДИН раз локально: python register_webhook.py
Подписка активируется в течение 24 часов (так пишет Bnovo).
"""
import os
import requests
import urllib3
from dotenv import load_dotenv
urllib3.disable_warnings()
load_dotenv()

BASE = 'https://api.pms.bnovo.ru'
USER_ID = 118966
PASSWORD = os.getenv('BNOVO_PASSWORD')

# !!! ЗАМЕНИ на свой боевой URL на Render, если отличается
WEBHOOK_URL = 'https://aktash-bot.onrender.com/bnovo-webhook'

auth = requests.post(f'{BASE}/api/v1/auth', json={'id': USER_ID, 'password': PASSWORD}, verify=False, timeout=10)
print('auth:', auth.status_code)
token = auth.json()['data']['access_token']
headers = {'Authorization': f'Bearer {token}'}

# Посмотреть текущих подписчиков
cur = requests.get(f'{BASE}/api/v1/webhooks/subscribers', headers=headers, verify=False, timeout=10)
print('текущие подписчики:', cur.status_code, cur.text[:500])

# Создать подписчика на события броней
r = requests.post(f'{BASE}/api/v1/webhooks/subscribers',
                  json={'url': WEBHOOK_URL, 'type': 'booking'},
                  headers=headers, verify=False, timeout=10)
print('создание подписчика:', r.status_code, r.text[:500])
