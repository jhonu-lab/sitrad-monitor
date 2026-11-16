"""
analysis/forecast.py — hacia dónde va la temperatura (solo termocupla).

Ajusta una recta a los últimos minutos y calcula en cuánto tiempo llegaría a
una temperatura dada SI SIGUE A ESE RITMO. No es un pronóstico: una cámara con
compresor sano casi siempre frena la subida. Por eso solo se proyecta cuando la
tendencia es clara (ajuste bueno, varios puntos, subida sostenida) y el texto lo
dice como "si sigue así".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

GOOD = ("VALID", "FROZEN")


@dataclass(frozen=True)
class Trend:
    slope_c_h: float          # °C por hora
    r2: float                 # calidad del ajuste (0..1)
    n: int
    window_min: float
    temp_now: float           # valor ajustado en el último instante
    rise_c: float             # cambio total ajustado en la ventana
    accel_c_h2: float | None  # curvatura: <0 = la subida se está frenando
    t_last: datetime

    def minutes_to(self, target_c: float) -> float | None:
        """Minutos hasta target SI SIGUE a este ritmo (None si no sube o ya lo superó)."""
        if self.slope_c_h <= 0 or self.temp_now >= target_c:
            return None
        return (target_c - self.temp_now) / self.slope_c_h * 60.0


def despike(readings, jump_c: float = 3.0, neighbor_tol: float = 1.5):
    """
    Quita lecturas aisladas (1 o 2 muestras seguidas) que se salen más de `jump_c` °C de AMBOS
    vecinos cuando los vecinos coinciden entre sí: son picos del sensor (falso contacto), no
    calentamiento real. Una subida real dura más de 2 muestras o no vuelve al valor previo.
    """
    rs = sorted((r for r in readings if r.value is not None and r.quality.value in GOOD),
                key=lambda r: r.event_time)
    drop: set[int] = set()
    n = len(rs)
    for i in range(1, n - 1):
        for k in (1, 2):
            j = i + k - 1                       # bloque rs[i..j]
            if j >= n - 1:
                continue
            a, b = float(rs[i - 1].value), float(rs[j + 1].value)
            if abs(a - b) > neighbor_tol:
                continue
            if all(abs(float(rs[m].value) - a) > jump_c and abs(float(rs[m].value) - b) > jump_c
                   for m in range(i, j + 1)):
                drop.update(range(i, j + 1))
    return [r for m, r in enumerate(rs) if m not in drop]


def _ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return a, b, r2


def fit_trend(readings, window_min: float = 20.0, min_points: int = 6,
              now: datetime | None = None) -> Trend | None:
    rs = despike(readings)
    if len(rs) < min_points:
        return None
    t_last = rs[-1].event_time
    if now is not None and (now - t_last).total_seconds() > 600:
        return None                      # datos viejos: no se proyecta
    w = [r for r in rs if r.event_time >= t_last - timedelta(minutes=window_min)]
    if len(w) < min_points:
        return None
    t0 = w[0].event_time
    xs = [(r.event_time - t0).total_seconds() / 3600.0 for r in w]      # horas
    ys = [float(r.value) for r in w]
    fit = _ols(xs, ys)
    if fit is None:
        return None
    a, b, r2 = fit
    # curvatura: pendiente de la 2.ª mitad menos la de la 1.ª, por hora
    accel = None
    h = len(w) // 2
    if h >= 3:
        f1, f2 = _ols(xs[:h], ys[:h]), _ols(xs[h:], ys[h:])
        if f1 and f2:
            dt = (sum(xs[h:]) / len(xs[h:])) - (sum(xs[:h]) / h)
            accel = (f2[1] - f1[1]) / dt if dt > 0 else None
    return Trend(slope_c_h=b, r2=max(0.0, r2), n=len(w), window_min=window_min,
                 temp_now=a + b * xs[-1], rise_c=b * (xs[-1] - xs[0]),
                 accel_c_h2=accel, t_last=t_last)
