#!/usr/bin/env python3
# =============================================================================
# RUN PRODUCTION — Worker contra Sitrad PRO real (o simulador con --simulate)
# =============================================================================
"""
Modos de uso:

  1) Producción pura (Sitrad real en la empresa):
        python run_production.py
     → Se conecta a https://localhost:8001/api (Sitrad real).
     → Si no hay conexión, aborta con código 2.

  2) Demo con Telegram real (recomendado para validar integración):
        python run_production.py --simulate
     → Levanta el simulador local en http://localhost:8001/api.
     → Envía alertas REALES a Telegram.
     → Ejecuta 25 ciclos y termina.

  3) Demo larga con Telegram:
        python run_production.py --simulate --ciclos 60 --intervalo 2
"""
import argparse
import json
import time
import signal
import sys
import logging
from datetime import datetime

from core.sitrad_client import SitradClient
from core.worker import MonitorWorker
from notifiers.console import ConsoleNotifier
from notifiers.file_log import FileLogNotifier

LOG_FILE = "logs/production.log"
LOG_FORMAT = "%(asctime)s | %(name)-22s | %(levelname)-8s | %(message)s"
CONFIG_FILE = "config.json"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Silenciar logs ruidosos de Flask cuando corremos el simulador
logging.getLogger("werkzeug").setLevel(logging.CRITICAL)

logger = logging.getLogger("SitradProduction")

_running = {"flag": True}


def _handle_signal(signum, _frame):
    logger.info(f"Señal {signum} recibida. Cerrando gracefully...")
    _running["flag"] = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# =============================================================================
# CONFIG
# =============================================================================
def load_config(path: str = CONFIG_FILE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.critical(f"No se encontró {path}.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(f"Error parseando {path}: {e}")
        sys.exit(1)


def build_notifiers(cfg: dict) -> list:
    """Construye los notificadores según config.json."""
    notifiers = [ConsoleNotifier(), FileLogNotifier("logs/alerts.jsonl")]
    n_cfg = cfg.get("notifiers", {})

    if n_cfg.get("telegram"):
        token = n_cfg.get("telegram_token", "").strip()
        chat = n_cfg.get("telegram_chat_id", "").strip()
        if token and chat:
            from notifiers.telegram import TelegramNotifier
            notifiers.append(TelegramNotifier(token, chat))
            logger.info("Notificador Telegram ACTIVADO.")
        else:
            logger.warning(
                "Telegram marcado como activo pero falta token o chat_id. "
                "Se omite."
            )
    else:
        logger.info("Telegram desactivado en config.json (notifiers.telegram = false).")

    return notifiers


# =============================================================================
# MODO PRODUCCIÓN PURA
# =============================================================================
def run_pure_production(cfg: dict):
    host = cfg["sitrad"]["production_host"]
    logger.info(f"Sitrad host (real): {host}")

    client = SitradClient(
        base_url=host,
        verify_ssl=cfg["sitrad"].get("verify_ssl", False),
        timeout=cfg["sitrad"].get("timeout", 10),
    )

    logger.info("Verificando conectividad con Sitrad PRO...")
    test = client.get_instruments()
    if not test:
        logger.critical(
            "No se pudo contactar a Sitrad PRO en "
            f"{host}. Verifica que Sitrad Service esté corriendo. "
            "Para demo local usa: python run_production.py --simulate"
        )
        sys.exit(2)
    logger.info(f"Conexión OK. {len(test)} instrumentos detectados.")

    _run_worker_loop(cfg, client)


# =============================================================================
# MODO SIMULADO (con notificadores reales)
# =============================================================================
def run_simulated(cfg: dict, ciclos_total: int, intervalo: float,
                  forever: bool):
    from simulator.fake_sitrad_api import run_server, advance_tick, reset_ticks

    logger.info("MODO SIMULADO: levantando simulador local en :8001")
    run_server(port=8001)
    time.sleep(1.5)
    reset_ticks()
    logger.info("Simulador listo. Apuntando el cliente a demo_host.")

    host = cfg["sitrad"]["demo_host"]
    logger.info(f"Sitrad host (simulado): {host}")

    client = SitradClient(
        base_url=host,
        verify_ssl=False,   # demo_host es HTTP, no HTTPS
        timeout=cfg["sitrad"].get("timeout", 10),
    )

    _run_worker_loop(cfg, client, ciclos_total=ciclos_total,
                     intervalo=intervalo, forever=forever,
                     advance=advance_tick)


# =============================================================================
# BUCLE DEL WORKER (compartido entre modos)
# =============================================================================
def _run_worker_loop(cfg: dict, client: SitradClient,
                     ciclos_total: int = 0,
                     intervalo: float = 0.0,
                     forever: bool = False,
                     advance=None):
    """
    Args:
        ciclos_total: si > 0, termina tras N ciclos.
        forever: si True, corre indefinidamente (ignora ciclos_total).
        advance: callback opcional que avanza el tick del simulador.
    """
    notifiers = build_notifiers(cfg)
    worker = MonitorWorker(
        client=client,
        notifiers=notifiers,
        cooldown=cfg["rules"]["cooldown_seconds"],
        required_consecutive=cfg["rules"]["required_consecutive"],
    )

    interval = intervalo or cfg["polling"]["interval_seconds"]
    logger.info(
        f"Polling cada {interval}s | Cooldown "
        f"{cfg['rules']['cooldown_seconds']}s | Confirmación "
        f"{cfg['rules']['required_consecutive']} lecturas"
    )

    cycle = 0
    while _running["flag"]:
        cycle += 1
        if not forever and ciclos_total and cycle > ciclos_total:
            break

        try:
            worker.run_cycle()
        except Exception as e:
            logger.exception(f"Error en ciclo #{cycle}: {e}")

        # Avanzar el simulador, si aplica
        if advance is not None:
            advance()

        # Espera interrumpible
        for _ in range(int(max(1, interval))):
            if not _running["flag"]:
                break
            time.sleep(1)

    logger.info(f"Worker detenido tras {cycle} ciclos.")


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Worker de monitoreo Sitrad PRO (producción o simulado)."
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Levanta el simulador local y usa notificadores reales "
             "(útil para demo con Telegram).",
    )
    parser.add_argument(
        "--ciclos", type=int, default=25,
        help="Número de ciclos en modo --simulate (default: 25).",
    )
    parser.add_argument(
        "--intervalo", type=float, default=1.5,
        help="Segundos entre ciclos en modo --simulate (default: 1.5).",
    )
    parser.add_argument(
        "--forever", action="store_true",
        help="En modo --simulate, correr indefinidamente (ignora --ciclos).",
    )
    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info("WORKER DE MONITOREO SITRAD PRO")
    logger.info(f"Inicio: {datetime.now().isoformat()}")
    logger.info(f"Modo: {'SIMULADO (con notificadores reales)' if args.simulate else 'PRODUCCIÓN (Sitrad real)'}")
    logger.info("=" * 70)

    cfg = load_config()

    if args.simulate:
        # En modo simulado ignoramos el mode del config, pero avisamos
        if cfg.get("mode") != "demo":
            logger.info(
                "Nota: config.json tiene mode='production' pero estás en "
                "--simulate. Usando demo_host de todos modos."
            )
        run_simulated(cfg, args.ciclos, args.intervalo, args.forever)
    else:
        if cfg.get("mode") != "production":
            logger.warning(
                "config.json tiene mode != 'production'. "
                "Continuando de todos modos contra el host de producción."
            )
        run_pure_production(cfg)


if __name__ == "__main__":
    main()