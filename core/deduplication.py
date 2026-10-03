"""Deduplicación con cooldown + histéresis temporal."""
import logging
from datetime import datetime, timedelta
from typing import Dict, Set

logger = logging.getLogger(__name__)


class DeduplicationManager:
    """
    Rastrea alertas activas y evita spam.

    Estrategia:
      1. Histéresis temporal: requiere N detecciones consecutivas.
      2. Cooldown: no reenvía la misma alerta dentro de la ventana.
      3. Resolución: cuando la condición desaparece, la alerta se marca
         como resuelta y se permite un nuevo envío si vuelve a ocurrir.
    """

    def __init__(self, cooldown_seconds: int, required_consecutive: int):
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.required = required_consecutive
        self.last_sent: Dict[str, datetime] = {}
        self.consecutive: Dict[str, int] = {}
        self.active: Set[str] = set()

    def should_send(self, key: str) -> bool:
        """Retorna True si la alerta debe enviarse."""
        # 1. Histéresis temporal: requiere N detecciones seguidas
        self.consecutive[key] = self.consecutive.get(key, 0) + 1
        if self.consecutive[key] < self.required:
            return False

        # 2. Cooldown: no reenviar dentro de la ventana
        now = datetime.now()
        if key in self.last_sent:
            if now - self.last_sent[key] < self.cooldown:
                # Ya está activa y dentro de cooldown
                self.active.add(key)
                return False

        # 3. Pasa filtros: enviar y registrar
        self.last_sent[key] = now
        self.active.add(key)
        return True

    def mark_resolved(self, key: str) -> bool:
        """Marca una alerta como resuelta. Retorna True si estaba activa."""
        self.consecutive.pop(key, None)
        if key in self.active:
            self.active.discard(key)
            self.last_sent.pop(key, None)
            return True
        return False

    def refresh_absent(self, current_keys: Set[str]) -> Set[str]:
        """
        Limpia contadores de claves que ya no aparecen y devuelve las claves
        que estaban activas y ahora no lo están (candidatas a recuperación).
        """
        # Resetear contadores de histéresis para claves ausentes
        for key in list(self.consecutive.keys()):
            if key not in current_keys:
                self.consecutive.pop(key, None)

        # Detectar recuperaciones
        absent = {k for k in self.active if k not in current_keys}
        return absent