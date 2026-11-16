"""
analysis/status_report.py — "¿cómo está la cava ahora?" en lenguaje de ingeniero.

Responde en pocas líneas lo que se pregunta al llegar a la cava: temperatura,
si está dentro del límite, qué hace el compresor, puerta, deshielo, producto
registrado y una conclusión con semáforo. Nada de nombres de canal ni de
hipótesis técnicas: eso queda para quien lo pida explícitamente.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from analysis.cooling_report import (
    LABEL, CoolingConfig, _Fmt, _cause_line, _dur, _good, _n, causa_corta, classify, find_excursions)
from analysis.forecast import fit_trend
from analysis.product_log import ICON, assess_loads, render_load_line
from analysis.rise_watch import _open_minutes, usual_max


def _run(series, now: datetime):
    """(estado actual, minutos que lleva en ese estado) o (None, 0)."""
    rs = [r for r in sorted(_good(series or []), key=lambda r: r.event_time) if r.event_time <= now]
    if not rs:
        return None, 0.0
    cur = float(rs[-1].value) >= 0.5
    start = rs[-1].event_time
    for r in reversed(rs):
        if (float(r.value) >= 0.5) != cur:
            break
        start = r.event_time
    return cur, (now - start).total_seconds() / 60.0


def _typical_on_run(series, now: datetime) -> float | None:
    """Duración mediana (min) de los ciclos completos de compresor encendido; None si hay pocos."""
    rs = [r for r in sorted(_good(series or []), key=lambda r: r.event_time) if r.event_time <= now]
    runs, start, on = [], None, False
    for r in rs:
        v = float(r.value) >= 0.5
        if v and not on:
            start, on = r.event_time, True
        elif not v and on:
            runs.append((r.event_time - start).total_seconds() / 60.0)
            on = False
    runs = [m for m in runs if m >= 2.0]
    if len(runs) < 3:
        return None
    runs.sort()
    return runs[len(runs) // 2]


def _pct_in_range(rs, limit_c: float, since: datetime) -> float | None:
    tot = ok = 0.0
    for a, b in zip(rs, rs[1:]):
        if b.event_time <= since:
            continue
        dt = min((b.event_time - max(a.event_time, since)).total_seconds() / 60.0, 15.0)
        if dt <= 0:
            continue
        tot += dt
        if float(a.value) <= limit_c:
            ok += dt
    return 100.0 * ok / tot if tot else None


def _render_status_detail(temp, door, defrost, refr, setpoint, cfg: CoolingConfig, now: datetime,
                          loads=None, brief: bool = False) -> str:
    f = _Fmt(cfg.tz_name)
    name = cfg.camera_name
    good = sorted(_good(temp), key=lambda r: r.event_time)
    if not good:
        return f"No tengo lecturas de la termocupla de {name}."
    last = good[-1]
    stale = (now - last.event_time).total_seconds() / 60.0
    if stale >= 10.0:
        return (f"🟠 No llegan lecturas de {name} desde las {f.hm(last.event_time)} (hace {_dur(stale)}). "
                f"La última temperatura fue {_n(float(last.value))} °C.")
    L = cfg.limit_c
    t_now = float(last.value)
    sp_vals = [float(r.value) for r in _good(setpoint or [])[-3:]]
    sp = sum(sp_vals) / len(sp_vals) if sp_vals else None
    trend = fit_trend(temp, window_min=cfg.trend_window_min, now=now)
    usual = usual_max(temp, L)

    head = f"Temperatura de {name}: {_n(t_now)} °C"
    head += " — dentro del límite" if t_now <= L else " — SOBRE el límite"
    head += f" de {_n(L, 0)} °C"
    if sp is not None:
        head += f" (setpoint {_n(sp)} °C)"
    head += "."
    if trend and trend.r2 >= 0.5 and abs(trend.slope_c_h) >= 0.5:
        head += (f" Está {'subiendo' if trend.slope_c_h > 0 else 'bajando'} "
                 f"{_n(abs(trend.slope_c_h))} °C/h en los últimos {_dur(trend.window_min)}.")
    if brief:
        return head

    lines = [f"ESTADO DE {name.upper()} — {f.dh(now)}", head]
    if sp is not None and sp >= L - 1.5:
        lines.append(f"⚠️ Setpoint en {_n(sp)} °C: con el compresor arrancando por encima del setpoint, la cámara "
                     f"regula cerca o por encima del límite de {_n(L, 0)} °C. Conviene confirmar que es intencional.")
    on, mins = _run(refr, now)
    typ = _typical_on_run(refr, now)
    long_run = bool(on) and mins >= max(30.0, 2.0 * (typ or 0.0))
    if on is not None:
        txt = ("Compresor: " + (f"pidiendo frío desde hace {_dur(mins)}" if on
                               else f"sin pedir frío desde hace {_dur(mins)} (reposo normal del ciclo)"))
        if long_run and typ:
            txt += f" (sus ciclos habituales duran ~{_dur(typ)})"
        lines.append(txt + ".")
    om = _open_minutes(door, now)
    if door and _good(door):
        lines.append(f"Puerta: abierta hace {_dur(om)}." if om > 0 else "Puerta: cerrada.")
    don, dmins = _run(defrost, now)
    if don is not None:
        lines.append(f"Deshielo: en curso desde hace {_dur(dmins)}." if don
                     else f"Deshielo: último terminó hace {_dur(dmins)}.")

    day = _pct_in_range(good, L, now - timedelta(hours=24))
    exs = find_excursions(temp, cfg, door, defrost, refr, loads, setpoint)
    hist = []
    if day is not None:
        hist.append(f"en ≤ {_n(L, 0)} °C el {day:.0f}% del tiempo en los datos disponibles")
    hist.append(f"{len(exs)} episodio(s) sobre el límite")
    closed = [e for e in exs if e.recovered]
    if closed:
        e = closed[-1]
        hist.append(f"el último a las {f.hm(e.start)} ({LABEL.get(e.kind or classify(e, cfg), 'sin causa visible')})")
    lines.append("Historial: " + "; ".join(hist) + ".")

    risks = assess_loads(loads or [], temp, L, now, cfg.product_rules)
    if risks:
        for r in risks:
            lines.append(f"{ICON[r.level]} " + render_load_line(r, L, cfg.tz_name))
    else:
        lines.append("Producto: no hay producto registrado (puedes decirme, por ejemplo: "
                     "\"hay 500 kg de ternera desde hace 20 minutos\").")

    # conclusión con semáforo
    ongoing = [e for e in exs if not e.recovered]
    if ongoing:
        e = ongoing[-1]
        lines.append(f"🟠 Sobre el límite desde las {f.hm(e.start)} (lleva {_dur(e.above_min)}, "
                     f"máximo {_n(e.peak_c)} °C). {_cause_line(e, cfg)}")
    elif (trend and trend.slope_c_h >= cfg.warn_slope_c_h and trend.r2 >= cfg.warn_r2
          and usual is not None and t_now >= usual + 0.3 and (on or on is None)):
        m = trend.minutes_to(L)
        lines.append("🟡 Vigilar: la temperatura sube más rápido y más alto de lo que esta cámara suele hacer"
                     + (f"; si sigue así llegaría a {_n(L, 0)} °C en unos {_dur(m)}." if m else "."))
    elif any(r.level in ("riesgo", "critico") for r in risks):
        lines.append("🟠 Hay producto con tiempo sobre el límite cercano al máximo configurado (ver arriba).")
    elif long_run:
        recent = any((now - l.t_in).total_seconds() <= 120 * 60 for l in (loads or []))
        lines.append(f"🟡 Vigilar: el compresor lleva {_dur(mins)} trabajando sin parar, más de lo habitual. "
                     + ("Con producto tibio recién ingresado es esperable; " if recent else
                        "No hay producto ni puerta que lo explique; ")
                     + "si la temperatura empieza a subir, conviene revisar la cava.")
    else:
        lines.append("🟢 Sin novedades: dentro del límite y comportándose como suele.")
    lines.append("Nota: es la temperatura del aire de la cámara (termocupla), no la del producto.")
    return "\n".join(lines)


def render_status(temp, door, defrost, refr, setpoint, cfg: CoolingConfig, now: datetime,
                  loads=None, brief: bool = False, detail: bool = False, limit_who: str | None = None) -> str:
    """Estado actual. Por defecto 3-4 líneas: semáforo, estado del equipo y producto. detail=True: versión larga."""
    if detail:
        return _render_status_detail(temp, door, defrost, refr, setpoint, cfg, now, loads, brief=False)
    f = _Fmt(cfg.tz_name)
    name = cfg.camera_name[:1].upper() + cfg.camera_name[1:]
    good = sorted(_good(temp), key=lambda r: r.event_time)
    if not good:
        return f"No tengo lecturas de la termocupla de {cfg.camera_name}."
    last = good[-1]
    stale = (now - last.event_time).total_seconds() / 60.0
    if stale >= 10.0:
        return (f"🟠 Sin lecturas de {cfg.camera_name} desde las {f.hm(last.event_time)} (hace {_dur(stale)}). "
                f"Última temperatura: {_n(float(last.value))} °C.")
    L = cfg.limit_c
    t_now = float(last.value)
    trend = fit_trend(temp, window_min=cfg.trend_window_min, now=now)
    usual = usual_max(temp, L)
    on, mins = _run(refr, now)
    typ = _typical_on_run(refr, now)
    long_run = bool(on) and mins >= max(30.0, 2.0 * (typ or 0.0))
    risks = assess_loads(loads or [], temp, L, now, cfg.product_rules)
    exs = find_excursions(temp, cfg, door, defrost, refr, loads, setpoint)
    ongoing = [e for e in exs if not e.recovered and e.last_c is not None and e.last_c > L]

    lim = f"límite {_n(L, 0)} °C" + (f" por {limit_who}" if limit_who else "")
    if brief:
        return f"Temperatura de {cfg.camera_name}: {_n(t_now)} °C ({lim})."
    sp_vals = [float(r.value) for r in _good(setpoint or [])[-3:]]
    sp = sum(sp_vals) / len(sp_vals) if sp_vals else None

    if ongoing:
        e = ongoing[-1]
        icon = "🟠"
        concl = f"sobre {_n(L, 0)} °C desde las {f.hm(e.start)} ({causa_corta(e, cfg)})"
    elif (trend and trend.slope_c_h >= cfg.warn_slope_c_h and trend.r2 >= cfg.warn_r2
          and usual is not None and t_now >= usual + 0.3 and (on or on is None)):
        m = trend.minutes_to(L)
        icon = "🟡"
        concl = f"sube rápido (+{_n(trend.slope_c_h)} °C/h)" + (f"; llegaría a {_n(L, 0)} °C en ~{_dur(m)}" if m else "")
    elif any(r.level in ("riesgo", "critico") for r in risks):
        icon, concl = "🟠", "producto cerca de su máximo permitido"
    elif long_run:
        icon = "🟡"
        concl = f"compresor {_dur(mins)} sin parar, más de lo habitual"
    else:
        icon, concl = "🟢", "sin novedades"
    lines = [f"{icon} {name}: {_n(t_now)} °C ({lim}) — {concl}"]
    eq = []
    if on is not None:
        eq.append(f"compresor {'pidiendo frío' if on else 'en reposo'} hace {_dur(mins)}")
    if door and _good(door):
        om = _open_minutes(door, now)
        eq.append(f"puerta abierta hace {_dur(om)}" if om > 0 else "puerta cerrada")
    don, dmins = _run(defrost, now)
    if don is not None:
        eq.append(f"deshielo en curso hace {_dur(dmins)}" if don else f"deshielo hace {_dur(dmins)}")
    if eq:
        lines.append(" · ".join(eq))
    if sp is not None and sp >= L - 1.5:
        lines.append(f"⚠️ Setpoint en {_n(sp)} °C: la cámara regula cerca o sobre el límite.")
    if risks:
        lines += [f"{ICON[r.level]} " + render_load_line(r, L, cfg.tz_name) for r in risks]
    else:
        lines.append("Sin producto registrado.")
    return "\n".join(lines)
