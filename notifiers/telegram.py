"""Notificador Telegram. Idéntico en demo y producción."""
import logging
import requests
from notifiers.base import BaseNotifier
from core.models import Alert

logger = logging.getLogger(__name__)


class TelegramNotifier(BaseNotifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id

    def send(self, alert: Alert) -> bool:
        return self._post(alert.format_message())

    def send_recovery(self, key: str, device_name: str,
                      sensor: str) -> bool:
        msg = (f"🟢 [RECUPERACIÓN]\n"
               f"El sensor {sensor} volvió a la normalidad.")
        return self._post(msg)

    def _post(self, text: str) -> bool:
        try:
            r = requests.post(self.url, json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }, timeout=10)
            return r.json().get("ok", False)
        except Exception as e:
            logger.error(f"Telegram falló: {e}")
            return False