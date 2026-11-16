from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

# core.clock

class Clock:
    """Fuente de tiempo real del sistema."""

    def now(self) -> datetime:
        """Devuelve la hora actual en UTC."""
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    """Reloj controlable manualmente para tests y ReplaySource."""

    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        """Devuelve el instante actualmente congelado."""
        return self._t

    def set(self, t: datetime) -> None:
        """Establece manualmente el instante actual."""
        self._t = t

    def advance(self, dt: timedelta) -> None:
        """Avanza el reloj el intervalo indicado."""
        self._t += dt

@lru_cache(maxsize=None)
def local_tz(name: str = "America/Bogota") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/Bogota")