"""Notificador de consola con colores ANSI (útil para logs y demo)."""
from notifiers.base import BaseNotifier
from core.models import Alert, Severity


class ConsoleNotifier(BaseNotifier):
    name = "console"

    COLORS = {
        Severity.CRITICAL: "\033[91m",
        Severity.WARNING:  "\033[93m",
        Severity.RECOVERY: "\033[92m",
    }
    RESET = "\033[0m"

    def send(self, alert: Alert) -> bool:
        color = self.COLORS.get(alert.severity, "")
        border = "═" * 70
        print(f"\n{color}{border}")
        print(f"  {alert.format_message().replace(chr(10), chr(10) + '  ')}")
        print(f"{border}{self.RESET}\n")
        return True

    def send_recovery(self, key: str, device_name: str,
                      sensor: str) -> bool:
        print(f"\n\033[92m{'─' * 70}")
        print(f"  🟢 [RECUPERACIÓN] {sensor} volvió a la normalidad.")
        print(f"{'─' * 70}\033[0m\n")
        return True