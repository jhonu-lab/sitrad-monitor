"""Notificador que redirige alertas a un callback (para el dashboard)."""
from typing import Callable
from notifiers.base import BaseNotifier
from core.models import Alert


class CallbackNotifier(BaseNotifier):
    name = "callback"

    def __init__(self, on_alert: Callable[[Alert], None]):
        self.on_alert = on_alert

    def send(self, alert: Alert) -> bool:
        try:
            self.on_alert(alert)
            return True
        except Exception:
            return False

    def send_recovery(self, key: str, device_name: str,
                      sensor: str) -> bool:
        return True