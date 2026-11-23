"""
tools/audit_real_data.py — ¿el pipeline sabe usar la estructura REAL de SITRAD?

    python tools/audit_real_data.py <controlador.xlsx> <multipower.xlsx> [--recommended] [--tail]

Carga los dos informes, los mete en SeriesStore y llama a las herramientas
reales de CavaAgent (stats, slope, correlate, energy) y a AnomalyDetector.
Imprime PASS/FAIL con la evidencia. --recommended aplica la configuración
de RECOMMENDED (gap de 600 s y canales exentos de señal plana).
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
from agent.cava_agent import CavaAgent
from analysis.anomaly_detector import AnomalyDetector
from analysis.engine import AnalysisEngine
from core.series_store import SeriesStore
from tools.sitrad_excel import load_all

DEV = 201

# Configuración recomendada para datos reales a 120 s (ver informe).
RECOMMENDED = {
    "max_change_gap_seconds": 600.0,      # 5 x cadencia de 120 s
    "flat_signal_exempt_channels": [      # constantes por diseño
        "Setpoint", "Desvío vs Setpoint", "M1 Frecuencia", "M2 Frecuencia",
        "M1 Factor de Potencia", "M2 Factor de Potencia",
    ],
}
RESULTS: list[tuple[str, bool, str]] = []


SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {detail}")


def skip(name: str, reason: str) -> None:
    SKIPPED.append(name)
    print(f"[SKIP] {name}\n       {reason}")


def big_gaps(readings, channel: str, min_minutes: float = 10.0, top: int = 3):
    ts = sorted(r.event_time for r in readings
                if r.channel == channel and r.quality.value == "VALID")
    gaps = [((b - a).total_seconds() / 60.0, a, b) for a, b in zip(ts, ts[1:])]
    return sorted((g for g in gaps if g[0] >= min_minutes), reverse=True)[:top]


_LADDER = (10, 30, 60, 120, 300, 600, 1200, 3600)     # pasos "típicos" en segundos


def _label(sec: float) -> str:
    import math
    nearest = min(_LADDER, key=lambda x: abs(math.log(max(sec, 1) / x)))
    return f"{nearest} s" if nearest < 60 else f"{nearest // 60} min"

def regimes(readings, channel: str):
    """Mapa de regímenes de muestreo: [(inicio, fin, etiqueta, n_lecturas)].
    Etiqueta = paso mediano de cada hora redondeado a uno típico, o SIN DATOS.
    Un corte es una hora sin ninguna lectura; la cadencia lenta NO es un corte."""
    from collections import defaultdict
    from datetime import timedelta
    ts = sorted(r.event_time for r in readings
                if r.channel == channel and r.quality.value == "VALID")
    if len(ts) < 2:
        return []
    bins = defaultdict(list)
    for t in ts:
        bins[t.replace(minute=0, second=0, microsecond=0)].append(t)
    out, h, end = [], min(bins), max(bins)
    while h <= end:
        v = bins.get(h, [])
        if len(v) >= 2:
            g = sorted((b - a).total_seconds() for a, b in zip(v, v[1:]))
            lab = _label(g[len(g) // 2])
        else:
            lab = "1 lectura/h" if v else "SIN DATOS"
        if out and out[-1][2] == lab:
            out[-1] = (out[-1][0], h + timedelta(hours=1), lab, out[-1][3] + len(v))
        else:
            out.append((h, h + timedelta(hours=1), lab, len(v)))
        h += timedelta(hours=1)
    return out


def dense_end(readings, channels, span_min=240.0, min_cov=0.8, block_min=30.0,
              min_block_cov=0.7, step_min=10.0):
    """Último instante T tal que, en [T-span, T], TODOS los canales cumplen:
    cobertura total >= min_cov Y cada bloque de `block_min` >= min_block_cov
    (así una última hora escasa no se esconde dentro de un promedio bueno).
    None si no existe."""
    from bisect import bisect_left, bisect_right
    from datetime import timedelta
    by = {c: sorted(r.event_time for r in readings
                    if r.channel == c and r.quality.value == "VALID") for c in channels}
    if any(not v for v in by.values()):
        return None
    cad = {}
    for c, ts in by.items():
        g = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:])) or [120.0]
        cad[c] = max(g[len(g) // 2], 1.0)
    t = min(v[-1] for v in by.values())
    first = max(v[0] for v in by.values())
    span, block = timedelta(minutes=span_min), timedelta(minutes=block_min)
    n_blocks = int(span_min // block_min)

    def count(ts, a, b):
        return bisect_right(ts, b) - bisect_left(ts, a)

    while t - span >= first:
        ok = True
        for c, ts in by.items():
            if count(ts, t - span, t) < min_cov * span.total_seconds() / cad[c]:
                ok = False
                break
            exp_block = block.total_seconds() / cad[c]
            if any(count(ts, t - span + i * block, t - span + (i + 1) * block) < min_block_cov * exp_block
                   for i in range(n_blocks)):
                ok = False
                break
        if ok:
            return t
        t -= timedelta(minutes=step_min)
    return None


def main(ctrl: str, mp: str, recommended: bool = False, tail: bool = False) -> None:
    readings, (rc, rm) = load_all(ctrl, mp, DEV)
    cfg = dict(RECOMMENDED) if recommended else {}

    def cadence(channel: str) -> float:
        ts = [r.event_time for r in readings if r.channel == channel]
        gaps = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
        return gaps[len(gaps) // 2] if gaps else 0.0

    cad_c, cad_m = cadence("Temp Cámara"), cadence("M1 Potencia Activa")
    print(f"cadencia mediana: controlador={cad_c:.0f} s | multipower={cad_m:.0f} s")
    if recommended:
        cfg["max_change_gap_seconds"] = max(60.0, 5 * max(cad_c, cad_m))
        print(f"max_change_gap_seconds derivado = {cfg['max_change_gap_seconds']:.0f} s")
    if max(cad_c, cad_m) > 3 * max(min(cad_c, cad_m), 1):
        print("AVISO: los instrumentos tienen cadencias MUY distintas; las "
              "correlaciones entre ellos tendrán pocos pares independientes.")
    print(f"configuración: {'RECOMENDADA' if recommended else 'ACTUAL (por defecto)'}")

    # ---- calidad de datos por canal clave: explica n bajos y huecos ----
    print("\n== CALIDAD DE DATOS (canales clave) ==")
    print(f"{'canal':26s}{'filas':>7s}{'válidas':>9s}{'vacías':>8s}{'paso med (s)':>14s}"
          f"{'hueco máx (min)':>17s}")
    for ch in ("Temp Cámara", "Estado Refrigeración", "M1 Potencia Activa",
               "M2 Potencia Activa", "M1 Energía Activa"):
        rs = [r for r in readings if r.channel == ch]
        ok = [r for r in rs if r.quality.value == "VALID"]
        ts = [r.event_time for r in ok]
        gaps = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:])) or [0.0]
        print(f"{ch:26s}{len(rs):7d}{len(ok):9d}{len(rs) - len(ok):8d}"
              f"{gaps[len(gaps) // 2]:14.0f}{gaps[-1] / 60:17.1f}")
        if rs and len(ok) < 0.8 * len(rs):
            print(f"   ^ {100 * (len(rs) - len(ok)) / len(rs):.0f} % de las filas de '{ch}' "
                  "vienen vacías en el Excel: revisa el informe exportado.")
    engine = AnalysisEngine(config=cfg)
    store = SeriesStore(max_hours=48)
    for r in readings:
        store.ingest(r)
    agent = CavaAgent(llm=object(), engine=engine, store=store, device_id=DEV)
    tool = agent._execute_tool

    print("\n== LECTURAS POR DÍA (M1 Potencia Activa y M2 Potencia Activa) ==")
    print(f"{'día (UTC)':12s}{'M1 lect.':>10s}{'M1 paso':>9s}{'M2 lect.':>10s}{'M2 paso':>9s}   (esperado a 120 s: 720)")
    from collections import defaultdict
    per = {c: defaultdict(list) for c in ("M1 Potencia Activa", "M2 Potencia Activa")}
    for r in readings:
        if r.channel in per and r.quality.value == "VALID":
            per[r.channel][r.event_time.date()].append(r.event_time)
    for day in sorted({d for c in per for d in per[c]}):
        cells = []
        for c in per:
            ts = sorted(per[c].get(day, []))
            g = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:])) or [0]
            cells += [f"{len(ts):10d}", f"{g[len(g) // 2]:9.0f}"]
        print(f"{str(day):12s}" + "".join(cells))

    print("\n== REGÍMENES DE MUESTREO (paso mediano por hora; cada cambio es un cambio real) ==")
    for ch in ("Temp Cámara", "M1 Potencia Activa", "M2 Potencia Activa"):
        print(ch)
        for a_, b_, lab, n in regimes(readings, ch):
            horas = (b_ - a_).total_seconds() / 3600.0
            print(f"   {a_:%Y-%m-%d %H:%M} -> {b_:%Y-%m-%d %H:%M} UTC  {lab:>10s}   "
                  f"({horas:5.1f} h, {n} lecturas)")

    m2_ok = True
    if not tail:
        base = ("Temp Cámara", "M1 Potencia Activa", "M1 Energía Activa")
        with_m2 = dense_end(readings, base + ("M2 Potencia Activa",))
        without = dense_end(readings, base)
        # Se prefiere la ventana MÁS RECIENTE; M2 solo cuenta si no obliga a ir
        # claramente más atrás en el tiempo.
        if with_m2 and (not without or (without - with_m2).total_seconds() <= 3600):
            t_end, req = with_m2, base + ("M2 Potencia Activa",)
        else:
            t_end, req = without, base
        m2_ok = "M2 Potencia Activa" in req
        if t_end is None:
            print("\nVENTANA: no existe ninguna de 4 h con datos suficientes en los canales "
                  "clave; se evalúa la cola del archivo (las fallas serán por datos).")
        else:
            dropped = sum(1 for r in readings if r.event_time > t_end)
            readings = [r for r in readings if r.event_time <= t_end]
            msg = (f"\nVENTANA EVALUADA: hasta {t_end:%Y-%m-%d %H:%M} UTC (últimas 4 h densas de "
                   f"{', '.join(req)}).")
            if not m2_ok:
                msg += " M2 no se exigió: no tiene ventana densa reciente."
            if dropped > 60:
                msg += (f" Se descartaron {dropped} lecturas posteriores, con muestreo escaso o "
                        "huecos: ESE tramo no se evaluó (use --tail para verlo).")
            print(msg)
        store = SeriesStore(max_hours=48)
        for r in readings:
            store.ingest(r)
        agent = CavaAgent(llm=object(), engine=engine, store=store, device_id=DEV)
        tool = agent._execute_tool

    print("\n== COBERTURA ==")
    print(f"controlador: {len(rc.included)} canales útiles de {rc.total_columns} columnas")
    print(f"multipower : {len(rm.included)} canales útiles de {rm.total_columns} columnas")
    chans = tool("list_channels", {})["canales"]
    print(f"canales visibles para el agente: {len(chans)}\n")
    print("== HERRAMIENTAS ==")

    # 1. stats por instrumento
    for ch in ("Temp Cámara", "M1 Potencia Activa", "Puerta Abierta"):
        r = tool("get_stats", {"canal": ch, "minutos": 60})
        check(f"get_stats {ch}", bool(r.get("ok")) and r.get("resultado") is not None,
              str(r.get("resultado") or r)[:150])

    # 2. energía: integración vs contador acumulado (verdad de referencia)
    e = tool("energy", {"canal": "M1 Potencia Activa", "minutos": 120})
    ser = [r for r in store.get_series(DEV, "M1 Energía Activa", minutes=120)
           if r.value is not None and r.quality.value == "VALID"]
    if len(ser) < 2:
        check("energy ≈ contador kWh (120 min)", False,
              f"el contador tiene {len(ser)} lecturas válidas en la ventana: no hay verdad de referencia")
        raise_after = True
    else:
        raise_after = False
    truth = (ser[-1].value - ser[0].value) if not raise_after else 0.0
    got = e.get("resultado")
    err = abs((got or 0) - truth) / truth * 100 if truth else 0
    if not raise_after:
      check("energy ≈ contador kWh (120 min)", got is not None and err < 10,
            f"herramienta={got} kWh | contador={truth:.2f} kWh | error={err:.0f} % | "
            f"cobertura={e.get('cobertura_pct')} % | {str(e.get('alternativa') or '')}")

    # 3. pendiente
    s = tool("get_slope", {"canal": "M1 Potencia Activa", "minutos": 120})
    check("get_slope M1 Potencia Activa", s.get("resultado") is not None, str(s.get("resultado"))[:120])

    # 4. correlaciones
    pairs = [
        ("mismo instrumento", "M1 Potencia Activa", "M1 Potencia Aparente"),
        ("entre medidores", "M1 Potencia Activa", "M2 Potencia Activa"),
        ("controlador vs multipower", "Estado Refrigeración", "M1 Potencia Activa"),
    ]
    for label, a, b in pairs:
        if not m2_ok and "M2" in (a + b):
            skip(f"correlate {label}", "M2 no tiene datos densos en la ventana evaluada")
            continue
        r = tool("correlate", {"a": a, "b": b, "minutos": 240})
        n = r.get("puntos_alineados", 0)
        extra = f" | {r['advertencia_muestra']}" if r.get("advertencia_muestra") else ""
        check(f"correlate {label}", r.get("resultado") is not None and n >= 20,
              f"puntos_alineados={n} resultado={r.get('resultado')}{extra}")

    # 5. serie constante (mismo instrumento, así que SÍ se alinea): no debe
    #    reportarse como "0.0 = sin relación"; la correlación no está definida.
    r = tool("correlate", {"a": "Temp Cámara", "b": "Estado Deshielo", "minutos": 900})
    check("correlate con serie constante no devuelve 0.0 engañoso",
          r.get("resultado") != 0.0,
          f"puntos_alineados={r.get('puntos_alineados')} resultado={r.get('resultado')}")

    # 6. antigüedad de la ventana (canal que dejó de reportar)
    r = tool("get_stats", {"canal": "M1 Potencia Activa", "minutos": 30})
    check("get_stats informa hasta cuándo llegan los datos",
          any(k in r for k in ("hasta", "antiguedad_min", "ultima_lectura")),
          f"claves devueltas={list(r.keys())}")

    # 7. falsos 'flat_signal' sobre canales constantes por diseño
    det = AnomalyDetector(engine=engine, config=cfg)
    series = {ch: store.get_series(DEV, ch, minutes=180) for ch in chans}
    flat = sorted(a.channel for a in det.detect(series) if a.kind == "flat_signal")
    by_design = {"Setpoint", "Desvío vs Setpoint", "Salida HOT", "Estado Deshielo",
                 "Estado Refrigeración", "Puerta Abierta"}
    spurious = [c for c in flat if c in by_design or "Frecuencia" in c or "Factor" in c]
    check("flat_signal no marca canales constantes por diseño", not spurious,
          f"flat_signal en {len(flat)} canales; de ellos constantes-por-diseño: {spurious[:6]}")

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n== RESUMEN: {len(RESULTS) - len(bad)}/{len(RESULTS)} OK"
          f"{f' ({len(SKIPPED)} omitidas)' if SKIPPED else ''} ==")
    for n in bad:
        print("   FALLA:", n)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--recommended" in sys.argv, "--tail" in sys.argv)
