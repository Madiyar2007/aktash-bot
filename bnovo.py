import requests
import os
from datetime import datetime

BASE_URL = 'https://api.pms.bnovo.ru'
PROPERTY_ID = int(os.getenv('BNOVO_PROPERTY_ID', 118966))

def get_bnovo_token():
    auth = requests.post(
        f'{BASE_URL}/api/v1/auth',
        json={
            'id': 32838,
            'password': os.getenv('BNOVO_PASSWORD')
        },
        verify=False
    )
    if auth.status_code == 200:
        return auth.json()['data']['access_token']
    return None

def check_availability(date_from, date_to):
    """Проверяет занятость номеров на указанные даты"""
    token = get_bnovo_token()
    if not token:
        return None
    
    headers = {'Authorization': f'Bearer {token}'}
    
    r = requests.get(
        f'{BASE_URL}/api/v1/bookings',
        params={
            'date_from': date_from,
            'date_to': date_to,
            'property_id': PROPERTY_ID,
            'limit': 100,
            'offset': 0
        },
        headers=headers,
        verify=False
    )
    
    if r.status_code == 200:
        bookings = r.json()['data']['bookings']
        return bookings
    return None
