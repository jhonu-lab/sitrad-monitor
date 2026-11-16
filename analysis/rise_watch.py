"""
analysis/rise_watch.py — avisos tempranos (antes de que sea tarde).

Tres vigilancias, todas con la termocupla y los estados del controlador:

  1. SUBIDA RÁPIDA: la recta de los últimos minutos indica que llegará al límite
     (o a un nivel crítico) pronto. Es una proyección "si sigue así", no un
     pronóstico: en simulación solo ~1 de cada 3 subidas así sigue hasta el final
     (con la puerta abierta casi nunca, porque al cerrarla se frena). Por eso el
     aviso es de VIGILANCIA y trae las causas posibles y qué mirar.
  2. PUERTA ABIERTA demasiado tiempo con la temperatura subiendo.
  3. PRODUCTO en la cámara (registrado con product_log): cuando el tiempo sobre el
     límite cruza el 50% y el 100% del máximo configurado para ese producto.

Las causas son hipótesis compatibles con los datos, ordenadas por evidencia.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from analysis.cooling_report import _Fmt, _dur, _good, _n, _state_fraction, setpoint_changes
from analysis.forecast import Trend, despike, fit_trend
from analysis.product_log import ICON, assess_loads, render_load_line


def _state_now(series, now: datetime, within_min: float = 0.0) -> bool:
    """¿El estado estuvo en 1 en la última lectura (o en los últimos `within_min` min)?"""
    rs = [r for r in _good(series or []) if r.event_time <= now]
    if not rs:
        return False
    if float(rs[-1].value) >= 0.5:
        return True
    return any(float(r.value) >= 0.5 for r in rs if (now - r.event_time).total_seconds() <= within_min * 60)


def _open_minutes(door, now: datetime) -> float:
    """Minutos que lleva abierta la puerta en esta apertura (0 si está cerrada)."""
    rs = [r for r in sorted(_good(door or []), key=lambda r: r.event_time) if r.event_time <= now]
    if not rs or float(rs[-1].value) < 0.5:
        return 0.0
    start = rs[-1].event_time
    for r in reversed(rs):
        if float(r.value) < 0.5:
            break
        start = r.event_time
    return (now - start).total_seconds() / 60.0


def usual_max(temp, limit_c: float, min_hours: float = 4.0) -> float | None:
    """Lo más alto que esta cámara suele llegar en operación normal: p95 de sus lecturas ≤ límite.
    None si hay poca historia (se usa entonces la regla de 'la subida se frena')."""
    rs = [r for r in _good(temp)]
    if len(rs) < 30:
        return None
    span_h = (max(r.event_time for r in rs) - min(r.event_time for r in rs)).total_seconds() / 3600.0
    if span_h < min_hours:
        return None
    vals = sorted(float(r.value) for r in rs if float(r.value) <= limit_c)
    if len(vals) < 30:
        return None
    return vals[int(0.95 * (len(vals) - 1))]


def possible_causes(trend: Trend, door, defrost, refr, loads, now: datetime, tz_name: str,
                    setpoint=None, limit_c: float = 4.0) -> list[str]:
    """Causas compatibles con lo que se ve ahora, de más a menos respaldada (frases cortas)."""
    out: list[str] = []
    f = _Fmt(tz_name)
    sps = [r for r in _good(setpoint or []) if r.event_time <= now]
    if sps:
        cur = float(sps[-1].value)
        ch = setpoint_changes(setpoint, now - timedelta(hours=3), now)
        if ch:
            tt, a0, b0 = ch[-1]
            out.append(f"el setpoint cambió de {_n(a0)} a {_n(b0)} °C a las {f.hm(tt)}")
        elif cur >= limit_c - 1.5:
            out.append(f"el setpoint está en {_n(cur)} °C, cerca del límite")
    om = _open_minutes(door, now)
    if om > 0:
        out.append(f"puerta abierta hace {_dur(om)}")
    elif _state_now(door, now, within_min=10):
        out.append("la puerta se abrió hace pocos minutos")
    if _state_now(defrost, now, within_min=15):
        out.append("deshielo en curso o reciente")
    for l in [l for l in loads if (now - l.t_in).total_seconds() <= 90 * 60]:
        t = f"ingresó {l.label()} hace {_dur((now - l.t_in).total_seconds() / 60.0)}"
        if l.entry_temp_c is not None:
            t += f" (a {_n(l.entry_temp_c)} °C)"
        out.append(t)
    duty = _state_fraction(refr, now - timedelta(minutes=20), now) if refr else None
    if duty is not None:
        if duty >= 0.9:
            out.append(f"compresor al {duty:.0%} y aun así sube (carga alta o pérdida de capacidad)")
        elif duty <= 0.3 and not out:
            out.append(f"el controlador pidió frío solo {duty:.0%} del tiempo")
    if not out:
        out.append("sin puerta, deshielo ni producto que lo expliquen")
    return out


@dataclass
class RiseWatch:
    cfg: object                                  # CoolingConfig
    _last_sent: dict = field(default_factory=dict)
    _levels: dict = field(default_factory=dict)  # id de carga -> último nivel avisado
    _door_alerted: str | None = None
    cases: list = field(default_factory=list)       # avisos emitidos, para registrar en el libro de casos

    def drain_cases(self) -> list:
        out, self.cases = self.cases, []
        return out

    @staticmethod
    def _kind_of_cause(text: str) -> str:
        t = text.lower()
        for key, kind in (("setpoint", "setpoint"), ("puerta abierta", "puerta"), ("puerta", "puerta"),
                          ("deshielo", "deshielo"), ("ingres", "ingreso_producto"), ("compresor", "carga_o_capacidad")):
            if key in t:
                return kind
        return "sin_explicacion"

    def _case(self, now, bot_kind: str, cause: str, feats: dict) -> dict:
        return {"id": "ew:" + now.isoformat(), "type": "alerta_temprana", "start": now.isoformat(), "end": None,
                "reported": True, "reported_at": now.isoformat(), "bot_kind": bot_kind, "bot_cause": cause,
                "features": feats}

    def _cool(self, key: str, now: datetime) -> bool:
        t = self._last_sent.get(key)
        return t is None or (now - t).total_seconds() >= self.cfg.warn_cooldown_min * 60

    def step(self, temp, door, defrost, refr, loads, now: datetime, setpoint=None) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if now is None:
            return out
        cfg = self.cfg
        f = _Fmt(cfg.tz_name)
        trend = fit_trend(temp, window_min=cfg.trend_window_min, now=now)
        _clean = despike(temp)
        last_val = float(_clean[-1].value) if _clean else None         # lo que se muestra: la lectura real, no la recta
        critical = cfg.critical_c if cfg.critical_c is not None else cfg.limit_c + 4.0
        loads = list(loads or [])

        # 1) subida rápida. Por debajo del límite solo cuenta si el compresor está pidiendo
        #    frío (REFR encendido buena parte de los últimos 10 min): con el compresor apagado,
        #    subir hasta el punto de arranque es el ciclo normal de la cámara.
        duty10 = _state_fraction(refr, now - timedelta(minutes=10), now) if refr else None
        compressor_asked = (duty10 is None and trend is not None and trend.temp_now >= cfg.limit_c - 1.0) \
            or (duty10 is not None and duty10 >= 0.5)
        above_limit = trend is not None and trend.temp_now >= cfg.limit_c
        usual = usual_max(temp, cfg.limit_c) if trend is not None and not above_limit else None
        if trend is not None and not above_limit:
            if usual is not None:
                normal_swing = trend.temp_now < usual + 0.3        # dentro de lo que suele alcanzar
            else:
                normal_swing = (trend.accel_c_h2 or 0.0) < -10.0   # la subida ya se frena
        else:
            normal_swing = False
        if (trend and trend.slope_c_h >= cfg.warn_slope_c_h and trend.r2 >= cfg.warn_r2
                and trend.rise_c >= 1.0 and (compressor_asked or above_limit) and not normal_swing
                and not _state_now(defrost, now, within_min=15)
                and _open_minutes(door, now) < cfg.door_warn_min):
            target = cfg.limit_c if trend.temp_now < cfg.limit_c else critical
            mins = trend.minutes_to(target)
            if mins is not None and mins <= cfg.warn_horizon_min and self._cool("rise", now):
                self._last_sent["rise"] = now
                causes = possible_causes(trend, door, defrost, refr, loads, now, cfg.tz_name, setpoint, cfg.limit_c)
                lines = [f"🟠 Sube rápido: {_n(last_val if last_val is not None else trend.temp_now)} °C (+{_n(trend.slope_c_h)} °C/h). "
                         f"Llegaría a {_n(target, 0)} °C en ~{_dur(mins)} si sigue así"
                         + (" (ya se está frenando)." if (trend.accel_c_h2 or 0) < -10 else "."),
                         "Causa probable: " + "; ".join(causes[:2]) + "."]
                lines += [f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name)
                          for r in assess_loads(loads, temp, cfg.limit_c, now, cfg.product_rules)][:2]
                out.append(("alerta_temprana", "\n".join(lines)))
                self.cases.append(self._case(
                    now, self._kind_of_cause(causes[0]), "; ".join(causes[:2]),
                    {"slope_c_h": round(trend.slope_c_h, 2), "temp_c": round(trend.temp_now, 2),
                     "r2": round(trend.r2, 2), "accel": round(trend.accel_c_h2 or 0.0, 1),
                     "rise_c": round(trend.rise_c, 2), "refr_duty10": None if duty10 is None else round(duty10, 2),
                     "limit_c": cfg.limit_c, "hour_local": now.astimezone(f.tz).hour}))

        # 2) puerta abierta demasiado tiempo
        om = _open_minutes(door, now)
        if om >= cfg.door_warn_min:
            key = f"door:{int((now - timedelta(minutes=om)).timestamp() // 60)}"
            if self._door_alerted != key:
                self._door_alerted = key
                msg = [f"🟠 Puerta abierta hace {_dur(om)} — "
                       + (f"{_n(last_val if last_val is not None else trend.temp_now)} °C" + (f" (+{_n(trend.slope_c_h)} °C/h)" if trend.slope_c_h > 0.5 else "")
                          if trend else "sin tendencia")
                       + ". Conviene cerrarla."]
                msg += [f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name)
                        for r in assess_loads(loads, temp, cfg.limit_c, now, cfg.product_rules)][:2]
                out.append(("alerta_temprana", "\n".join(msg)))
                self.cases.append(self._case(
                    now, "puerta", f"puerta abierta hace {_dur(om)}",
                    {"door_open_min": round(om, 1), "temp_c": None if not trend else round(trend.temp_now, 2),
                     "slope_c_h": None if not trend else round(trend.slope_c_h, 2), "limit_c": cfg.limit_c,
                     "hour_local": now.astimezone(f.tz).hour}))

        # 3) riesgo del producto registrado
        order = ["ok", "vigilar", "riesgo", "critico"]
        for r in assess_loads(loads, temp, cfg.limit_c, now, cfg.product_rules):
            prev = self._levels.get(r.load.id, "ok")
            if order.index(r.level) > order.index(prev) and r.level in ("riesgo", "critico"):
                self._levels[r.load.id] = r.level
                head = "🔴 Producto en riesgo" if r.level == "critico" else "🟠 Producto con riesgo creciente"
                out.append(("riesgo_producto",
                            f"{head}\n" + render_load_line(r, cfg.limit_c, cfg.tz_name)
                            + ".\nLa decisión sobre el producto es de calidad."))
        for lid in list(self._levels):
            if lid not in {l.id for l in loads}:
                del self._levels[lid]
        return out
