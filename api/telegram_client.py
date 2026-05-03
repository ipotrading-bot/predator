import requests

class TelegramClient:
    def __init__(self):
        self.bot_token = st.secrets["TELEGRAM_BOT_TOKEN"]
        self.chat_id = st.secrets["TELEGRAM_CHAT_ID"]
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(self, message: str):
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message
        }
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()

def get_telegram_client():
    return TelegramClient()
