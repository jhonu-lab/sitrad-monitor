#!/usr/bin/env python3
"""
Demonio del bot de Telegram.

Mantiene el bot escuchando comandos y permite al ingeniero disparar
la demo desde el chat, sin tocar la laptop.

Uso:
    python run_bot_daemon.py

Detener con Ctrl+C.
"""
import json
import sys
import time
import signal
import logging
from datetime import datetime

from notifiers.telegram_bot import TelegramBotListener, TelegramSender
from core.demo_controller import DemoController

# Silenciar werkzeug (Flask) cuando arranque el simulador
logging.getLogger("werkzeug").setLevel(logging.CRITICAL)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-22s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler("logs/bot_daemon.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("BotDaemon")

_running = {"flag": True}


def _handle_signal(signum, _frame):
    logger.info(f"Señal {signum} recibida. Deteniendo...")
    _running["flag"] = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


CONFIG_FILE = "config.json"


def load_config(path=CONFIG_FILE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.critical(f"No se encontró {path}.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(f"Error parseando {path}: {e}")
        sys.exit(1)


# =============================================================================
# HANDLERS DE COMANDOS
# =============================================================================
def make_handlers(sender: TelegramSender, demo: DemoController, cfg: dict):
    """Construye los handlers de cada comando."""

    def cmd_start(chat_id: str, text: str):
        sender.send(chat_id, (
            "👋 *Monitor Sitrad PRO*\n\n"
            "Comandos disponibles:\n"
            "• `/demo [N]`   — Inicia demo con N ciclos (default 25)\n"
            "• `/status`     — Estado actual del sistema\n"
            "• `/stop`       — Cancela la demo en curso\n"
            "• `/ping`       — Verifica que el bot responde\n"
            "• `/help`       — Muestra esta ayuda\n\n"
            "_La demo simula controladores Full Gauge y envía alertas "
            "reales aquí mismo._"
        ))

    def cmd_demo(chat_id: str, text: str):
        # Parsear ciclos opcionales: /demo 15
        partes = text.split()
        ciclos = 25
        if len(partes) > 1:
            try:
                ciclos = max(5, min(100, int(partes[1])))
            except ValueError:
                sender.send(chat_id, "⚠️ Uso: `/demo` o `/demo 30`")
                return

        if demo.is_running():
            st = demo.status()
            sender.send(chat_id, (
                f"⚠️ Ya hay una demo en curso "
                f"(ciclo {st['cycle']}/{st['total']}).\n"
                f"Espera a que termine o envía `/stop`."
            ))
            return

        # Duración recomendada: ~30-40 segundos totales
        intervalo = max(0.5, min(3.0, 30.0 / ciclos))

        if demo.start(chat_id, total_cycles=ciclos, interval=intervalo):
            logger.info(f"Demo iniciada por chat {chat_id} ({ciclos} ciclos)")
        else:
            sender.send(chat_id, "❌ No se pudo iniciar la demo.")

    def cmd_status(chat_id: str, text: str):
        st = demo.status()
        if st["state"] == "RUNNING":
            elapsed = (datetime.now() - st["started_at"]).seconds \
                if st["started_at"] else 0
            sender.send(chat_id, (
                f"🟢 *Demo en curso*\n"
                f"• Ciclo: {st['cycle']}/{st['total']}\n"
                f"• Tiempo transcurrido: {elapsed}s\n"
                f"• Solicitada por: {st['requested_by']}"
            ))
        else:
            sender.send(chat_id, (
                "⚪ *Sistema en reposo*\n"
                "No hay demos en curso.\n"
                "Envía `/demo` para iniciar una."
            ))

    def cmd_stop(chat_id: str, text: str):
        if demo.stop():
            sender.send(chat_id, "⏹️ Deteniendo demo...")
        else:
            sender.send(chat_id, "ℹ️ No hay demo en curso.")

    def cmd_ping(chat_id: str, text: str):
        sender.send(chat_id, (
            f"🏓 Pong\n"
            f"• Hora servidor: {datetime.now().strftime('%H:%M:%S')}\n"
            f"• Estado: {'demo corriendo' if demo.is_running() else 'en reposo'}"
        ))

    return {
        "start": cmd_start,
        "help": cmd_start,
        "demo": cmd_demo,
        "status": cmd_status,
        "stop": cmd_stop,
        "ping": cmd_ping,
    }


# =============================================================================
# MAIN
# =============================================================================
def main():
    logger.info("=" * 70)
    logger.info("BOT DAEMON — Monitor Sitrad PRO")
    logger.info(f"Inicio: {datetime.now().isoformat()}")
    logger.info("=" * 70)

    cfg = load_config()
    n_cfg = cfg.get("notifiers", {})

    token = n_cfg.get("telegram_token", "").strip()
    chat_id = n_cfg.get("telegram_chat_id", "").strip()

    if not token or not chat_id:
        logger.critical(
            "config.json no tiene telegram_token/telegram_chat_id. "
            "Configúralos y vuelve a intentar."
        )
        sys.exit(1)

    # Chats autorizados (por defecto solo el configurado)
    allowed = n_cfg.get("allowed_chat_ids") or [chat_id]

    sender = TelegramSender(token)
    demo = DemoController(cfg, sender)

    listener = TelegramBotListener(
        bot_token=token,
        allowed_chat_ids=allowed,
        poll_interval=2.0,
    )

    # Registrar handlers
    for cmd, fn in make_handlers(sender, demo, cfg).items():
        listener.register(cmd, fn)

    # Mensaje de arranque al chat principal
    sender.send(chat_id, (
        f"🤖 *Bot iniciado*\n"
        f"• Hora: {datetime.now().strftime('%H:%M:%S')}\n"
        f"• Comandos: /demo /status /stop /ping /help\n\n"
        f"Escribe `/demo` para iniciar una prueba."
    ))

    listener.start()
    logger.info(f"Bot listo. Autorizado para chat(s): {allowed}")
    logger.info("Esperando comandos (Ctrl+C para detener)...")

    # Mantener el proceso vivo
    while _running["flag"]:
        time.sleep(1)

    listener.stop()
    logger.info("Bot daemon detenido.")


if __name__ == "__main__":
    main()