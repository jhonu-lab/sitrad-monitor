"""
tools/run_validation.py — validación rápida, sin Telegram ni LLM.

    python tools/run_validation.py [--days 4] [--seed 42] [--config config.json]
                                   [--out logs/validation]

Corre el mismo pipeline de análisis (SeriesStore -> QualityGuard ->
AnomalyDetector -> ReportPolicy -> assess_incident) sobre días simulados con
el reloj congelado. Guarda los casos y la verdad del simulador y al final
imprime el informe de tools/case_report.py.

La decisión sobre cada incidente (diagnóstico + silenciar) sale de
analysis.diagnosis.assess_incident, la MISMA función que usa el Reporter:
así lo medido aquí es lo que se enviaría en la demo.

Diferencias con la corrida en vivo (para no engañarse):
  * sin límite de mensajes por hora ni enfriamiento entre mensajes;
  * el LLM no interviene: el diagnóstico es el determinista.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analysis.anomaly_detector import AnomalyDetector
from analysis.baseline import BehaviorProfile
from analysis.diagnosis import assess_incident
from analysis.engine import AnalysisEngine
from core.case_log import CaseLog
from core.incident_tracker import DiagnosisTracker
from core.quality_guard import QualityGuard
from core.report_policy import ReportPolicy
from core.series_store import SeriesStore
from simulation.cava_random import CavaRandomSource

import case_report


def simulate(*, config="config.json", out="logs/validation", days=4.0, seed=42,
             tick_seconds=120, warmup_normal_h=2.5, quiet=False) -> tuple[str, str, int]:
    """Corre la simulación y devuelve (cases_path, truth_path, n_mensajes)."""
    cfg = json.load(open(config, encoding="utf-8"))
    acfg = dict(cfg.get("analysis", {}))
    acfg.update({"max_messages_per_hour": 100000, "policy_cooldown_seconds": 0})
    cava = dict(cfg["cava_colanta"])
    window_min = int(cfg.get("report_window_minutes", 60))
    dev = int(cava["device_id"])
    comp_channel = cfg.get("compressor_channel", "Estado Refrigeración")

    os.makedirs(out, exist_ok=True)
    truth_path = os.path.join(out, "sim_truth.jsonl")
    cases_path = os.path.join(out, "cases.jsonl")
    open(cases_path, "w", encoding="utf-8").close()

    cava["truth_log"] = truth_path
    cava["normal_warmup_hours"] = warmup_normal_h
    src = CavaRandomSource(cava, seed=seed)
    store = SeriesStore(max_hours=48)
    guard = QualityGuard(acfg)
    eng = AnalysisEngine(config=acfg)
    prof = BehaviorProfile(
        config={"learning_hours": acfg.get("learning_hours", 2.0),
                "tz_name": cfg.get("operations", {}).get("timezone", "America/Bogota")},
        analysis=eng)
    det = AnomalyDetector(engine=eng, config=acfg)
    pol = ReportPolicy(config=acfg)
    cases = CaseLog(cases_path)

    t = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)
    src.poll(t)
    ticks = int(days * 86400 / tick_seconds)
    n_inc = 0
    suppress_benign = bool(acfg.get("suppress_benign_incidents", False))
    suppress_unexpl = bool(acfg.get("suppress_unexplained_incidents", True))
    unexpl_sev = float(acfg.get("unexplained_max_severity", 0.8))
    suppressed_inc: set[str] = set()
    track_on = bool(acfg.get("diagnosis_updates", True))
    tracker = DiagnosisTracker(
        every_s=float(acfg.get("diagnosis_update_every_seconds", 240)),
        confirm=int(acfg.get("diagnosis_update_confirm", 2)),
        min_confidence=float(acfg.get("diagnosis_update_min_confidence", 0.5)))

    def assess(series, kind, sev):
        refs = None
        bp = prof.get_binary(comp_channel)
        if bp is not None:
            refs = (bp.on_p95_min, bp.off_p95_min)
        return assess_incident(
            series, incident_kind=kind, severity=sev,
            levels=det.consistency_levels(), compressor_refs=refs,
            suppress_benign=suppress_benign, suppress_unexplained=suppress_unexpl,
            unexplained_max_severity=unexpl_sev)

    for i in range(ticks):
        t += timedelta(seconds=tick_seconds)
        for r in src.poll(t).readings:
            store.ingest(guard.apply(r))
        series = {k[len(f"{dev}:"):]: v
                  for k, v in store.all_series(minutes=window_min).items()
                  if k.startswith(f"{dev}:")}
        for rs in series.values():
            prof.ingest_many(rs)
        if i % 5 == 0:
            prof.fit()
        act = {}
        for s in det.detect(series, prof):
            p = act.get(s.channel)
            if p is None or s.severity > p.severity:
                act[s.channel] = s
        for m in pol.decide_anomalies(act, t):
            kind = getattr(m.kind, "value", str(m.kind))
            ikey = str(m.key).split(":")[1] if ":" in str(m.key) else str(m.key)
            if "incident_kind" in m.payload and kind == "RESOLUCION":
                tracker.drop(ikey)
                suppressed_inc.discard(ikey)
            elif "incident_kind" in m.payload:
                res = assess(series, m.payload.get("incident_kind"),
                             m.payload.get("severity_max"))
                silenced = res.silenced
                # Si una escalada ya no se silencia, el incidente se envía desde ahí.
                if silenced:
                    suppressed_inc.add(ikey)
                elif ikey in suppressed_inc and not str(m.key).startswith("incident_open"):
                    suppressed_inc.discard(ikey)
                tracker.register(ikey, res.bundle.principal, announced=not silenced)
                cases.record(key=m.key, payload=m.payload, bundle=res.bundle, now=t,
                             suppressed=silenced)
                n_inc += 1
        if track_on:
            opened = pol.open_incidents
            for u in tracker.check(
                    t, opened.keys(),
                    lambda k, series=series, opened=opened: assess(
                        series, opened[k]["kind"], opened[k]["severity_max"]),
                    lambda k, opened=opened: opened[k]):
                inc = u.incident
                payload = {"incident_kind": inc["kind"],
                           "channels": sorted(inc["channels"]),
                           "opened_at": inc["opened_at"].isoformat(),
                           "severity_max": inc["severity_max"]}
                suppressed_inc.discard(u.ikey)
                cases.record(key=f"incident_update:{u.ikey}:{t.isoformat()}",
                             payload=payload, bundle=u.assessment.bundle, now=t,
                             suppressed=False)
                n_inc += 1
    if not quiet:
        print(f"Simulados {days:g} días ({ticks} lecturas por canal), "
              f"{n_inc} mensajes de incidente. Archivos en {out}/\n")
    return cases_path, truth_path, n_inc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="logs/validation")
    ap.add_argument("--tick-seconds", type=int, default=120)
    ap.add_argument("--warmup-normal-h", type=float, default=2.5)
    a = ap.parse_args()
    cases_path, truth_path, _ = simulate(
        config=a.config, out=a.out, days=a.days, seed=a.seed,
        tick_seconds=a.tick_seconds, warmup_normal_h=a.warmup_normal_h)
    case_report.main(["--cases", cases_path, "--truth", truth_path,
                      "--labels", os.path.join(a.out, "case_labels.jsonl"),
                      "--warmup-h", str(a.warmup_normal_h)])


if __name__ == "__main__":
    main()
