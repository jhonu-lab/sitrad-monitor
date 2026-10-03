"""Guarda cada alerta como línea JSON (formato JSON Lines)."""
import json
import os
from datetime import datetime
from notifiers.base import BaseNotifier
from core.models import Alert


class FileLogNotifier(BaseNotifier):
    name = "file_log"

    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path

    def send(self, alert: Alert) -> bool:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": alert.timestamp.isoformat(),
                "severity": alert.severity.value,
                "device_id": alert.device_id,
                "device": alert.device_name,
                "sensor": alert.sensor_name,
                "type": alert.alert_type,
                "value": alert.value_detected,
                "action": alert.suggested_action,
            }, ensure_ascii=False) + "\n")
        return True

    def send_recovery(self, key: str, device_name: str,
                      sensor: str) -> bool:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "severity": "RECUPERACIÓN",
                "key": key,
                "sensor": sensor,
            }, ensure_ascii=False) + "\n")
        return True