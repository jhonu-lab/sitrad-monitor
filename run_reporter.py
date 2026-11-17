from __future__ import annotations
# run_reporter
import argparse
from pathlib import Path
import json
import logging
import os
import sys
from core.logging_filters import RedactTokens as _RedactTokens
from notifiers.retry_notifier import RetryNotifier

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURACIÓN
# ============================================================================

def load_config(path: str) -> dict:
    if not os.path.isabs(path) and not os.path.exists(path):
        alt = _base_dir() / path
        if alt.exists():
            path = str(alt)
    if not os.path.exists(path):
        print(f"ERROR: no existe el archivo de configuración: {path}", file=sys.stderr)
        raise SystemExit(1)
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    except json.JSONDecodeError as exc:
        print(
            f"ERROR: configuración JSON inválida en {path}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)


# ============================================================================
# VALIDACIÓN
# ============================================================================

def validate_config(cfg: dict, mode: str) -> None:
    """
    Comprueba que existan las secciones mínimas necesarias.

    No intenta validar todos los parámetros del sistema.
    Cada componente es responsable de validar sus propios parámetros.
    """

    required_sections = (
        "cava_colanta",
        "storage",
        "analysis",
        "llm",
        "telegram",
    )

    for section in required_sections:
        if section not in cfg:
            raise SystemExit(
                f"ERROR: falta la sección '{section}' en config.json."
            )

    cava_config = cfg["cava_colanta"]

    # En modo SITRAD, sustituye la lista del simulador por el catálogo
    # real (mismo `device_id`, mismos nombres canónicos que emite
    # tools/sitrad_excel.py y que esperan las reglas de diagnóstico).
    
    # La arquitectura multidimensional necesita canales en ambos modos.
    # Se usa el catálogo canónico compartido para evitar listas duplicadas.
    from core.channel_catalog import channels_config

    if mode == "sitrad" or "channels" not in cava_config:
        channels = channels_config()

        # Actualizar la configuración original es importante:
        # main() vuelve a leer cfg["cava_colanta"] después de validar.
        cfg["cava_colanta"]["channels"] = channels
        cava_config = cfg["cava_colanta"]

        logger.info(
            "Catálogo de canales cargado para modo %s: %d canales.",
            mode,
            len(channels),
        )


    if "device_id" not in cava_config:
        raise SystemExit(
            "ERROR: falta 'cava_colanta.device_id'."
        )

    if "device_name" not in cava_config:
        raise SystemExit(
            "ERROR: falta 'cava_colanta.device_name'."
        )

    if "channels" not in cava_config:
        raise SystemExit(
            "ERROR: falta 'cava_colanta.channels'. "
            "La arquitectura nueva utiliza múltiples canales."
        )

    channels = cava_config["channels"]

    if not isinstance(channels, list):
        raise SystemExit(
            "ERROR: 'cava_colanta.channels' debe ser una lista."
        )

    if not channels:
        raise SystemExit(
            "ERROR: 'cava_colanta.channels' no puede estar vacía."
        )

    for index, channel in enumerate(channels):
        if not isinstance(channel, dict):
            raise SystemExit(
                f"ERROR: 'cava_colanta.channels[{index}]' "
                "debe ser un objeto JSON."
            )
        if "name" not in channel:
            raise SystemExit(
                f"ERROR: 'cava_colanta.channels[{index}].name' "
                "es obligatorio."
            )

    if mode == "sitrad":
        if "sitrad" not in cfg:
            raise SystemExit(
                "ERROR: falta la sección 'sitrad' para el modo real (puede ir vacía: {})."
            )

        sitrad_config = cfg["sitrad"]
        sitrad_config.setdefault("base_url", "https://localhost:8004/api/v1")


# ============================================================================
# MAIN
# ============================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Monitor multidimensional de Sitrad Pro."
    )
    parser.add_argument(
        "--mode",
        choices=("demo", "sitrad"),
        default=None,
        help="Modo de ejecución del bot. Si no se especifica, usa config.json.",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Ruta del archivo de configuración.",
    )
    parser.add_argument(
        "--inspect-excel",
        metavar="PATH",
        default=None,
        help="Analiza un Excel de SITRAD y reporta instrumento y canales. No arranca el bot.",
    )
    parser.add_argument(
        "--inspect-api",
        action="store_true",
        help="Consulta SITRAD y reporta convertidores, instrumentos y canales. No arranca el bot.",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Con --inspect-excel, guarda el informe en JSON en esta ruta.",
    )
    return parser.parse_args()


