"""
tools/smoke_reporter.py — prueba de humo del Reporter COMPLETO, sin Telegram ni LLM.

    python tools/smoke_reporter.py [--hours 6] [--ask "temperatura actual" --ask "..."]

Corre el Reporter real ciclo a ciclo con reloj congelado y un notificador de
consola que guarda lo enviado. Comprueba que (1) el ciclo no lanza excepciones,
(2) las preguntas se responden. Sale con código 1 si algo falla.
Si el proyecto tiene FileLog / HistoricalMemory reales se usan; si no, stubs.
"""
from __future__ import annotations
import argparse, json, logging, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
from analysis.anomaly_detector import AnomalyDetector
from analysis.baseline import BehaviorProfile
from analysis.engine import AnalysisEngine
from analysis.event_engine import EventEngine
from agent.cava_agent import CavaAgent
from agent.context_serializer import ContextSerializer
from agent.demo_llm import DemoLLM
from core.clock import FrozenClock
from core.quality_guard import QualityGuard
from core.report_policy import ReportPolicy
from core.reporter import Reporter
from core.series_store import SeriesStore
from core.channel_catalog import ROLE_TO_CHANNEL
from notifiers.console_notifier import ConsoleNotifier
from simulation.cava_random import CavaRandomSource


class _Permissive:
    """Stub: cualquier método devuelve lista vacía."""
    def __getattr__(self, name):
        return lambda *a, **k: []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--ask", action="append", default=["temperatura actual"])
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--scenario", default="random")
    ap.add_argument("--setpoint", type=float, default=1.0)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--ask-at-min", type=int, default=None)
    ap.add_argument("--cases-dir", default=None, help="carpeta para cooling_cases.jsonl y feedback.jsonl")
    ap.add_argument("--dump-cases", action="store_true")
    ap.add_argument("--excel", default=None, help="repetir este historial de SITRAD en vez del simulador")
    ap.add_argument("--step", type=int, default=60, help="segundos de datos por ciclo")
    ap.add_argument("--api", default=None, help="URL de una API SITRAD (p. ej. el servidor falso); usa --excel solo para la hora de inicio")
    ap.add_argument("--products", default=None, help="products.json a cargar al iniciar")
    ap.add_argument("--rule", default="ternera=120", help="producto=minutos máximos sobre el límite (ejemplo para la demo)")
    a = ap.parse_args()

    errors: list[str] = []

    class _Catch(logging.Handler):
        def emit(self, rec):
            if rec.levelno >= logging.ERROR:
                errors.append(f"{rec.name}: {rec.getMessage()}")
    logging.getLogger().addHandler(_Catch())
    logging.getLogger().setLevel(logging.ERROR)

    cfg = json.load(open(a.config, encoding="utf-8"))
    cfg.setdefault("cooling", {})
    if a.rule:
        prod, mx = a.rule.split("=")
        cfg["cooling"].setdefault("product_rules", {})[prod] = {"max_minutes_above": float(mx)}
    cava = dict(cfg["cava_colanta"]); dev = int(cfg["cava_colanta"]["device_id"])
    cava.update({"truth_log": "/tmp/smoke_truth.jsonl", "normal_warmup_hours": 1.0, "setpoint": a.setpoint})
    if a.scenario != "random":
        from simulation.cava_random import SCENARIO_PRESETS
        cava["scenario_script"] = SCENARIO_PRESETS[a.scenario]
    acfg = dict(cfg.get("analysis", {}))
    if a.api:
        from simulation.excel_replay import load_sitrad_table
        from sources.sitrad_api import SitradApi, SitradApiSource
        rows0, _ = load_sitrad_table(a.excel)
        source = SitradApiSource(SitradApi(a.api, "u", "p"), dev, poll_seconds=0, alarms_every_s=0, save_raw=False)
        source.discover()
        clock = FrozenClock(rows0[0][0])
        a.hours = a.hours if a.hours else 6.0
    elif a.excel:
        from simulation.excel_replay import ExcelReplaySource
        source = ExcelReplaySource(a.excel, dev, cfg.get("operations", {}).get("timezone", "America/Bogota"),
                                   stop_at_end=False)
        clock = FrozenClock(source.start_time)
        a.hours = ((source.end_time - source.start_time).total_seconds() + 600) / 3600.0
    else:
        clock = FrozenClock(datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc))
        source = CavaRandomSource(cava, seed=a.seed)
    store = SeriesStore(max_hours=24)
    engine = AnalysisEngine(config=acfg)
    profile = BehaviorProfile(config={"learning_hours": 1.0,
                                      "tz_name": "America/Bogota"}, analysis=engine)
    detector = AnomalyDetector(engine=engine, config=acfg)
    guard = QualityGuard(acfg)
    policy = ReportPolicy(config=acfg)
    event_engine = EventEngine(analysis=engine, config=acfg)
    serializer = ContextSerializer()
    agent = CavaAgent(llm=DemoLLM(), engine=engine, store=store, context_serializer=serializer,
                      memory=_Permissive(), device_id=dev, clock=clock,
                      levels_provider=detector.consistency_levels)
    notifier = ConsoleNotifier()
    notifier.print = lambda *x, **k: None
    rcfg = {
        "polling_seconds": 5, "channels_que_disparan_eventos": [ROLE_TO_CHANNEL["temp_principal"]],
        "device_name": cava.get("device_name", "Cava"), "device_id": dev,
        "compressor_channel": ROLE_TO_CHANNEL["compresor"], "analysis": acfg,
        "cooling": {**cfg.get("cooling", {}), "products_path": a.products,
                                  "cases_path": (a.cases_dir + "/cooling_cases.jsonl") if a.cases_dir else None,
                                  "feedback_path": (a.cases_dir + "/feedback.jsonl") if a.cases_dir else None}, "operations": cfg.get("operations", {}),
        "report_window_minutes": cfg.get("report_window_minutes", 60),
        "stale_channel_minutes": 5, "warmup_on_start": False,
    }
    rep = Reporter(source=source, store=store, event_engine=event_engine, agent=agent,
                   context_serializer=ContextSerializer(), telegram=notifier,
                   filelog=_Permissive(), clock=clock, config=rcfg, profile=profile,
                   detector=detector, quality_guard=guard, policy=policy)

    steps = int(a.hours * 3600 / a.step)        # un ciclo = a.step segundos de datos
    asked_at = {(a.ask_at_min if a.ask_at_min is not None else steps // 2): list(a.ask)}
    pending: dict[str, int] = {}
    for i in range(steps):
        clock.advance(timedelta(seconds=a.step))
        # varias lecturas por ciclo para que el almacén tenga cadencia de 2 min
        try:
            rep.run_one_cycle(clock.now())
        except Exception as exc:                     # un ciclo NUNCA debe lanzar
            errors.append(f"ciclo {i}: {exc!r}")
            break
        if i in asked_at:
            for q in asked_at[i]:
                n0 = len(notifier.sent)
                notifier.ask(q)
                rep.run_one_cycle(clock.now())
                pending[q] = len(notifier.sent) - n0
    if a.show:
        for m in notifier.sent:
            print("-" * 60); print(m)
    if a.dump_cases:
        print("\nCASOS REGISTRADOS:")
        for c in sorted(rep.case_book.cases.values(), key=lambda c: c["start"]):
            print(f"  {rep.case_book._local(c['start']).strftime('%H:%M')} {c['type']:16s} enviado={str(c.get('reported')):5s} bot={c['bot_kind']}")
    print(f"\nCiclos: {steps}  mensajes enviados: {len(notifier.sent)}")
    ok = True
    for q, n in pending.items():
        print(f"  pregunta {q!r}: {n} mensaje(s) de respuesta")
        ok &= n > 0
    if errors:
        ok = False
        print("\nERRORES:")
        for e in errors[:10]:
            print("  ", e)
    print("\nRESULTADO:", "OK" if ok else "FALLA")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
