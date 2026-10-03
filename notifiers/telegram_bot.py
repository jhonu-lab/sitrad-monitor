"""
Listener y sender de Telegram para comandos interactivos.

- TelegramBotListener: hace polling a getUpdates y despacha comandos.
- TelegramSender:     envía mensajes simples (usado por el DemoController).

IMPORTANTE: Telegram solo permite UN consumidor de getUpdates por bot.
No levantes dos procesos usando el mismo token.
"""
import threading
import time
import logging
import requests
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class TelegramSender:
    """Cliente mínimo para enviar mensajes a un chat."""

    def __init__(self, bot_token: str):
        self.base = f"https://api.telegram.org/bot{bot_token}"

    def send(self, chat_id: str, text: str) -> bool:
        try:
            r = requests.post(
                f"{self.base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            return r.json().get("ok", False)
        except Exception as e:
            logger.error(f"sendMessage falló: {e}")
            return False


class TelegramBotListener:
    """
    Escucha mensajes entrantes y despacha comandos a handlers registrados.

    Uso:
        listener = TelegramBotListener(token, allowed_chat_ids=["123"])
        listener.register("demo", mi_handler)
        listener.start()
    """

    def __init__(self, bot_token: str,
                 allowed_chat_ids: Optional[list] = None,
                 poll_interval: float = 2.0):
        self.token = bot_token
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.allowed_chat_ids = {str(c) for c in (allowed_chat_ids or []) if c}
        self.poll_interval = poll_interval
        self.offset = 0
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.handlers: Dict[str, Callable[[str, str], None]] = {}

    def register(self, command: str, handler: Callable[[str, str], None]):
        """
        Registra un handler.
        command: sin '/'  (ej: "demo" para /demo)
        handler: callable(chat_id: str, full_text: str) → None
        """
        self.handlers[command.lower()] = handler

    def start(self):
        if self.running:
            return
        self.running = True
        self._skip_backlog()
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name="TelegramBotListener",
        )
        self.thread.start()
        logger.info("TelegramBotListener iniciado (polling activo).")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("TelegramBotListener detenido.")

    # ------------------------------------------------------------------ #
    def _skip_backlog(self):
        """Descarta updates acumulados mientras el bot estaba apagado."""
        try:
            r = requests.get(
                f"{self.base}/getUpdates",
                params={"offset": -1, "timeout": 0},
                timeout=5,
            )
            data = r.json()
            results = data.get("result", [])
            if results:
                self.offset = results[-1]["update_id"] + 1
        except Exception:
            pass

    def _loop(self):
        while self.running:
            try:
                self._poll_once()
            except Exception as e:
                logger.error(f"Error en poll de Telegram: {e}")
            time.sleep(self.poll_interval)

    def _poll_once(self):
        r = requests.get(
            f"{self.base}/getUpdates",
            params={"offset": self.offset, "timeout": 10},
            timeout=15,
        )
        data = r.json()
        if not data.get("ok"):
            return
        for upd in data.get("result", []):
            self.offset = upd["update_id"] + 1
            self._handle_update(upd)

    def _handle_update(self, upd: dict):
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        # ← AÑADE ESTA LÍNEA:
        logger.info(f"Mensaje recibido de chat_id={chat_id} "
                    f"({msg.get('chat', {}).get('first_name', '?')}): {text}")

        # Filtro de seguridad (el que ya tenías)
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.warning(f"Ignorado mensaje de chat no autorizado: {chat_id}")
            return

        if not text.startswith("/"):
            return

        # Parsear /comando o /comando@botname
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()

        handler = self.handlers.get(cmd)
        if handler:
            try:
                handler(chat_id, text)
            except Exception as e:
                logger.exception(f"Handler /{cmd} falló: {e}")