"""analysis/aggregations.py — agregaciones ad-hoc y exportación CSV de una serie (sin LLM)."""
from __future__ import annotations

import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo

AGGS = ("mean", "min", "max", "count", "sum", "p95")
GROUPS = ("hour", "day", "week", "none")


def _valid(series, desde, hasta):
    return [r for r in series if r.value is not None and r.quality.value in ("VALID", "FROZEN")
            and desde <= r.event_time <= hasta]


def _agg(vals: list[float], agg: str) -> float:
    if agg == "mean":
        return sum(vals) / len(vals)
    if agg == "min":
        return min(vals)
    if agg == "max":
        return max(vals)
    if agg == "count":
        return float(len(vals))
    if agg == "sum":
        return sum(vals)
    v = sorted(vals)                                     # p95
    return v[int(0.95 * (len(v) - 1))]


def _group(t: datetime, group_by: str, tz) -> str:
    lt = t.astimezone(tz)
    if group_by == "hour":
        return lt.strftime("%Y-%m-%d %H:00")
    if group_by == "day":
        return lt.strftime("%Y-%m-%d")
    if group_by == "week":
        y, w, _ = lt.isocalendar()
        return f"{y}-W{w:02d}"
    return "total"


def aggregate(series, desde, hasta, agg: str = "mean", group_by: str = "day",
              tz_name: str = "America/Bogota") -> list[dict]:
    """agg ∈ mean|min|max|count|sum|p95 · group_by ∈ hour|day|week|none. La media es de lecturas (no ponderada en tiempo)."""
    if agg not in AGGS:
        raise ValueError(f"agg debe ser uno de {AGGS}")
    if group_by not in GROUPS:
        raise ValueError(f"group_by debe ser uno de {GROUPS}")
    tz = ZoneInfo(tz_name)
    groups: dict[str, list[float]] = {}
    for r in _valid(series, desde, hasta):
        groups.setdefault(_group(r.event_time, group_by, tz), []).append(float(r.value))
    return [{"grupo": g, "valor": round(_agg(v, agg), 3), "n": len(v)} for g, v in sorted(groups.items())]


def export_csv(series, desde, hasta, path: str, tz_name: str = "America/Bogota") -> int:
    """Escribe timestamp_local, timestamp_utc, valor, calidad. Devuelve el número de filas. UTF-8 con BOM (Excel)."""
    tz = ZoneInfo(tz_name)
    rows = [r for r in series if desde <= r.event_time <= hasta]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp_local", "timestamp_utc", "valor", "calidad"])
        for r in rows:
            w.writerow([r.event_time.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
                        r.event_time.isoformat(), "" if r.value is None else r.value, r.quality.value])
    return len(rows)
