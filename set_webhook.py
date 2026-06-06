import requests
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("WAZZUP_API_KEY")

url = "https://api.wazzup24.com/v3/webhooks"
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}
payload = {
    "webhooksUri": "https://aktash-bot.onrender.com/webhook",
    "subscriptions": {
        "messagesAndStatuses": True,
        "contactsAndDeals": False
    }
}

r = requests.patch(url, json=payload, headers=headers)
print("Статус:", r.status_code)
print("Ответ:", r.text)