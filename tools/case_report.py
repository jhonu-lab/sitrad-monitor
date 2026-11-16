"""
tools/case_report.py — ¿acierta el diagnóstico?

    python tools/case_report.py [--cases logs/cases.jsonl]
                                [--truth logs/sim_truth.jsonl]
                                [--labels logs/case_labels.jsonl]
                                [--warmup-h 2] [--tail-min 20] [--target 0.8]

Compara los casos que el bot diagnosticó con lo que REALMENTE ocurrió:
  * verdad del simulador (--truth), o
  * etiquetas manuales (--labels, ver tools/label_case.py), que mandan.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

NORMAL_NAMES = {"normal", "entrada_producto", "carga_grande"}

# Hipótesis que describen operación normal: un incidente cuya hipótesis
# principal es una de ellas es una alerta que el propio bot explica como normal.
BENIGN = {"deshielo_programado", "recuperacion_post_deshielo", "carga_grande",
          "entrada_producto"}


def _read(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _t(s):
    return datetime.fromisoformat(s)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="logs/cases.jsonl")
    ap.add_argument("--truth", default="logs/sim_truth.jsonl")
    ap.add_argument("--labels", default="logs/case_labels.jsonl")
    ap.add_argument("--warmup-h", type=float, default=0.0)
    ap.add_argument("--tail-min", type=float, default=20.0)
    ap.add_argument("--target", type=float, default=0.8)
    ap.add_argument("--consequence-min", type=float, default=30.0,
                    help="un incidente que abre hasta N min después de terminar una "
                         "falla, dentro de un episodio normal, es consecuencia de ella "
                         "(cámara aún recuperándose) y no cuenta como falsa alarma")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    P = (lambda *x, **k: None) if a.quiet else print

    raw = _read(a.cases)
    truth = [e for e in _read(a.truth) if "start" in e and "end" in e]   # sin marcadores
    labels = {r["case_id"]: r for r in _read(a.labels)}

    P(f"Leído: {len(raw)} registros en {a.cases} · {len(truth)} episodios en {a.truth}")
    if not raw:
        P("  -> No hay casos. Si hubo alertas en el chat, CaseLog no está conectado:\n"
              "     revisa reporter.case_log en run_reporter.py y el hook en reporter.py.")
    if not truth and not labels:
        P("  -> No hay verdad (ni sim_truth.jsonl ni etiquetas): no se puede evaluar.")

    # Un caso = un incidente. Se evalúa el diagnóstico al abrirse.
    cases: dict[str, dict] = {}
    for r in raw:
        c = cases.setdefault(r["case_id"], {"open": None, "last": None})
        if r["stage"] == "open" and c["open"] is None:
            c["open"] = r
        c["last"] = r
    for c in cases.values():
        c["open"] = c["open"] or c["last"]

    # Episodios evaluables (se descarta el aprendizaje inicial de cada corrida).
    episodes, run_start = [], None
    for e in truth:
        if e.get("first"):
            run_start = _t(e["start"])
        if run_start and _t(e["start"]) < run_start + timedelta(hours=a.warmup_h):
            continue
        episodes.append({**e, "t0": _t(e["start"]), "t1": _t(e["end"])})
    fuera_alcance: list[str] = []
    tail = timedelta(minutes=a.tail_min)

    def episode_at(t):
        best = None
        for e in episodes:
            if e["t0"] - timedelta(minutes=2) <= t <= e["t1"] + tail:
                if best is None or e["t0"] > best["t0"]:
                    best = e
        return best

    # ---- casos -> verdad ----
    rows = []
    consecuencias: list[str] = []
    for cid, c in cases.items():
        o = c["open"]
        t = _t(o["opened_at"]) if o.get("opened_at") else _t(o["ts"])
        ep = episode_at(t)
        if cid in labels:
            real, src = labels[cid]["real_cause"], "etiqueta"
        elif ep is not None and ep.get("scope") == "informe":
            fuera_alcance.append(cid)      # lo evalúa el informe de temperatura, no el diagnóstico
            continue
        elif ep is not None:
            real, src = ("sin_falla" if ep["name"] in NORMAL_NAMES else ep["name"]), "simulador"
            if real == "sin_falla" and a.consequence_min > 0:
                prev = [x for x in episodes if x.get("anomalo") and x["t1"] <= ep["t0"]
                        and t - x["t1"] <= timedelta(minutes=a.consequence_min)]
                if prev:
                    consecuencias.append(cid)
                    continue
        else:
            continue          # fuera del período evaluable (aprendizaje)
        rows.append({"case_id": cid, "t": t, "real": real, "src": src,
                     "dx": o["principal"], "conf": o["confianza"],
                     "dx_last": c["last"]["principal"], "ep": ep,                     
                     "silenciado": bool(o.get("suppressed"))
                     and not (c["last"]["stage"] == "escalate"
                              and not c["last"].get("suppressed"))},
                     )

    skipped_cases = len(cases) - len(rows) - len(consecuencias) - len(fuera_alcance)
    if a.warmup_h > 0:
        P(f"Excluido por aprendizaje (--warmup-h {a.warmup_h:g}): "
              f"{len(truth) - len(episodes)} episodios, {skipped_cases} casos")
    if fuera_alcance:
        P(f"Incidentes en episodios del informe de temperatura (no evalúa el diagnóstico): {len(fuera_alcance)}")
    if consecuencias:
        P(f"Incidentes por consecuencia de una falla previa (no cuentan como falsa alarma, "
          f"--consequence-min {a.consequence_min:g}): {len(consecuencias)}")
    silenciados = [r for r in rows if r["silenciado"]]
    if silenciados:
        P(f"Incidentes silenciados (hipótesis benigna, registrados pero no enviados): "
              f"{len(silenciados)}")
    rows = [r for r in rows if not r["silenciado"]]   # lo que llegó al usuario
    fallas = [r for r in rows if r["real"] != "sin_falla"]
    falsas = [r for r in rows if r["real"] == "sin_falla"]
    correctos = [r for r in fallas if r["dx"] == r["real"]]

    # ---- episodios anómalos: detectados / perdidos ----
    anom = [e for e in episodes if e.get("anomalo") and e.get("scope") != "informe"]
    det, perdidos = [], []
    for e in anom:
        hit = [r for r in rows if r["ep"] is e and r["real"] != "sin_falla"]
        (det if hit else perdidos).append((e, hit))
    ok_ep = sum(1 for e, hit in det if any(r["dx"] == e["name"] for r in hit[:1]))

    P("=" * 66)
    P("VALIDACIÓN DEL DIAGNÓSTICO")
    P("=" * 66)
    P(f"Episodios de falla evaluados:     {len(anom)}")
    if anom:
        P(f"  detectados (hubo incidente):    {len(det)}  ({len(det) / len(anom):.0%})")
        P(f"  diagnosticados correctamente:   {ok_ep}  ({ok_ep / len(anom):.0%} de los inyectados)")
    P(f"Casos diagnosticados (incidentes): {len(rows)}")
    if fallas:
        P(f"  sobre una falla real:           {len(fallas)}  "
              f"-> diagnóstico correcto {len(correctos)}/{len(fallas)} "
              f"({len(correctos) / len(fallas):.0%})")
    sin_causa = sum(1 for r in falsas if r["dx"] == "indeterminado")
    P(f"  falsas alarmas (no había falla): {len(falsas)}"
          f"  ({sin_causa} sin causa identificada, {len(falsas) - sin_causa} con causa errónea)")
    if rows:
        acc = len(correctos) / len(rows)
        P(f"\nAcierto sobre todos los casos:    {acc:.1%}  "
              f"(meta {a.target:.0%}; las falsas alarmas cuentan como error)")
        verdict = "CUMPLE" if acc >= a.target else "NO CUMPLE"
        P(f"Meta de {a.target:.0%}: {verdict}  — con {len(rows)} casos"
              f"{' (se pedían ~20)' if len(rows) < 20 else ''}")

    benignos = [r for r in rows if r["dx"] in BENIGN]
    if benignos and rows:
        resto = len(rows) - len(benignos)
        acc2 = len([r for r in correctos if r["dx"] not in BENIGN]) / resto if resto else 0.0
        P(f"\nSi se silenciaran los {len(benignos)} incidentes cuya hipótesis principal es "
              f"operación normal ({', '.join(sorted({r['dx'] for r in benignos}))}):")
        P(f"  quedarían {resto} casos y el acierto sería {acc2:.1%}")

    P("\nMatriz de confusión (real -> diagnóstico principal):")
    conf = defaultdict(Counter)
    for r in rows:
        conf[r["real"]][r["dx"]] += 1
    for real in sorted(conf):
        P(f"  {real:24s} {dict(conf[real])}")

    if perdidos:
        P("\nFallas inyectadas SIN incidente:")
        for e, _ in perdidos:
            P(f"  {e['name']:24s} {e['start'][:16]} ({(e['t1'] - e['t0']).total_seconds() / 60:.0f} min)")
    bad = [r for r in fallas if r["dx"] != r["real"]]
    if bad:
        P("\nDiagnósticos incorrectos:")
        for r in bad:
            P(f"  {r['t']:%Y-%m-%d %H:%M}  real={r['real']}  dijo={r['dx']} ({r['conf']:.2f})")
    if falsas:
        P("\nFalsas alarmas:")
        for r in falsas:
            P(f"  {r['t']:%Y-%m-%d %H:%M}  dijo={r['dx']} ({r['conf']:.2f})")

    return {
        "episodios": len(anom), "detectados": len(det), "dx_ok_episodios": ok_ep,
        "casos": len(rows), "casos_falla": len(fallas), "casos_ok": len(correctos),
        "falsas": len(falsas), "silenciados": len(silenciados),
        "consecuencias": len(consecuencias),
        "acierto": (len(correctos) / len(rows)) if rows else 0.0,
        "recall": (len(det) / len(anom)) if anom else 0.0,
        "dx_episodios": (ok_ep / len(anom)) if anom else 0.0,
        "perdidos": [(e["name"], e["start"][:16]) for e, _ in perdidos],
        "incorrectos": [(r["real"], r["dx"]) for r in bad],
        "falsas_dx": [r["dx"] for r in falsas],
    }


if __name__ == "__main__":
    main()