def _base_dir() -> Path:
    """Carpeta donde viven config.json y logs/. Junto al .exe si está empaquetado,
    o junto al script si se ejecuta con python."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def main() -> None:
    args = parse_args()

    # ── Modos de inspección (no arrancan el bot) ──
    if args.inspect_excel:
        from tools.inspect_excel import run_inspect_excel
        rc = run_inspect_excel(args.inspect_excel, args.config, args.output)
        raise SystemExit(rc)

    if args.inspect_api:
        from tools.inspect_sitrad_api import main as inspect_api_main
        sys.argv = [sys.argv[0], "--config", args.config]
        rc = inspect_api_main()
        raise SystemExit(rc)

    # ── Modo bot (lo que ya tienes) ──
    cfg = load_config(args.config)

    # El argumento CLI tiene prioridad sobre config.json.
    mode = args.mode or cfg.get("mode", "demo")

    validate_config(cfg, mode)

    # ------------------------------------------------------------------
    # Operación continua (debe definirse ANTES de BehaviorProfile y
    # ReportPolicy, que consumen tz_name y ops).
    # ------------------------------------------------------------------

    ops = cfg.get("operations", {})
    rotation = ops.get("rotation", {})
    tz_name = ops.get("timezone", "America/Bogota")

    logger.info(
        "Configuración cargada: %s",
        args.config,
    )

    logger.info(
        "Modo seleccionado: %s",
        mode,
    )

    # ------------------------------------------------------------------
    # Tiempo
    # ------------------------------------------------------------------

    from core.clock import Clock

    excel_source = None
    if mode == "demo" and cfg.get("simulation", {}).get("scenario") in ("excel", "jsonl"):
        # Repetición de un historial real de SITRAD (Excel exportado o lecturas.jsonl del extractor):
        # el reloj parte del primer dato del archivo.
        from simulation.excel_replay import ExcelReplaySource, ReplayClock

        simulation_config = cfg["simulation"]
        if simulation_config["scenario"] == "excel":
            if not simulation_config.get("excel_path"):
                raise SystemExit("ERROR: con simulation.scenario='excel' hay que indicar simulation.excel_path.")
            excel_source = ExcelReplaySource(
                path=simulation_config["excel_path"],
                device_id=int(cfg["cava_colanta"]["device_id"]),
                tz_name=cfg.get("operations", {}).get("timezone", "America/Bogota"),
                date_order=simulation_config.get("date_order", "auto"),
                stop_at_end=bool(simulation_config.get("stop_at_end", True)),
            )
        else:
            from simulation.jsonl_replay import JsonlReplaySource

            if not simulation_config.get("jsonl_path"):
                raise SystemExit("ERROR: con simulation.scenario='jsonl' hay que indicar simulation.jsonl_path.")
            excel_source = JsonlReplaySource(
                path=simulation_config["jsonl_path"],
                device_id=int(cfg["cava_colanta"]["device_id"]),
                instrument_id=cfg.get("sitrad", {}).get("instrument_id"),
                instrument_name=cfg.get("sitrad", {}).get("instrument_name"),
                channel_map=cfg.get("sitrad", {}).get("channel_map"),
                stop_at_end=bool(simulation_config.get("stop_at_end", True)),
            )
        speed = float(simulation_config.get("speed", 24.0))
        if speed <= 0:
            raise SystemExit("ERROR: simulation.speed debe ser mayor que cero.")
        clock = ReplayClock(excel_source.start_time, speed)
        logger.info(
            "Repetición de historial: %s → %s a %.1fx (%.1f s de datos por segundo real).",
            excel_source.info["primera"].strftime("%d/%m/%Y %H:%M"),
            excel_source.info["ultima"].strftime("%d/%m/%Y %H:%M"), speed, speed,
        )
    elif mode == "demo":
        from simulation.speed_clock import SpeedClock

        simulation_config = cfg.get("simulation", {})
        speed = float(simulation_config.get("speed", 12.0))

        if speed <= 0:
            raise SystemExit(
                "ERROR: simulation.speed debe ser mayor que cero."
            )

        clock = SpeedClock(speed=speed)

        logger.info(
            "Reloj acelerado activo: %.1fx. "
            "Una hora simulada equivale a %.1f minutos reales.",
            speed,
            60.0 / speed,
        )
    else:
        clock = Clock()
        logger.info("Reloj real activo para SITRAD.")

    # ------------------------------------------------------------------
    # Fuente de datos
    # ------------------------------------------------------------------

    cava_config = cfg["cava_colanta"]

    if mode == "demo" and excel_source is not None:
        source = excel_source
        logger.info("Fuente: historial de SITRAD (%s), %d filas.",
                    cfg["simulation"].get("excel_path") or cfg["simulation"].get("jsonl_path"),
                    excel_source.info["filas"])

    elif mode == "demo":

        from simulation.cava_random import (
            CavaRandomSource,
            SCENARIO_PRESETS,
            scenario_names,
        )
        from simulation.speed_clock import SpeedClock

        simulation_config = cfg.get("simulation", {})
        scenario = simulation_config.get("scenario", "random")
        sim_cfg = dict(cava_config)

        # Un solo generador para toda la simulación:
        #   "random" ........ episodios aleatorios etiquetados (verdad en JSONL)
        #   <preset> ........ guion reproducible (ver SCENARIO_PRESETS)
        #   "script" ........ guion propio en simulation.script
        if scenario in ("random", "random_labeled"):
            pass
        elif scenario in SCENARIO_PRESETS:
            sim_cfg["scenario_script"] = SCENARIO_PRESETS[scenario]
        elif scenario == "script" and simulation_config.get("script"):
            sim_cfg["scenario_script"] = simulation_config["script"]
        else:
            raise SystemExit(
                f"simulation.scenario='{scenario}' no existe. "
                f"Opciones: {', '.join(scenario_names())} o 'script'."
            )

        source = CavaRandomSource(
            config=sim_cfg,
            seed=simulation_config.get("seed", 42),
            min_minutes=simulation_config.get("random_min_minutes"),
            max_minutes=simulation_config.get("random_max_minutes"),
        )
        logger.info("Fuente simulada: CavaRandomSource, escenario=%s.", scenario)

    else:

        from sources.sitrad_api import SitradApi, SitradApiSource, resolve_credentials

        sitrad_config = cfg.get("sitrad", {})
        user, password = resolve_credentials(sitrad_config)
        api = SitradApi(
            base_url=sitrad_config.get("base_url", "https://localhost:8004/api/v1"),
            user=user,
            password=password,
            verify_ssl=sitrad_config.get("verify_ssl", False),
            timeout=sitrad_config.get("timeout", 30),
        )
        source = SitradApiSource(
            api,
            device_id=int(cava_config["device_id"]),
            instrument_id=sitrad_config.get("instrument_id"),
            instrument_name=sitrad_config.get("instrument_name"),
            channel_map=sitrad_config.get("channel_map"),
            poll_seconds=float(sitrad_config.get("poll_seconds", 30)),
            alarms_every_s=float(sitrad_config.get("alarms_every_seconds", 60)),
            save_raw=bool(sitrad_config.get("save_raw", True)),
            data_dir=sitrad_config.get("data_dir", "sitrad_data"),
            announce_existing_alarms=bool(sitrad_config.get("announce_existing_alarms", False)),
            passthrough=bool(sitrad_config.get("passthrough", True)),   # ← NUEVO
        )
        try:
            source.discover()
            if mode == "sitrad" and hasattr(source, "discover_all"):
                try:
                    tree = source.discover_all()
                    logger.info(
                        "SITRAD: %d convertidor(es), %d instrumento(s), %d canales efectivos "
                        "(%d catalogados + %d passthrough).",
                        tree.get("n_converters", 0),
                        tree.get("n_instruments", 0),
                        tree.get("n_channels_effective", 0),
                        tree.get("n_channels_catalogados", 0),
                        tree.get("n_channels_passthrough", 0),
                    )
                    for c in tree.get("converters", []):
                        for ins in c.get("instruments", []):
                            logger.info("  · convertidor %s → %s (id=%s, %d canales)",
                                        c.get("name"), ins.get("name"), ins.get("id"),
                                        ins.get("n_channels_effective", 0))
                    for ins in tree.get("unassigned_instruments", []):
                        logger.info("  · (sin convertidor) %s (id=%s, %d canales)",
                                    ins.get("name"), ins.get("id"),
                                    ins.get("n_channels_effective", 0))
                except Exception:
                    logger.exception("Descubrimiento SITRAD falló; sigo con el instrumento configurado.")
        except Exception as exc:
            raise SystemExit(f"ERROR al conectar con SITRAD: {exc}")

        logger.info("Fuente seleccionada: API local de SITRAD (instrumento %s).", source.instrument_id)

    # ------------------------------------------------------------------
    # Almacenamiento temporal
    # ------------------------------------------------------------------

    from core.series_store import SeriesStore

    storage_config = cfg["storage"]

    max_hours = storage_config.get(
        "max_hours",
        24,
    )

    store = SeriesStore(
        max_hours=max_hours,
    )

    logger.info(
        "SeriesStore inicializado con %s horas de historial.",
        max_hours,
    )

    # ------------------------------------------------------------------
    # Motor de análisis
    # ------------------------------------------------------------------

    from analysis.engine import AnalysisEngine

    analysis_config = cfg["analysis"]

    engine = AnalysisEngine(
        config=analysis_config,
    )

    logger.info(
        "AnalysisEngine inicializado."
    )

    # ------------------------------------------------------------------
    # Motor de eventos
    # ------------------------------------------------------------------

    from analysis.event_engine import EventEngine

    event_engine = EventEngine(
        analysis=engine,
        config=analysis_config,
    )

    logger.info(
        "EventEngine inicializado."
    )

    # ------------------------------------------------------------------
    # Perfil de comportamiento
    # ------------------------------------------------------------------

    from analysis.baseline import BehaviorProfile

    profile = BehaviorProfile(
        config={
            "learning_hours": analysis_config.get(
                "learning_hours", 2.0,
            ),
            "tz_name": tz_name,
        },
        analysis=engine,
    )

    logger.info("BehaviorProfile inicializado.")

    # ------------------------------------------------------------------
    # Detector de anomalías
    # ------------------------------------------------------------------

    from analysis.anomaly_detector import AnomalyDetector

    detector = AnomalyDetector(
        engine=engine,
        config=analysis_config,
    )

    logger.info("AnomalyDetector inicializado.")

    # ------------------------------------------------------------------
    # Guardia de calidad
    # ------------------------------------------------------------------

    from core.quality_guard import QualityGuard

    quality_guard = QualityGuard(config=analysis_config)

    logger.info("QualityGuard inicializado.")

    # ------------------------------------------------------------------
    # Política de informes
    # ------------------------------------------------------------------

    from core.report_policy import ReportPolicy

    summary_hours = tuple(
        ops.get("summary_hours_local", (6, 14, 22))
    )

    policy = ReportPolicy(config={
        **analysis_config,
        "event_settle_seconds": analysis_config.get(
            "event_settle_seconds", 60,
        ),
        "event_dedup_margin_seconds": analysis_config.get(
            "event_dedup_margin_seconds", 60,
        ),
        "max_messages_per_hour": ops.get(
            "max_messages_per_hour", 12,
        ),
        "summary_hours_local": summary_hours,
        "local_tz": tz_name,
    })

    logger.info("ReportPolicy inicializada.")

    # ------------------------------------------------------------------
    # FileLog
    # ------------------------------------------------------------------

    from core.file_log import FileLog

    report_log = storage_config.get(
        "report_log",
        "logs/reports.jsonl",
    )

    events_log = storage_config.get(
        "events_log",
        "logs/events.jsonl",
    )

    observations_log = storage_config.get(
        "observations_log",
        "logs/observations.jsonl",
    )

    file_log = FileLog(
        path=report_log,
        events_path=events_log,
        observations_path=observations_log,
        rotation_mode=rotation.get("mode", "none"),
        rotation_keep_days=rotation.get("keep_days", 7),
        rotation_max_mb=rotation.get("max_file_mb", 50.0),
        tz_name=tz_name,
    )

    logger.info(
        "FileLog inicializado."
    )

    # ------------------------------------------------------------------
    # Memoria histórica
    # ------------------------------------------------------------------

    from analysis.memory import HistoricalMemory

    memory = HistoricalMemory(
        file_log=file_log,
    )

    logger.info(
        "HistoricalMemory inicializada."
    )

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    from agent.llm_client import build_llm

    llm = build_llm(
        cfg["llm"],
    )

    logger.info(
        "Cliente LLM inicializado."
    )

    # ------------------------------------------------------------------
    # Serializador de contexto
    # ------------------------------------------------------------------

    from agent.context_serializer import ContextSerializer

    serializer = ContextSerializer()

    logger.info(
        "ContextSerializer inicializado."
    )

    # ------------------------------------------------------------------
    # Agente
    # ------------------------------------------------------------------

    from agent.cava_agent import CavaAgent
    agent = CavaAgent(
        llm=llm,
        engine=engine,
        store=store,
        context_serializer=serializer,
        memory=memory,
        device_id=int(cava_config["device_id"]),
        clock=clock,
        levels_provider=detector.consistency_levels,
    )

    logger.info(
        "CavaAgent inicializado."
    )
    # ------------------------------------------------------------------
    # Memoria de conversación por usuario (opcional)
    # ------------------------------------------------------------------

    from agent.conversation_memory import ConversationMemory
    agent.conversations = ConversationMemory(
        path=cfg.get("operations", {}).get("conversations_log", "logs/conversations.jsonl"),
        context_turns=3, max_chars_answer=400)
    agent.channel_aliases = cfg.get("channel_aliases", {})
    # La fuente SITRAD solo mapea canales del controlador: nunca hay Multipower. En demo/simulación
    # el store está vacío al arrancar, así que NO se deduce de él (daría False): se deja None.
    if mode == "sitrad":
        agent.multipower_present = False
        if hasattr(source, "discover_all"):
            agent.discovery_provider = source.discover_all

    logger.info(
        "Memoria de conversación: %s (aliases=%d).",
        agent.conversations.path,
        len(agent.channel_aliases),
    )
    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    telegram_config = cfg["telegram"]

    token = str(telegram_config.get("token", "") or "").strip()

    # Sin token (o con "enabled": false) se usa la consola: los informes
    # se imprimen y las preguntas se escriben en la terminal.
    if telegram_config.get("enabled", True) and token:

        from notifiers.telegram_report import TelegramReporter

        inner = TelegramReporter(
            bot_token=token,
            chat_id=telegram_config.get("chat_id", ""),
        )

        telegram = RetryNotifier(
            inner=inner,
            max_retries=ops.get("telegram_retry_max", 100),
            retry_interval_seconds=ops.get(
                "telegram_retry_interval_seconds", 30,
            ),
            clock=clock,
        )

    else:

        from notifiers.console_notifier import ConsoleNotifier

        telegram = ConsoleNotifier(
            interactive=True,
        )
    logger.info(
        "Telegram inicializado."
    )

    # ------------------------------------------------------------------
    # Configuración específica del Reporter
    # ------------------------------------------------------------------

    from core.channel_catalog import ROLE_TO_CHANNEL

    reporter_config = {
        "polling_seconds": cfg.get(
            "polling_seconds",
            5,
        ),

        "channels_que_disparan_eventos": analysis_config.get(
            "channels_que_disparan_eventos",
            [ROLE_TO_CHANNEL["temp_principal"]],
        ),

        "min_delta_canal_primario": analysis_config.get(
            "min_delta_canal_primario",
            0.5,
        ),

        "report_window_minutes": cfg.get(
            "report_window_minutes",
            5,
        ),

        "min_event_duration_min": analysis_config.get(
            "min_event_duration_min",
            0.5,
        ),

        "max_event_duration_min": analysis_config.get(
            "max_event_duration_min",
            5.0,
        ),

        "min_event_magnitude": analysis_config.get(
            "min_event_magnitude",
            0.3,
        ),

        "device_name": cava_config[
            "device_name"
        ],

        "device_id": cava_config[
            "device_id"
        ],
        "compressor_channel": cava_config.get(
            "compressor_channel", ROLE_TO_CHANNEL["compresor"]),

        "analysis": analysis_config,
        "cooling": cfg.get("cooling", {}),      # informe de temperatura
        "operations": ops,                      # zona horaria, latido, etc.
        "storage": storage_config,
        "mode": mode,

        "event_dedup_margin_seconds": analysis_config.get(
            "event_dedup_margin_seconds",
            60,
        ),

        "event_settle_seconds": analysis_config.get(
            "event_settle_seconds",
            60,
        ),

        "analysis_every_seconds": cfg.get(
            "analysis_every_seconds", 30,
        ),

        "anomaly_every_seconds": cfg.get(
            "anomaly_every_seconds", 30,
        ),

        "anomaly_clear_seconds": cfg.get(
            "anomaly_clear_seconds", 60,
        ),

        "heartbeat_every_minutes": ops.get(
            "heartbeat_every_minutes", 0,
        ),

        "stale_channel_minutes": ops.get(
            "stale_channel_minutes", 5,
        ),

        "warmup_on_start": ops.get(
            "warmup_on_start", True,
        ),
    }

    # ------------------------------------------------------------------
    # Orquestador
    # ------------------------------------------------------------------

    from core.reporter import Reporter

    reporter = Reporter(
        source=source,
        store=store,
        event_engine=event_engine,
        agent=agent,
        context_serializer=serializer,
        telegram=telegram,
        filelog=file_log,
        clock=clock,
        profile=profile,
        detector=detector,
        quality_guard=quality_guard,
        policy=policy,
        config=reporter_config,
    )

    logger.info(
        "Monitor iniciado correctamente."
    )

    logger.info(
        "Arquitectura activa: "
        "DataSource -> SeriesStore -> AnalysisEngine -> "
        "EventEngine -> CavaAgent -> Reporter."
    )

    logger.info(
        "Ramas activas: eventos=%s anomalías=%s salud=%s latido=%s "
        "política=%s",
        True,
        profile is not None and detector is not None,
        policy is not None,
        reporter_config["heartbeat_every_minutes"] > 0,
        policy is not None,
    )
    from core.case_log import CaseLog
    reporter.case_log = CaseLog(
        storage_config.get("cases_log", "logs/cases.jsonl")
    )
    # ------------------------------------------------------------------
    # Ejecución
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Replay del historial propio (SITRAD no expone el datalogger por API): solo store.ingest,
    # sin pasar por event_engine ni cooling_monitor.step.
    # En demo no se hace: sus observaciones llevan fechas simuladas.
    # ------------------------------------------------------------------
    if file_log is not None and mode == "sitrad" and storage_config.get("replay_on_start", True):
        from datetime import datetime, timedelta, timezone
        try:
            hours = min(float(storage_config.get("max_hours", 168)), float(store.max_hours))
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            n = 0
            for r in file_log.read_observations_since(cutoff):
                store.ingest(quality_guard.apply(r))
                n += 1
            logger.info("FileLog replay: %d lecturas desde %s.", n, cutoff.isoformat())
        except Exception:
            logger.exception("FileLog replay falló; sigo en vivo.")

    reporter.run_forever()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(name)s | "
            "%(message)s"
        ),
    )
    for _h in logging.getLogger().handlers:
        _h.addFilter(_RedactTokens())

    main()