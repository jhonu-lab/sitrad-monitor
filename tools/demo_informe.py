"""
tools/demo_informe.py — lo que recibiría el ingeniero (solo termocupla).

    python tools/demo_informe.py [--hours 24] [--seed 3] [--setpoint 1.0]

Simula la cámara, corre el monitor de temperatura y muestra cada mensaje
(alerta / informe de episodio) y al final el informe del periodo.
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
from analysis.cooling_report import CoolingConfig, CoolingMonitor, render_period_report
from core.series_store import SeriesStore
from simulation.cava_random import CavaRandomSource

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--setpoint", type=float, default=1.0)
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    cava = dict(cfg["cava_colanta"]); dev = int(cava["device_id"])
    cava.update({"setpoint": a.setpoint, "truth_log": "/tmp/truth_demo.jsonl",
                 "normal_warmup_hours": 2.0})
    src = CavaRandomSource(cava, seed=a.seed)
    store = SeriesStore(max_hours=48)
    cc = CoolingConfig.from_dict({**cfg.get("cooling", {}), "camera_name": "Cava Colanta"})
    mon = CoolingMonitor(cc)
    t = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    src.poll(t); t0 = t
    for _ in range(int(a.hours * 3600 / 120)):
        t += timedelta(seconds=120)
        for r in src.poll(t).readings:
            store.ingest(r)
        g = lambda ch: store.get_series(dev, ch, minutes=48 * 60)
        for kind, text in mon.step(g("Temp Cámara"), g("Puerta Abierta"), g("Estado Deshielo"), g("Estado Refrigeración")):
            hh = (t - timedelta(hours=5)).strftime("%H:%M")
            print(f"\n[{hh}] {'-' * 8} mensaje Telegram ({kind}) {'-' * 8}\n{text}")
    g = lambda ch: store.get_series(dev, ch, minutes=48 * 60)
    print("\n" + "=" * 70 + "\nINFORME DEL PERIODO\n" + "=" * 70)
    print(render_period_report(g("Temp Cámara"), t0, t, cc, g("Puerta Abierta"), g("Estado Deshielo"), g("Estado Refrigeración"), g("Setpoint")))

if __name__ == "__main__":
    main()
