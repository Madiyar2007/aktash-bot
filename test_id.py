import requests
import os
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()

load_dotenv()
BASE_URL = 'https://api.pms.bnovo.ru'
password = os.getenv('BNOVO_PASSWORD')

print("Pervye 10 simvolov parolya:", password[:10] if password else "PUSTO")
print("Poslednie 5 simvolov:", password[-5:] if password else "PUSTO")
print("Dlina parolya:", len(password) if password else 0)
print()

# Perebiraem raznye ID
for test_id in [32838, 118966]:
    auth = requests.post(
        f'{BASE_URL}/api/v1/auth',
        json={'id': test_id, 'password': password},
        verify=False
    )
    print(f"id={test_id}: status {auth.status_code}")
    if auth.status_code == 200:
        print("   RABOTAET! Etot ID pravilnyy")
    else:
        print("  ", auth.text[:150])
    print()
