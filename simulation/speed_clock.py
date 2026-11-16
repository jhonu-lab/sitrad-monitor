from __future__ import annotations
# simulation.speed_clock
import time
from datetime import datetime, timedelta, timezone

from core.clock import Clock


class SpeedClock(Clock):
    """
    Reloj que avanza 'speed' veces más rápido que el real.

    Con speed=4, un minuto real equivale a 4 minutos simulados. Úsalo
    solo con fuentes sintéticas; reduce la espera entre episodios.
    Importante: si el LLM tarda 20 s en responder, con speed=4 el
    sistema pierde ~80 s simulados de muestreo. Usa speed bajo (2-4).
    """

    def __init__(self, speed: float = 1.0, start: datetime | None = None) -> None:
        if speed <= 0:
            raise ValueError("speed debe ser mayor que cero.")
        self.speed = float(speed)
        self._start = start or datetime.now(timezone.utc)
        self._real0 = time.monotonic()

    def now(self) -> datetime:
        elapsed = (time.monotonic() - self._real0) * self.speed
        return self._start + timedelta(seconds=elapsed)
