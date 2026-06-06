import requests
import os
from dotenv import load_dotenv
import json

load_dotenv()

API_KEY = os.getenv("WAZZUP_API_KEY")
headers = {"Authorization": f"Bearer {API_KEY}"}

# Poluchaem vse chaty
print("=== VSE CHATY ===")
r = requests.get("https://api.wazzup24.com/v3/chats", headers=headers)
print("Status:", r.status_code)
if r.status_code == 200:
    chats = r.json()
    print(f"Vsego chatov: {len(chats)}")
    print(json.dumps(chats[:3], ensure_ascii=False, indent=2))
else:
    print(r.text[:300])
