"""
analysis/load_report.py — informe de ingreso de producto.

Es lo más parecido al ejemplo del ingeniero ("la res entra a 38 °C y después de
X tiempo llega a 4 °C"): para un producto registrado, qué hizo la cámara desde
que entró, cuánto tardó el aire en volver al límite, cuánto estuvo expuesto el
producto sobre el límite y cuánto trabajó el compresor.

Con la termocupla del aire NO se puede saber cuándo el producto mismo llegó a
4 °C (en simulación el producto tardó ~9 h mientras el aire estuvo ~2 h sobre el
límite). El informe lo dice; para ese dato hace falta una sonda en el producto.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from analysis.cooling_report import (
    CoolingConfig, _Fmt, _dur, _good, _n, _state_fraction, find_excursions)
from analysis.product_log import ICON, Load, assess_loads, render_load_line


def _render_load_detail(load: Load, temp, refr, cfg: CoolingConfig, now: datetime,
                        door=None, defrost=None) -> str:
    f = _Fmt(cfg.tz_name)
    end = load.t_out or now
    from analysis.forecast import despike
    rs = despike(temp)
    L = cfg.limit_c
    lines = [f"INFORME DE INGRESO DE PRODUCTO — {load.label()}"]
    head = f"Entró a las {f.hm(load.t_in)}"
    if load.entry_temp_c is not None:
        head += f" a {_n(load.entry_temp_c)} °C"
    head += (f" y salió a las {f.hm(load.t_out)} (estuvo {_dur((load.t_out - load.t_in).total_seconds() / 60.0)})."
             if load.t_out else f"; lleva {_dur((end - load.t_in).total_seconds() / 60.0)} en la cámara.")
    lines.append(head)

    before = [r for r in rs if r.event_time <= load.t_in]
    after = [r for r in rs if load.t_in <= r.event_time <= end]
    if not after:
        lines.append("No hay lecturas de la termocupla desde que entró el producto.")
        return "\n".join(lines)
    t0 = float(before[-1].value) if before else float(after[0].value)
    peak = max(after, key=lambda r: float(r.value))
    pk = float(peak.value)
    if pk - t0 < 0.5:
        lines.append(f"La cámara casi no se alteró: estaba a {_n(t0)} °C y llegó como máximo a {_n(pk)} °C.")
    else:
        lines.append(f"Al entrar, la cámara estaba a {_n(t0)} °C; subió hasta {_n(pk)} °C a las "
                     f"{f.hm(peak.event_time)} (+{_n(pk - t0)} °C).")
    if pk <= L:
        lines.append(f"El aire de la cámara nunca superó {_n(L, 0)} °C desde que entró.")
    else:
        back = find_excursions(temp, cfg, door, defrost, refr)
        ex = next((e for e in back if e.start <= peak.event_time <= (e.end or e.last_t)), None)
        if ex is not None and ex.recovered:
            lines.append(f"El aire volvió a {_n(L, 0)} °C a las {f.hm(ex.end)}: "
                         f"{_dur((ex.end - load.t_in).total_seconds() / 60.0)} después de entrar el producto "
                         f"({_dur(ex.above_min)} en total sobre el límite).")
        else:
            lines.append(f"El aire aún no vuelve a {_n(L, 0)} °C "
                         f"(lleva {_dur((end - load.t_in).total_seconds() / 60.0)} desde la entrada).")

    for r in assess_loads([load], temp, L, end, cfg.product_rules):
        lines.append(f"{ICON[r.level]} " + render_load_line(r, L, cfg.tz_name))
        if r.max_minutes is not None:
            lines.append("Cumplimiento del plazo: " + ("✅ dentro del máximo configurado." if r.minutes_above <= r.max_minutes
                                                         else "❌ superó el máximo configurado."))
    if refr:
        d_in = _state_fraction(refr, load.t_in, end)
        d_pre = _state_fraction(refr, load.t_in - timedelta(minutes=120), load.t_in - timedelta(minutes=5))
        if d_in is not None:
            t = f"Compresor: pidió frío el {d_in:.0%} del tiempo desde que entró el producto"
            if d_pre is not None:
                t += f" (en las 2 h previas: {d_pre:.0%})"
            lines.append(t + ".")
    if load.entry_temp_c is not None:
        lines.append(f"Ojo: el producto entró a {_n(load.entry_temp_c)} °C, pero la termocupla mide el aire, no el "
                     f"producto: con ella no se puede saber cuándo el producto mismo llegó a {_n(L, 0)} °C. "
                     "Para eso hace falta una sonda dentro del producto.")
    else:
        lines.append("Nota: es la temperatura del aire de la cámara (termocupla), no la del producto.")
    return "\n".join(lines)


def render_load_report(load: Load, temp, refr, cfg: CoolingConfig, now: datetime,
                       door=None, defrost=None, detail: bool = False) -> str:
    """Informe de ingreso de un producto, medido contra SU techo. Por defecto 5 líneas."""
    if detail:
        return _render_load_detail(load, temp, refr, cfg, now, door, defrost)
    from dataclasses import replace
    from analysis.forecast import despike
    f = _Fmt(cfg.tz_name)
    end = load.t_out or now
    risk = assess_loads([load], temp, cfg.limit_c, end, cfg.product_rules)[0]
    L = risk.limit_c
    rs = despike(temp)
    before = [r for r in rs if r.event_time <= load.t_in]
    after = [r for r in rs if load.t_in <= r.event_time <= end]
    perm = _dur((end - load.t_in).total_seconds() / 60.0)
    head = f"Entró {load.t_in.astimezone(f.tz).strftime('%d/%m %H:%M')}"
    if load.entry_temp_c is not None:
        head += f" a {_n(load.entry_temp_c)} °C"
    head += f"; " + (f"estuvo {perm}." if load.t_out else f"lleva {perm}.")
    out = [f"INFORME DE INGRESO — {load.label()}", head]
    if not after:
        out.append("No hay lecturas de la termocupla desde que entró.")
        return "\n".join(out)
    t0 = float(before[-1].value) if before else float(after[0].value)
    peak = max(after, key=lambda r: float(r.value))
    pk = float(peak.value)
    if pk <= L:
        air = f"Aire: {_n(t0)} → máx. {_n(pk)} °C; nunca superó {_n(L, 0)} °C."
    else:
        ex = next((e for e in find_excursions(temp, replace(cfg, limit_c=L), door, defrost, refr)
                   if e.start <= peak.event_time <= (e.end or e.last_t)), None)
        if ex is not None and ex.recovered:
            air = (f"Aire: {_n(t0)} → máx. {_n(pk)} °C ({f.hm(peak.event_time)}); volvió a ≤{_n(L, 0)} °C a las "
                   f"{f.hm(ex.end)}, {_dur((ex.end - load.t_in).total_seconds() / 60.0)} tras el ingreso.")
        else:
            air = f"Aire: {_n(t0)} → máx. {_n(pk)} °C; aún sobre {_n(L, 0)} °C."
    out.append(air)
    line = f"{ICON[risk.level]} " + render_load_line(risk, L, cfg.tz_name)
    if risk.max_minutes is not None:
        line += " ✅" if risk.minutes_above <= risk.max_minutes else " ❌"
    out.append(line)
    if refr:
        d_in = _state_fraction(refr, load.t_in, end)
        d_pre = _state_fraction(refr, load.t_in - timedelta(minutes=120), load.t_in - timedelta(minutes=5))
        if d_in is not None:
            out.append(f"Compresor {d_in:.0%} desde el ingreso" + (f" (antes: {d_pre:.0%})." if d_pre is not None else "."))
    if load.entry_temp_c is not None:
        out.append(f"Solo mido el aire: no sé cuándo el producto llegó a {_n(L, 0)} °C (haría falta una sonda en él).")
    else:
        out.append("Solo mido el aire de la cámara, no el producto.")
    return "\n".join(out)
