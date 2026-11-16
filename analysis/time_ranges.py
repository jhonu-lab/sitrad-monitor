"""analysis/time_ranges.py — fechas relativas en español ("ayer", "la semana pasada") -> rango UTC."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def parse_time_range(q: str, now: datetime, tz_name: str = "America/Bogota"):
    """`q` ya normalizado (minúsculas, sin acentos). Devuelve (desde_utc, hasta_utc, etiqueta) o None.

    Reconoce: anteayer, ayer, hoy, esta semana, la semana pasada, la ultima semana / ultimos 7 dias.
    Los días son calendario LOCAL (tz_name); `hasta` nunca pasa de `now`.
    """
    tz = ZoneInfo(tz_name)
    loc = now.astimezone(tz)
    day0 = datetime.combine(loc.date(), time.min, tzinfo=tz)

    def utc(a, b, label):
        return a.astimezone(timezone.utc), min(b, loc).astimezone(timezone.utc), label

    if re.search(r"\banteayer\b", q):
        return utc(day0 - timedelta(days=2), day0 - timedelta(days=1), "anteayer")
    if re.search(r"\bayer\b", q):
        return utc(day0 - timedelta(days=1), day0, "ayer")
    if re.search(r"\bsemana pasada\b", q):
        mon = day0 - timedelta(days=loc.weekday())
        return utc(mon - timedelta(days=7), mon, "la semana pasada")
    if re.search(r"\b(esta semana)\b", q):
        return utc(day0 - timedelta(days=loc.weekday()), loc, "esta semana")
    if re.search(r"\b(ultima semana|ultimos 7 dias|ultimos siete dias)\b", q):
        return utc(loc - timedelta(days=7), loc, "los últimos 7 días")
    if re.search(r"\bhoy\b", q):
        return utc(day0, loc, "hoy")
    return None
