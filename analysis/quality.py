"""analysis/quality.py — calidad de datos por canal en una ventana (cobertura, huecos, antigüedad)."""
from __future__ import annotations

from datetime import datetime
from statistics import median

GOOD = ("VALID", "FROZEN")


def channel_quality(readings, t0: datetime, t1: datetime, now: datetime | None = None,
                    gap_min: float = 10.0) -> dict:
    """Cobertura = tiempo cubierto por lecturas buenas / ventana [t0, t1]. Una lectura cubre hasta la siguiente,
    con tope de 3× la cadencia mediana (mínimo 2 min): más allá es hueco."""
    rs = sorted((r for r in readings if t0 <= r.event_time <= t1), key=lambda r: r.event_time)
    cnt = {"VALID": 0, "FROZEN": 0, "MISSING": 0, "ERR": 0, "OFFLINE": 0}
    for r in rs:
        cnt[r.quality.value] = cnt.get(r.quality.value, 0) + 1
    window = max((t1 - t0).total_seconds(), 1.0)
    out = {"n_valid": cnt["VALID"], "n_frozen": cnt["FROZEN"], "n_missing": cnt["MISSING"] + cnt["OFFLINE"],
           "n_err": cnt["ERR"], "cobertura_pct": 0.0, "antiguedad_min": None, "gap_max_min": None,
           "n_gaps_mayores_10min": 0, "estado": "sin lecturas"}
    if not rs:
        return out
    dts = [(b.event_time - a.event_time).total_seconds() for a, b in zip(rs, rs[1:]) if b.event_time > a.event_time]
    cap = max(3 * (median(dts) if dts else 120.0), 120.0)
    covered, gaps = 0.0, []
    good = [r for r in rs if r.quality.value in GOOD and r.value is not None]
    for a, b in zip(good, good[1:]):
        d = (b.event_time - a.event_time).total_seconds()
        covered += min(d, cap)
        if d > cap:
            gaps.append(d / 60.0)
    if good:
        covered += min(cap, (t1 - good[-1].event_time).total_seconds())
        gaps_all = [(good[0].event_time - t0).total_seconds() / 60.0] if (good[0].event_time - t0).total_seconds() > cap else []
        gaps += gaps_all
    out["cobertura_pct"] = round(min(100.0, 100.0 * covered / window), 1)
    ref = now or t1
    out["antiguedad_min"] = round((ref - rs[-1].event_time).total_seconds() / 60.0, 1)
    out["gap_max_min"] = round(max(gaps), 1) if gaps else 0.0
    out["n_gaps_mayores_10min"] = sum(1 for g in gaps if g > gap_min)
    if not good:
        out["estado"] = "sin datos válidos (canal deshabilitado o sin sensor)"
    else:
        out["estado"] = "ok" if out["cobertura_pct"] >= 80 else "con huecos"
    return out
