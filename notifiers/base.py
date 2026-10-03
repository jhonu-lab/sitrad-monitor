"""
Interfaz común para todos los notificadores.
"""
from abc import ABC, abstractmethod
from core.models import Alert


class BaseNotifier(ABC):
    """Contrato mínimo de un notificador."""

    name: str = "base"

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """Envía una alerta. Retorna True si fue exitoso."""
        raise NotImplementedError

    def send_recovery(self, key: str, device_name: str,
                      sensor: str) -> bool:
        """Envía notificación de recuperación. Default: no-op."""
        return False

    def close(self) -> None:
        """Hook de limpieza al detener el worker."""
        pass