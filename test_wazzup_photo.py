import requests
import os
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("WAZZUP_API_KEY")
headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

test_url = "https://raw.githubusercontent.com/Madiyar2007/aktash-bot/main/photos/loft/1.jpg"
chat_id = "77057710575"
channel_id = "f2fb13af-f426-40ef-a3f0-f7c5f5bb3310"

print("=== contentUri ===")
r = requests.post("https://api.wazzup24.com/v3/message", headers=headers, json={
    "channelId": channel_id,
    "chatId": chat_id,
    "chatType": "whatsapp",
    "contentUri": test_url,
})
print(r.status_code, r.text[:300])
