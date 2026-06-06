import requests
import os
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("WAZZUP_API_KEY")

# Берём из последнего сообщения в логах
chat_id = "77773133500"
channel_id = "ТВОЙ_CHANNEL_ID"

r = requests.post(
    "https://api.wazzup24.com/v3/message",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "channelId": channel_id,
        "chatId": chat_id,
        "chatType": "whatsapp",
        "isTyping": True
    }
)
print("Статус:", r.status_code)
print("Ответ:", r.text)
