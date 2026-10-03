# notifiers/whatsapp_callembot.py
import requests

class WhatsAppCallMeBotNotifier:
    def __init__(self, phone: str, apikey: str):
        self.phone = phone
        self.apikey = apikey
        self.url = "https://api.callmebot.com/whatsapp.php"

    def send(self, alert):
        # Formatear el mensaje (igual que el de Telegram)
        text = alert.format_message()
        try:
            # CallMeBot espera los parámetros como query string
            params = {
                "phone": self.phone,
                "text": text,
                "apikey": self.apikey
            }
            r = requests.get(self.url, params=params, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def send_recovery(self, key, device_name, sensor):
        # Implementación similar
        pass