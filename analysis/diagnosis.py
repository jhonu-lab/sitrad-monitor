"""
analysis/diagnosis.py — capa determinista de diagnóstico.

Entrada : series por canal (lecturas ordenadas) hasta "ahora".
Salida  : DiagnosisBundle = rasgos medidos + hipótesis ordenadas, cada una
          con evidencia a favor y en contra. El LLM solo redacta a partir
          de este paquete; no razona sobre series crudas.

Instrumentos: CONTROLADOR (temperatura, REFR, DEFR, puerta) y MULTIPOWER
(M1 = circuito de la cava, M2 = otro circuito; supuesto de simulación).
La clave del diagnóstico es CRUZAR ambos: REFR (lo que el controlador
PIDE) contra los kW de M1 (lo que el circuito CONSUME).

Los nombres de hipótesis coinciden con EPISODE_CATALOG de CavaRandomSource.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any

from core.contracts import ChannelReading, DataQuality

try:
    from core.channel_catalog import ALL_SPECS, ROLE_TO_CHANNEL
    _GROUPS: dict[str, list[str]] = {}
    for _s in ALL_SPECS:
        _GROUPS.setdefault(_s.instrument, []).append(_s.name)
except Exception:                                    # pragma: no cover
    ROLE_TO_CHANNEL, _GROUPS = {}, {}

DEFAULT_CFG: dict[str, Any] = {
    "min_refr_run_min": 6.0,   # un compresor que lleva menos de esto está arrancando: no se juzga su consumo
    "ch_temp": ROLE_TO_CHANNEL.get("temp_principal", "Temp Cámara"),
    "ch_setpoint": "Setpoint",
    "ch_refr": ROLE_TO_CHANNEL.get("compresor", "Estado Refrigeración"),
    "ch_defr": ROLE_TO_CHANNEL.get("deshielo", "Estado Deshielo"),
    "ch_door": ROLE_TO_CHANNEL.get("puerta", "Puerta Abierta"),
    "ch_evap": ROLE_TO_CHANNEL.get("evaporador", "Temp Evaporador"),
    "ch_fan": ROLE_TO_CHANNEL.get("ventilador", "Estado Ventilador"),
    "cap_evap_rise_c_h": 2.0,         # capacidad_reducida: el evaporador sube más que esto con REFR=ON
    "cap_refr_run_min": 30.0,         #   ... durante al menos esto
    "cap_temp_rise_c_h": 1.0,         #   y la cámara sube más que esto
    "cap_refr_duty": 0.8,             #   con REFR ON más de esta fracción del tiempo
    "ch_kw": ROLE_TO_CHANNEL.get("kw_m1", "M1 Potencia Activa"),
    "volt_own": ["M1 Tensión F1", "M1 Tensión F2", "M1 Tensión F3"],
    "volt_ref": ["M2 Tensión F1", "M2 Tensión F2", "M2 Tensión F3"],
    "consistency_rule": "refr_vs_kw",
    "groups": _GROUPS,
    "window_minutes": 30.0,
    "flat_min_samples": 10,
    "missing_min_consecutive": 3,
    "rising_c_per_h": 1.5,            # pendiente que cuenta como "sube"
    "dead_ratio": 0.4,
    "partial_ratio": 0.8,
    "volt_drop_pct": 3.0,
    "volt_base_gap_min": 20.0,
    "defrost_max_min": 40.0,          # duración programada + holgura
    "defrost_grace_min": 45.0,
    "door_long_min": 8.0,
    "provisional_below_min": 6.0,
}
# Hipótesis que describen operación NORMAL: un incidente cuya hipótesis
# principal es una de ellas es una alerta que el propio sistema explica
# como esperada (deshielo programado, recuperación, carga alta, entrada de
# producto). Se registran como caso pero no se envían al usuario.
BENIGN_HYPOTHESES = frozenset({
    "deshielo_programado", "recuperacion_post_deshielo",
    "carga_grande", "entrada_producto",
})

# Tipos de incidente que SÍ pueden silenciarse cuando el diagnóstico es benigno.
# Los estructurales (consistencia entre instrumentos, señal plana, estado
# excedido, falta de datos) delatan una anomalía que una hipótesis benigna
# no explica: nunca se silencian, aunque el diagnóstico salga benigno.
SILENCEABLE_KINDS = frozenset({"out_of_profile", "persistent_drift"})
# Hipótesis que describen el estado del equipo, no la causa de un incidente: van a bundle.avisos.
INFORMATIVE_HYPOTHESES = frozenset({"datalogger_lleno"})


def is_benign(bundle, incident_kind: str | None = None) -> bool:
    if not bundle.hipotesis or bundle.principal not in BENIGN_HYPOTHESES:
        return False
    return incident_kind is None or incident_kind in SILENCEABLE_KINDS


# ----------------------------------------------------------------------
# Decisión única sobre un incidente (la usan Reporter y la validación)
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class IncidentAssessment:
    bundle: Any
    silenced: bool
    reason: str = ""          # "benigna" | "sin_explicacion" | ""


def is_unexplained(bundle, incident_kind: str | None, severity: float | None,
                   max_severity: float = 0.8) -> bool:
    """
    Incidente puramente estadístico (fuera de perfil / deriva) para el que
    ninguna regla física encontró explicación y que no es grave: no merece
    una alerta. Se registra y entra en el resumen de turno.
    Los estructurales (consistencia, señal plana, falta de datos, estado
    excedido) nunca entran aquí: delatan algo que sí hay que mirar.
    """
    if incident_kind is not None and incident_kind not in SILENCEABLE_KINDS:
        return False
    if bundle.hipotesis and bundle.principal != "indeterminado":
        return False
    return severity is None or float(severity) < max_severity


def _prefer_non_benign(bundle, incident_kind: str | None):
    """
    Un incidente ESTRUCTURAL (consistencia entre instrumentos, señal plana,
    estado excedido, falta de datos) delata algo que una causa de operación
    normal no explica. Si el diagnóstico sale benigno (p. ej. hay un deshielo
    en curso, que apaga las reglas del compresor) pero existe una hipótesis
    no benigna, esa pasa a ser la principal. La benigna queda como alternativa.
    """
    from dataclasses import replace

    if incident_kind is None or incident_kind in SILENCEABLE_KINDS:
        return bundle
    hs = list(bundle.hipotesis)
    if not hs or hs[0].nombre not in BENIGN_HYPOTHESES:
        return bundle
    nb = [h for h in hs if h.nombre not in BENIGN_HYPOTHESES and h.nombre != "indeterminado"]
    if not nb:
        return bundle
    top = nb[0]
    rest = [h for h in hs if h is not top]
    second = rest[0].score if rest else 0.0
    conf = max(0.0, min(1.0, top.score - second))
    return replace(bundle, hipotesis=tuple([top] + rest), confianza=conf,
                   provisional=bundle.provisional or conf < 0.2)


def assess_incident(series, *, incident_kind: str | None = None,
                    severity: float | None = None, levels=None,
                    compressor_refs=None, config=None,
                    suppress_benign: bool = True,
                    suppress_unexplained: bool = True,
                    unexplained_max_severity: float = 0.8) -> IncidentAssessment:
    """diagnose() + decisión de silenciar, en un solo lugar."""
    bundle = diagnose(series, config=config, compressor_refs=compressor_refs,
                      levels=levels)
    bundle = _prefer_non_benign(bundle, incident_kind)
    if suppress_benign and is_benign(bundle, incident_kind):
        return IncidentAssessment(bundle, True, "benigna")
    if suppress_unexplained and is_unexplained(
            bundle, incident_kind, severity, unexplained_max_severity):
        return IncidentAssessment(bundle, True, "sin_explicacion")
    return IncidentAssessment(bundle, False, "")
BAD_Q = {DataQuality.MISSING, DataQuality.OFFLINE, DataQuality.ERR}
GOOD_Q = {DataQuality.VALID, DataQuality.FROZEN}


@dataclass(frozen=True)
class Hipotesis:
    nombre: str
    score: float
    a_favor: tuple[str, ...] = ()
    en_contra: tuple[str, ...] = ()
    faltan: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosisBundle:
    hasta: datetime
    rasgos: dict[str, Any]
    hipotesis: tuple[Hipotesis, ...]
    confianza: float
    provisional: bool
    faltan: tuple[str, ...] = ()
    avisos: tuple[str, ...] = ()      # condiciones permanentes del equipo (p. ej. datalogger lleno): no compiten

    @property
    def principal(self) -> str:
        return self.hipotesis[0].nombre if self.hipotesis else "indeterminado"

    def to_context(self, top: int = 3) -> str:
        lines = [f"hipotesis_principal_calculada: {self.principal}",
                 f"confianza: {self.confianza:.2f}",
                 f"provisional: {'si' if self.provisional else 'no'}",
                 "rasgos:"]
        for k, v in self.rasgos.items():
            if v is None or k.startswith("_"):
                continue
            lines.append(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")
        for a in self.avisos:
            lines.append(f"[AVISO] {a}")
        for h in self.hipotesis[:top]:
            lines.append(f"hipotesis {h.nombre} (score {h.score:.2f})")
            lines += [f"  + {x}" for x in h.a_favor]
            lines += [f"  - {x}" for x in h.en_contra]
            lines += [f"  ? falta: {x}" for x in h.faltan]
        return "\n".join(lines)


# ------------------------------------------------------------------ utilidades

def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _good(rs):
    return sorted((r for r in rs if r.value is not None and r.quality in GOOD_Q),
                  key=lambda r: r.event_time)


def _trailing_bad(rs) -> int:
    n = 0
    for r in sorted(rs, key=lambda x: x.event_time, reverse=True):
        if r.quality in BAD_Q or r.value is None:
            n += 1
        else:
            break
    return n


def _dt_min(rs) -> float:
    ts = sorted(r.event_time for r in rs)
    if len(ts) < 2:
        return 2.0
    g = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
    return max(g[len(g) // 2], 1.0) / 60.0


def _slope_per_h(pts) -> float | None:
    if len(pts) < 5:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in pts]
    ys = [v for _, v in pts]
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 1e-12:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _bit_run(rs) -> tuple[int | None, float]:
    g = _good(rs)
    if not g:
        return None, 0.0
    dt = _dt_min(g)
    cur = 1 if float(g[-1].value) > 0.5 else 0
    start = g[-1].event_time
    for r in reversed(g[:-1]):
        if (1 if float(r.value) > 0.5 else 0) != cur:
            break
        start = r.event_time
    return cur, (g[-1].event_time - start).total_seconds() / 60.0 + dt


def _last_mean(rs, n=3):
    g = _good(rs)
    return mean(float(r.value) for r in g[-n:]) if g else None


def _open_minutes(rs, since: datetime) -> float:
    g = [r for r in _good(rs) if r.event_time >= since]
    return sum(1 for r in g if float(r.value) > 0.5) * _dt_min(g) if g else 0.0


# ------------------------------------------------------------------ rasgos

def extract_features(series, cfg, compressor_refs=None, levels=None):
    F: dict[str, Any] = {}
    all_t = [r.event_time for rs in series.values() for r in rs]
    if not all_t:
        return F
    now = max(all_t)
    w0 = now - timedelta(minutes=cfg["window_minutes"])
    F["ahora"] = now.isoformat()

    # Comunicación por instrumento
    missing = sorted(ch for ch, rs in series.items()
                     if _trailing_bad(rs) >= cfg["missing_min_consecutive"])
    F["canales_sin_datos"] = ", ".join(missing) if missing else None
    F["n_canales_sin_datos"] = len(missing)
    F["n_canales"] = len(series)
    down, alive = [], []
    for g, chs in cfg["groups"].items():
        present = [c for c in chs if c in series]
        if not present:
            continue
        (down if all(c in missing for c in present) else alive).append(g)
    F["equipos_sin_datos"] = ", ".join(down) if down else None
    F["equipos_con_datos"] = ", ".join(alive) if alive else None
    if missing:
        F["min_sin_datos"] = max(_trailing_bad(series[c]) * _dt_min(series[c])
                                 for c in missing)
        # Alarmas del propio controlador
    def _state_on(ch):
        rs = _good(series.get(ch, []))
        return bool(rs and float(rs[-1].value) > 0.5)
    F["alr_temp_alta_on"] = _state_on("Alarma Temp Alta")
    F["alr_temp_baja_on"] = _state_on("Alarma Temp Baja")
    F["alr_puerta_on"]   = _state_on("Alarma Puerta")
    F["alr_deshielo_on"] = _state_on("Alarma Deshielo Timeout")
    F["datalogger_lleno_on"] = _state_on("Datalogger Lleno")
    F["datalogger_overwrote_on"] = _state_on("Datalogger Ya Sobrescribió")
    _dl = _good(series.get("Datalogger Uso", []))
    if _dl:
        F["datalogger_uso_pct"] = float(_dl[-1].value)
    F["err_s1_on"] = _state_on("Error Sensor 1")
    F["err_s2_on"] = _state_on("Error Sensor 2")
    F["err_s3_on"] = _state_on("Error Sensor 3")
    # Límite del MT (para coherencia con temp_limit_c del bot)
    lim_rs = _good(series.get("Límite Alarma Temp", []))
    if lim_rs:
        F["limite_alarma_mt_c"] = float(lim_rs[-1].value)
    defr_cfg = _good(series.get("Duración Deshielo Config", []))
    if defr_cfg:
        F["deshielo_max_config"] = int(defr_cfg[-1].value)
    # Temperatura de cámara
    temp = [r for r in _good(series.get(cfg["ch_temp"], [])) if r.event_time >= w0]
    if len(temp) >= 6 and cfg["ch_temp"] not in missing:
        vals = [float(r.value) for r in temp]
        F["temp_actual"] = vals[-1]
        sp = _last_mean(series.get(cfg["ch_setpoint"], []), 1)
        if sp is not None:
            F["setpoint"] = sp
            F["desvio_c"] = vals[-1] - sp
        F["pendiente_c_h"] = _slope_per_h([(r.event_time, float(r.value)) for r in temp])
        F["delta_ventana_c"] = median(vals[-3:]) - median(vals[:3])
        n = int(cfg["flat_min_samples"])
        F["temp_plana"] = len(vals) >= n and max(vals[-n:]) - min(vals[-n:]) < 1e-9
        F["minutos_observados"] = (temp[-1].event_time - temp[0].event_time).total_seconds() / 60.0

    # Refrigeración (lo que el controlador PIDE)
    refr_all = series.get(cfg["ch_refr"], [])
    refr_w = [r for r in _good(refr_all) if r.event_time >= w0]
    if refr_w and cfg["ch_refr"] not in missing:
        bit, run = _bit_run(refr_all)
        F["refr_estado"] = "ON" if bit else "OFF"
        F["refr_racha_min"] = run
        bits = [1 if float(r.value) > 0.5 else 0 for r in refr_w]
        F["refr_duty"] = mean(bits)
        F["refr_transiciones"] = sum(1 for a, b in zip(bits, bits[1:]) if a != b)
        if compressor_refs:
            F["refr_on_p95_min"] = compressor_refs[0]

    # Deshielo y puerta
    defr = series.get(cfg["ch_defr"], [])
    if defr and cfg["ch_defr"] not in missing:
        bit, run = _bit_run(defr)
        F["deshielo_activo"] = bool(bit)
        F["deshielo_racha_min"] = run if bit else 0.0
        g = _good(defr)
        on_t = [r.event_time for r in g if float(r.value) > 0.5]
        if on_t:
            F["min_desde_deshielo"] = (now - on_t[-1]).total_seconds() / 60.0
    door = series.get(cfg["ch_door"], [])
    if door and cfg["ch_door"] not in missing:
        bit, run = _bit_run(door)
        F["puerta_abierta"] = bool(bit)
        F["puerta_racha_min"] = run if bit else 0.0
        F["puerta_min_abierta_ventana"] = _open_minutes(door, w0)

    # Evaporador y ventilador (solo si el controlador los entrega)
    ev_w = [r for r in _good(series.get(cfg["ch_evap"], [])) if r.event_time >= w0]
    if len(ev_w) >= 6 and cfg["ch_evap"] not in missing:
        F["evap_actual"] = float(ev_w[-1].value)
        F["evap_pendiente_c_h"] = _slope_per_h([(r.event_time, float(r.value)) for r in ev_w])
    fan = series.get(cfg["ch_fan"], [])
    if fan and cfg["ch_fan"] not in missing:
        fb, _ = _bit_run(fan)
        F["ventilador_estado"] = "ON" if fb else "OFF"

    # Potencia M1 contra lo que REFR pide
    kw_rs = _good(series.get(cfg["ch_kw"], []))
    if kw_rs and cfg["ch_kw"] not in missing:
        F["kw_actual"] = _last_mean(kw_rs, 3)
    lv = (levels or {}).get(cfg["consistency_rule"])
    on_k = off_k = None
    if lv and lv.get("n_on", 0) >= 10 and lv.get("n_off", 0) >= 10:
        on_k, off_k = lv["on"], lv["off"]
    if on_k is None and kw_rs and refr_w:       # respaldo: del propio histórico
        kw_by_t = {r.event_time: float(r.value) for r in kw_rs}
        dmap = {r.event_time: float(r.value) > 0.5 for r in _good(defr)}
        a = [kw_by_t[r.event_time] for r in _good(refr_all)
             if float(r.value) > 0.5 and r.event_time in kw_by_t and not dmap.get(r.event_time)]
        b = [kw_by_t[r.event_time] for r in _good(refr_all)
             if float(r.value) <= 0.5 and r.event_time in kw_by_t and not dmap.get(r.event_time)]
        if len(a) >= 4 and len(b) >= 4:
            on_k, off_k = median(a), median(b)
    if on_k is not None and off_k is not None and on_k - off_k > 1e-6:
        F["kw_esperado_on"], F["kw_esperado_off"] = on_k, off_k
        if F.get("kw_actual") is not None and F.get("refr_estado") == "ON":
            F["kw_ratio_refr"] = (F["kw_actual"] - off_k) / (on_k - off_k)

    # Tensión: caída contra la PROPIA base de la ventana (no contra el otro
    # medidor, que puede tener otra toma) y si el otro medidor también cae.
        # Tensión: caída contra la PROPIA base (no contra el otro medidor, que
    # puede tener otra toma) y si el otro medidor también cae. La base es la
    # mediana de TODA la serie disponible anterior a `volt_base_gap_min`
    # minutos: así una caída sostenida más de 30 min sigue detectándose.
    def _drop(chs):
        rec, base = [], []
        cut = now - timedelta(minutes=cfg["volt_base_gap_min"])
        for c in chs:
            g = _good(series.get(c, []))
            older = [float(r.value) for r in g if r.event_time <= cut]
            if len(g) >= 8 and len(older) >= 6:
                rec.append(mean(float(r.value) for r in g[-2:]))
                base.append(median(older))
        if not rec:
            return None, None
        return mean(rec), mean(base)

    own_v, own_b = _drop(cfg["volt_own"])
    ref_v, ref_b = _drop(cfg["volt_ref"])
    if own_v is not None:
        F["tension_propia_v"] = own_v
        F["tension_base_v"] = own_b
        F["caida_tension_pct"] = (1.0 - own_v / own_b) * 100.0
    if ref_v is not None:
        F["tension_otro_medidor_v"] = ref_v
        F["caida_otro_medidor_pct"] = (1.0 - ref_v / ref_b) * 100.0
    return F


# ------------------------------------------------------------------ hipótesis

def _sube(F, cfg) -> bool:
    s = F.get("pendiente_c_h")
    return s is not None and s >= cfg["rising_c_per_h"]


def _capacidad_reducida(F, cfg, refr_on: bool, pend) -> Hipotesis | None:
    """El frío pedido no se traduce en frío entregado: evaporador y cámara suben con REFR ON sostenido y
    el ventilador funciona (no es falla de ventilador)."""
    ev_sl = F.get("evap_pendiente_c_h")
    if not (refr_on and not F.get("deshielo_activo") and ev_sl is not None and pend is not None
            and ev_sl > cfg["cap_evap_rise_c_h"]
            and F.get("refr_racha_min", 0.0) >= cfg["cap_refr_run_min"]
            and pend > cfg["cap_temp_rise_c_h"]
            and F.get("refr_duty", 0.0) > cfg["cap_refr_duty"]
            and F.get("ventilador_estado") == "ON"):
        return None
    s = 0.8
    pro = [f"Temp Evaporador sube {ev_sl:+.1f} °C/h con REFR ON {F['refr_racha_min']:.0f} min seguidos",
           f"REFR ON {F['refr_duty']:.0%} del tiempo",
           f"Temp Cámara {pend:+.1f} °C/h",
           "Ventilador del evaporador en ON: no es falla de ventilador"]
    if F.get("puerta_abierta") or F.get("puerta_min_abierta_ventana", 0.0) >= 1.0:
        s -= 0.2
        con = ["Hubo puerta abierta en la ventana: parte de la subida puede ser de ella"]
    else:
        s += 0.1
        con = []
    return Hipotesis("capacidad_reducida", _clip(s), tuple(pro), tuple(con),
                     faltan=("Presión de succión/descarga para distinguir refrigerante de evaporador con hielo",))


def _hyps(F, cfg) -> list[Hipotesis]:
    out: list[Hipotesis] = []
    refr_on = F.get("refr_estado") == "ON"
    ratio = F.get("kw_ratio_refr")
    sube = _sube(F, cfg)
    pend = F.get("pendiente_c_h")

    # datalogger_lleno: el buffer circular del controlador está al límite (se pierde lo más antiguo)
    if F.get("datalogger_lleno_on") or (F.get("datalogger_uso_pct") or 0) >= 95:
        uso = F.get("datalogger_uso_pct")
        out.append(Hipotesis("datalogger_lleno", 0.85, (
            (f"Datalogger al {uso:.0f}% de uso" if uso is not None else "Datalogger lleno")
            + (" (ya sobrescribió)" if F.get("datalogger_overwrote_on") else ""),
            "Los datos más antiguos se pierden en la próxima vuelta"), (),
            faltan=("Tamaño del datalogger y política de sobrescritura",)))

    # sin_comunicacion
    if F.get("n_canales_sin_datos", 0):
        pro, con = [], []
        if F.get("equipos_sin_datos"):
            s = 0.9
            pro.append(f"Sin datos del equipo: {F['equipos_sin_datos']} "
                       f"({F.get('min_sin_datos', 0):.0f} min)")
            if F.get("equipos_con_datos"):
                pro.append(f"{F['equipos_con_datos']} sigue reportando: falla del enlace "
                           "con un instrumento, no de la cava")
        else:
            s = 0.55
            pro.append(f"Solo sin datos: {F['canales_sin_datos']}")
            con.append("Otros canales del mismo equipo siguen reportando")
        out.append(Hipotesis("sin_comunicacion", _clip(s), tuple(pro), tuple(con)))

    # sensor_congelado
    if F.get("temp_plana"):
        pro, con, s = [f"Temp Cámara idéntica en las últimas {cfg['flat_min_samples']} "
                       f"muestras ({F['temp_actual']:.1f} °C)"], [], 0.7
        if F.get("refr_transiciones") == 0:
            s += 0.2
            pro.append("REFR no cambia de estado en la ventana: el controlador actúa "
                       "con un valor que no se mueve")
        else:
            con.append("REFR sigue ciclando")
        out.append(Hipotesis("sensor_congelado", _clip(s), tuple(pro), tuple(con),
                             faltan=("Segunda lectura de temperatura (otra sonda o "
                                     "termómetro externo) para confirmar",)))
        if F.get("deshielo_activo") or (
                F.get("min_desde_deshielo") is not None
                and F["min_desde_deshielo"] <= cfg["defrost_grace_min"]):
            s += 0.2
            pro.append("Hay un deshielo reciente y la temperatura debería haberse movido: "
                       "una lectura quieta no es normal")
    # baja_tension
    cai = F.get("caida_tension_pct")
    if cai is not None and cai >= cfg["volt_drop_pct"]:
        otro = F.get("caida_otro_medidor_pct")
        pro = [f"Tensión M1 {F['tension_propia_v']:.0f} V, {cai:.1f} % bajo su nivel "
               f"previo ({F['tension_base_v']:.0f} V)"]
        con = []
        if otro is not None and otro < cfg["volt_drop_pct"] / 2:
            pro.append(f"M2 sin cambio ({otro:+.1f} %): caída local del circuito de M1, "
                       "no de la red")
            s = 0.88
        elif otro is not None:
            pro.append(f"M2 también cae {otro:.1f} %: probable caída de la red")
            s = 0.8
        else:
            s = 0.8
        if F.get("desvio_c") is not None and F["desvio_c"] <= 4.0 and not sube:
            pro.append("Sin efecto térmico en la cámara (por ahora)")
        out.append(Hipotesis("baja_tension", _clip(s), tuple(pro), tuple(con),
                             faltan=("Registro de la red o del alimentador para "
                                     "separar problema de la red de uno interno",)))

    # deshielo
    if F.get("deshielo_activo"):
        run = F["deshielo_racha_min"]
        if run > cfg["defrost_max_min"]:
            out.append(Hipotesis("deshielo_extendido", 0.9, (
                f"Deshielo activo {run:.0f} min (programado ≤ {cfg['defrost_max_min']:.0f})",
                "La temperatura de la cámara sigue subiendo" if sube else
                "Temperatura aún sin tendencia clara"), (),
                faltan=("Temperatura del evaporador o sensor de fin de deshielo",)))
        elif not F.get("temp_plana"):
            out.append(Hipotesis("deshielo_programado", 0.8, (
                f"Deshielo activo {run:.0f} min (dentro de lo programado)",
                "La subida de temperatura es esperada durante el deshielo"), ()))

    # Familia REFR pide frío
    if refr_on and not F.get("deshielo_activo"):
        racha = F.get("refr_racha_min", 0.0)
        if racha < cfg["min_refr_run_min"]:
            ratio = None          # arrancando: el consumo aún no es comparable con el régimen normal
        if ratio is not None and ratio < cfg["dead_ratio"]:
            pro = [f"REFR activo {racha:.0f} min, pero M1 consume {F['kw_actual']:.1f} kW "
                   f"(en reposo {F['kw_esperado_off']:.1f}, con compresor {F['kw_esperado_on']:.1f})",
                   "El controlador pide frío y el circuito no muestra consumo de compresor"]
            s = 0.8
            if sube:
                s += 0.15
                pro.append(f"La temperatura sube {pend:+.1f} °C/h")
            out.append(Hipotesis("compresor_sin_arranque", _clip(s), tuple(pro), (),
                                 faltan=("Estado del contactor o de las protecciones "
                                         "del compresor (no disponible en estos dos "
                                         "instrumentos)",)))
        elif ratio is not None and ratio < cfg["partial_ratio"]:
            pro = [f"REFR activo {racha:.0f} min con M1 en {F['kw_actual']:.1f} kW: "
                   f"{ratio:.0%} del aumento normal de consumo con compresor",
                   "El compresor consume menos de lo habitual mientras trabaja"]
            s = 0.7
            if sube:
                s += 0.2
                pro.append(f"La temperatura sube {pend:+.1f} °C/h pese a REFR activo")
            out.append(Hipotesis("fallo_refrigeracion", _clip(s), tuple(pro), (),
                                 faltan=("Presión de succión y descarga (controlador "
                                         "de presión): sin ella no se distingue "
                                         "pérdida de refrigerante, evaporador con "
                                         "hielo u obstrucción",)))
        elif ratio is not None:
            # Consumo normal con REFR activo: ¿carga o puerta?
            post_def = (F.get("min_desde_deshielo") is not None
                        and F["min_desde_deshielo"] <= cfg["defrost_grace_min"])
            if F.get("puerta_min_abierta_ventana", 0.0) >= 1.0 or F.get("puerta_abierta"):
                pass
            elif post_def and not sube:
                out.append(Hipotesis("recuperacion_post_deshielo", 0.7, (
                    f"REFR activo tras un deshielo (hace {F['min_desde_deshielo']:.0f} min) "
                    f"con consumo normal ({ratio:.0%} del esperado)",
                    "El compresor recupera la temperatura de la cámara: comportamiento esperado"
                    + (f" (la temperatura baja {pend:+.1f} °C/h)" if pend is not None else "")),
                    ()))
            elif F.get("refr_duty", 0.0) >= 0.9:
                out.append(Hipotesis("carga_grande", 0.6, (
                    f"REFR activo {F['refr_duty']:.0%} del tiempo con consumo normal "
                    f"({ratio:.0%} del esperado)",
                    "El compresor responde a más carga térmica; no hay pérdida de capacidad"),
                    (),
                    faltan=("Registro de ingreso de producto o temperatura del "
                            "producto",)))

    h = _capacidad_reducida(F, cfg, refr_on, pend)
    if h is not None:
        out.append(h)
    # Alarmas directas del controlador: son más fiables que derivar del patrón.
    if F.get("alr_temp_alta_on"):
        out.append(Hipotesis("alarma_temp_alta_controlador", 0.85, (
            "El controlador reporta alarma de temperatura alta (IsAlrHighTempS1)",
            f"Temp Cámara {F.get('temp_actual', 0):.1f} °C"), ()))
    if F.get("alr_temp_baja_on"):
        out.append(Hipotesis("alarma_temp_baja_controlador", 0.8, (
            "El controlador reporta alarma de temperatura baja (IsAlrLowTempS1)",), ()))
    if F.get("alr_deshielo_on"):
        # refuerza deshielo_extendido con el timeout del propio MT
        out.append(Hipotesis("deshielo_extendido", 0.95, (
            "El controlador reporta timeout de deshielo (IsDefrTimeOut)",
            f"Deshielo activo {F.get('deshielo_racha_min', 0):.0f} min",
            f"Máximo configurado: {F.get('deshielo_max_config', '?')} min"), ()))
    if F.get("err_s1_on"):
        out.append(Hipotesis("sensor_error", 0.9, (
            "El controlador reporta error en la sonda S1 (IsErrorS1)",), ()))
    if F.get("alr_puerta_on"):
        out.append(Hipotesis("puerta_prolongada", 0.85, (
            "El controlador reporta alarma de puerta abierta (IsAlrmDoorOpn)",), ()))
    # Puerta
    if F.get("puerta_abierta") or F.get("puerta_min_abierta_ventana", 0.0) >= 1.0:
        mins = max(F.get("puerta_racha_min", 0.0), F.get("puerta_min_abierta_ventana", 0.0))
        if mins >= cfg["door_long_min"]:
            pro = [f"Puerta abierta {mins:.0f} min (≥ {cfg['door_long_min']:.0f})"]
            if sube:
                pro.append(f"La temperatura sube {pend:+.1f} °C/h")
            if ratio is not None and ratio >= cfg["partial_ratio"]:
                pro.append("El compresor responde con consumo normal: no hay falla de equipo")
            out.append(Hipotesis("puerta_prolongada", _clip(0.75 + (0.1 if sube else 0)),
                                 tuple(pro), ()))
        else:
            out.append(Hipotesis("entrada_producto", 0.6, (
                f"Puerta abierta solo {mins:.0f} min",
                "Duración compatible con entrada o salida de producto"), ()))
    return out


def diagnose(series, config=None, compressor_refs=None, levels=None) -> DiagnosisBundle:
    """
    `levels`: salida de AnomalyDetector.consistency_levels() (niveles de kW
    con REFR=1 / REFR=0 aprendidos en operación normal). Si falta, se
    derivan del histórico disponible.
    """
    cfg = {**DEFAULT_CFG, **(config or {})}
    F = extract_features(series, cfg, compressor_refs, levels)
    if not F:
        return DiagnosisBundle(datetime.min, {}, (), 0.0, True)

    hyps = _hyps(F, cfg)
    # Si TODO está sin comunicación, los rasgos físicos son viejos.
    if F.get("n_canales_sin_datos", 0) == F.get("n_canales", -1):
        hyps = [h for h in hyps if h.nombre == "sin_comunicacion"]

    # Condiciones permanentes del equipo: se informan aparte y NO compiten como causa del incidente
    # (si competieran, un datalogger siempre lleno volvería "explicado" cualquier incidente).
    avisos = tuple(" · ".join(h.a_favor) for h in hyps if h.nombre in INFORMATIVE_HYPOTHESES)
    hyps = [h for h in hyps if h.nombre not in INFORMATIVE_HYPOTHESES]
    hyps = sorted((h for h in hyps if h.score >= 0.35), key=lambda h: -h.score)
    top = hyps[0].score if hyps else 0.0
    second = hyps[1].score if len(hyps) > 1 else 0.0
    conf = _clip(top - second) if hyps else 0.0
    if not hyps:
        hyps = [Hipotesis("indeterminado", 0.0, (),
                          ("Ninguna regla alcanza el umbral con los datos actuales",))]
    provisional = conf < 0.2 or (
        F.get("minutos_observados", 99.0) < cfg["provisional_below_min"])
    return DiagnosisBundle(
        hasta=datetime.fromisoformat(F["ahora"]), rasgos=F,
        hipotesis=tuple(hyps), confianza=conf, provisional=provisional, avisos=avisos)