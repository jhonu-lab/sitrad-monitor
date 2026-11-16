"""
analysis/controller_summary.py — respuestas cortas sobre lo que reporta el controlador:
puerta, compresor, deshielo, setpoint y producto registrado.

Todo se calcula en código a partir de las series (sin LLM). Pensado para las
preguntas típicas del ingeniero: "¿cuántas veces se abrió la puerta?", "¿cuánto
trabaja el compresor?", "¿cada cuánto hace deshielo?", "¿cuál es el setpoint?",
"¿hay producto en la cava?".
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from analysis.cooling_report import (
    CoolingConfig, _Fmt, _dur, _good, _n, _on_intervals, _state_fraction,
    door_activity, duty_blocks, setpoint_changes)
from analysis.product_log import ICON, assess_loads, render_load_line
from analysis.rise_watch import _open_minutes


def _span(minutes: float) -> str:
    return _dur(minutes)


def door_summary(door, now: datetime, minutes: float, cfg: CoolingConfig) -> str:
    if not _good(door or []):
        return ("No tengo lecturas del sensor de puerta (puede estar deshabilitado en el controlador). "
                "Habilítalo en el menú del TC-900E para que pueda contar aperturas.")
    f = _Fmt(cfg.tz_name)
    t0 = now - timedelta(minutes=minutes)
    n, tot, longest = door_activity(door, t0, now)
    om = _open_minutes(door, now)
    ahora = f"Ahora: abierta hace {_dur(om)}." if om > 0 else "Ahora: cerrada."
    if n == 0:
        return f"Puerta (últimas {_span(minutes)}): no se registraron aperturas. {ahora}"
    last = [a for a, _ in _on_intervals(door, t0, now)][-1]
    return (f"Puerta (últimas {_span(minutes)}): {n} apertura(s), {_dur(tot)} abierta en total "
            f"(la más larga, {_dur(longest)}); la última empezó a las {f.hm(last)}. {ahora}")


def compressor_summary(refr, now: datetime, minutes: float, cfg: CoolingConfig) -> str:
    if not _good(refr or []):
        return "No tengo lecturas de la orden de refrigeración (REFR)."
    t0 = now - timedelta(minutes=minutes)
    duty = _state_fraction(refr, t0, now)
    if duty is None:
        return "No hay datos suficientes del compresor en ese periodo."
    txt = f"Compresor (últimas {_span(minutes)}): el controlador pidió frío el {duty:.0%} del tiempo"
    blocks = duty_blocks(refr, t0, now)
    if len(blocks) >= 3:
        txt += " (por bloques de 4 h: " + ", ".join(f"{b:.0%}" for b in blocks) + ")"
    txt += "."
    on_runs, off_runs, start, on = [], [], None, None
    for r in sorted(_good(refr), key=lambda r: r.event_time):
        if r.event_time < t0:
            continue
        v = float(r.value) >= 0.5
        if on is None:
            start, on = r.event_time, v
        elif v != on:
            (on_runs if on else off_runs).append((r.event_time - start).total_seconds() / 60.0)
            start, on = r.event_time, v
    on_runs, off_runs = [m for m in on_runs if m >= 2], [m for m in off_runs if m >= 2]
    if len(on_runs) >= 3 and len(off_runs) >= 3:
        txt += (f" Ciclo típico: ~{_dur(median(on_runs))} encendido y ~{_dur(median(off_runs))} en reposo "
                f"({len(on_runs)} arranques).")
    return txt


def defrost_summary(defrost, now: datetime, minutes: float, cfg: CoolingConfig) -> str:
    if not _good(defrost or []):
        return "No tengo lecturas del estado de deshielo."
    f = _Fmt(cfg.tz_name)
    t0 = now - timedelta(minutes=minutes)
    iv = _on_intervals(defrost, t0, now)
    if not iv:
        return f"Deshielo (últimas {_span(minutes)}): no se registraron deshielos."
    durs = [(b - a).total_seconds() / 60.0 for a, b in iv]
    txt = (f"Deshielo (últimas {_span(minutes)}): {len(iv)} ciclo(s), duración típica {_dur(median(durs))}; "
           f"el último empezó a las {f.hm(iv[-1][0])} y duró {_dur(durs[-1])}.")
    if len(iv) >= 3:
        gaps = [(iv[i + 1][0] - iv[i][0]).total_seconds() / 3600.0 for i in range(len(iv) - 1)]
        txt += f" Se repiten cada ~{_n(median(gaps))} h."
    return txt


def setpoint_summary(setpoint, temp, now: datetime, minutes: float, cfg: CoolingConfig) -> str:
    sp = [float(r.value) for r in _good(setpoint or [])]
    if not sp:
        return "No tengo lecturas del setpoint."
    f = _Fmt(cfg.tz_name)
    cur = sum(sp[-3:]) / len(sp[-3:])
    t0 = now - timedelta(minutes=minutes)
    txt = f"Setpoint actual: {_n(cur)} °C."
    for tt, a, b in setpoint_changes(setpoint, t0, now):
        txt += f" Cambió de {_n(a)} a {_n(b)} °C a las {f.hm(tt)}."
    tv = [float(r.value) for r in _good(temp or []) if r.event_time >= now - timedelta(hours=3)]
    if len(tv) >= 10:
        txt += f" En las últimas 3 h la cámara osciló entre {_n(min(tv))} y {_n(max(tv))} °C."
        if max(tv) > cfg.limit_c:
            txt += f" ⚠️ Eso supera el límite de {_n(cfg.limit_c, 0)} °C."
    if cur >= cfg.limit_c - 1.5:
        txt += (f" ⚠️ Con este setpoint la cámara regula cerca o por encima del límite de {_n(cfg.limit_c, 0)} °C "
                "(el compresor arranca por encima del setpoint): conviene confirmar que es intencional.")
    return txt


def loads_summary(loads, temp, now: datetime, cfg: CoolingConfig) -> str:
    risks = assess_loads(loads or [], temp, cfg.limit_c, now, cfg.product_rules)
    if not risks:
        return ("No tengo producto registrado en la cámara. El sistema no puede saber qué hay dentro a partir "
                "de la temperatura: solo conoce lo que el operario registra. Puedes decirme, por ejemplo: "
                "\"hay 500 kg de ternera desde hace 20 minutos\".")
    lines = ["Producto registrado en la cámara:"]
    lines += [f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name) for r in risks]
    return "\n".join(lines)
