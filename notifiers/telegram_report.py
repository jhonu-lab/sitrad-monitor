"""Notificador Telegram: envío, recepción de preguntas e identidad de usuario.

Cambios respecto a la versión anterior
    * Cada pregunta llega como `UserQuestion` (subclase de str) con
      .user_key, .user_name, .chat_id y .command. Como sigue siendo un str,
      el Reporter y ConsoleNotifier existentes funcionan sin cambios.
    * El parseo es una función pura (`parse_incoming`) con pruebas.
    * Una mención "@bot ¿...?" ya no se descarta; "/ask@bot texto" conserva
      "texto"; un "@" dentro de la pregunta (correo) ya no la trunca.
    * En grupos solo se atiende a quien menciona al bot, responde a un
      mensaje del bot o usa un comando. Así el bot no contesta la charla
      normal del grupo aunque el modo de privacidad esté desactivado.
    * El backlog se descarta de verdad al arrancar (antes no avanzaba el
      offset, así que los mensajes viejos se contestaban igual).
    * Lista blanca opcional de usuarios (`allowed_user_ids`).
    * Varios chats: `chat_id` + `extra_chat_ids` reciben las alertas; cada
      respuesta vuelve al chat donde se hizo la pregunta.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Iterable, Optional

import requests

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Sistema de monitoreo conectado.\n\n"
    "Puedes preguntarme, por ejemplo:\n"
    "• ¿Cuál fue la temperatura promedio en los últimos 30 minutos?\n"
    "• ¿Qué eventos ocurrieron hoy?\n"
    "• ¿Qué relación hubo entre la temperatura y la potencia?\n"
    "• ¿Cuánta energía se consumió en la última hora?\n\n"
    "En un grupo, escríbeme mencionándome (@{bot}) o respondiendo a uno de "
    "mis mensajes.\n"
    "/historial — tus últimas preguntas\n"
    "/olvidar — borra lo que recuerdo de ti"
)

_CMD_RE = re.compile(r"^/(\w+)(?:@(\w+))?(?:\s+(.*))?$", re.S)


class UserQuestion(str):
    """Texto de la pregunta + quién la hizo. Es un str: compatible hacia atrás."""

    def __new__(cls, text: str, user_key: str = "", user_name: str = "",
                chat_id: str = "", command: Optional[str] = None):
        obj = super().__new__(cls, text)
        obj.user_key = user_key
        obj.user_name = user_name
        obj.chat_id = chat_id
        obj.command = command            # None | "forget" | "history"
        return obj


@dataclass(frozen=True)
class Incoming:
    kind: str                            # question | help | forget | history
    text: str
    chat_id: str
    user_id: str
    user_name: str

    @property
    def user_key(self) -> str:
        return f"{self.chat_id}:{self.user_id}"


def parse_incoming(
    message: dict,
    bot_username: str = "",
    bot_id: Optional[int] = None,
    allowed_chat: str = "",
    allowed_user_ids: Optional[Iterable] = None,
    group_requires_mention: bool = True,
    allowed_chats: Optional[Iterable] = None,
) -> Optional[Incoming]:
    """Interpreta un `message` de Telegram. None = ignorar.

    `allowed_chats` (o el antiguo `allowed_chat`): si hay alguno, solo se
    atienden esos chats. Vacío = cualquier chat.
    """
    text = (message.get("text") or "").strip()
    if not text:
        return None

    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    chats = {str(c) for c in (allowed_chats or ()) if str(c).strip()}
    if allowed_chat:
        chats.add(str(allowed_chat))
    if chats and chat_id not in chats:
        return None

    frm = message.get("from") or {}
    if frm.get("is_bot"):
        return None
    user_id = str(frm.get("id", ""))
    if allowed_user_ids is not None and user_id not in {str(u) for u in allowed_user_ids}:
        return None
    user_name = frm.get("first_name") or frm.get("username") or user_id

    def mk(kind: str, body: str) -> Incoming:
        return Incoming(kind, body.strip(), chat_id, user_id, user_name)

    # ---- comandos -------------------------------------------------------
    m = _CMD_RE.match(text)
    if m:
        cmd, target, rest = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
        if target and bot_username and target.lower() != bot_username.lower():
            return None                  # comando dirigido a otro bot
        if cmd in {"start", "help", "ayuda"}:
            return mk("help", "")
        if cmd in {"olvidar", "forget"}:
            return mk("forget", "")
        if cmd == "historial":
            return mk("history", "")
        if cmd in {"preguntar", "ask", "p"}:
            return mk("question", rest) if rest else mk("help", "")
        body = f"{cmd} {rest}".strip()   # comando desconocido: como antes
        return mk("question", body)

    # ---- mención / respuesta al bot ------------------------------------
    addressed = False
    if bot_username:
        pat = re.compile(rf"@{re.escape(bot_username)}\b", re.I)
        if pat.search(text):
            addressed = True
            text = pat.sub("", text).strip()
    reply = message.get("reply_to_message") or {}
    if bot_id is not None and (reply.get("from") or {}).get("id") == bot_id:
        addressed = True

    is_group = chat.get("type") in {"group", "supergroup"}
    if is_group and group_requires_mention and not addressed:
        return None
    if not text:
        return mk("help", "")            # solo "@bot"
    return mk("question", text)


class TelegramReporter:
    """Cliente Telegram con polling de comandos en hilo aparte."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str = "",
        poll_timeout: int = 30,
        poll_interval: float = 1.0,
        send_timeout: int = 15,
        group_requires_mention: bool = True,
        allowed_user_ids: Optional[Iterable] = None,
        extra_chat_ids: Optional[Iterable] = None,
    ) -> None:
        if not bot_token:
            raise ValueError("TelegramReporter requiere bot_token.")

        self.bot_token = str(bot_token)
        self.chat_id = str(chat_id or "").strip()
        # Chats que reciben las alertas y desde los que se aceptan preguntas.
        self.chat_ids: list[str] = []
        for c in [self.chat_id, *(extra_chat_ids or ())]:
            c = str(c or "").strip()
            if c and c not in self.chat_ids:
                self.chat_ids.append(c)
        self.poll_timeout = int(poll_timeout)
        self.poll_interval = float(poll_interval)
        self.send_timeout = int(send_timeout)
        self.group_requires_mention = group_requires_mention
        self._tls = threading.local()
        self.allowed_user_ids = (
            None if allowed_user_ids is None else {str(u) for u in allowed_user_ids}
        )

        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.bot_username = ""
        self.bot_id: Optional[int] = None

        self._offset = 0
        self._commands: Queue = Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ envío
    def _get_session(self, kind: str) -> requests.Session:
        """Una requests.Session por (hilo, kind).

        requests.Session no es thread-safe: el pool de conexiones y el CookieJar
        se corrompen con concurrencia. La rama de polling y la del reporter
        comparten este notificador, así que cada hilo necesita su propia sesión.
        """
        attr = f"_sess_{kind}"
        sess = getattr(self._tls, attr, None)
        if sess is None:
            sess = requests.Session()
            setattr(self._tls, attr, sess)
        return sess

    @staticmethod
    def _split_for_telegram(text: str, limit: int = 4000) -> list[str]:
        """Corta preferentemente en saltos de línea, luego espacios, luego duro."""
        if len(text) <= limit:
            return [text]
        chunks, remaining = [], text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = remaining.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
        return chunks

    def send(self, text: str, chat_id: Optional[str] = None) -> bool:
        target = str(chat_id or self.chat_id or "").strip()
        if not target:
            logger.warning("Telegram: chat_id vacío, no se envía mensaje.")
            return False
        if not text:
            return False
        chunks = self._split_for_telegram(str(text), 4000)
        n = len(chunks)
        if n > 1:
            logger.info("Telegram: mensaje de %d chars dividido en %d partes.",
                        len(str(text)), n)
        try:
            for i, chunk in enumerate(chunks, start=1):
                body = f"({i}/{n})\n{chunk}" if n > 1 else chunk
                response = self._get_session("send").post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": target, "text": body, "disable_web_page_preview": True},
                    timeout=self.send_timeout,
                )
                response.raise_for_status()
            return True
        except requests.exceptions.RequestException as exc:
            logger.error("Error enviando a Telegram (parte %d/%d): %s", i, n, exc)
            return False

    def send_photo(self, path: str, caption: str = "", chat_id: Optional[str] = None) -> bool:
        target = str(chat_id or self.chat_id or "").strip()
        if not target:
            logger.warning("send_photo: no hay chat_id.")
            return False
        try:
            with open(path, "rb") as fh:
                r = self._get_session("send").post(
                    f"{self.base_url}/sendPhoto",
                    data={"chat_id": target, "caption": (caption or "")[:1024]},
                    files={"photo": fh},
                    timeout=self.send_timeout,
                )
            if not r.ok:
                logger.warning("sendPhoto falló: HTTP %s", r.status_code)
            return bool(r.ok)
        except (requests.RequestException, OSError) as exc:
            logger.warning("sendPhoto falló: %s", type(exc).__name__)
            return False
    # ---------------------------------------------------------------- polling

    def start_polling(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-poller", daemon=True,
        )
        self._thread.start()
        logger.info("Telegram polling iniciado.")

    def stop_polling(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("Telegram polling detenido.")

    def _identify_bot(self) -> None:
        try:
            r = self._get_session("main").get(f"{self.base_url}/getMe", timeout=10)
            r.raise_for_status()
            me = r.json().get("result") or {}
            self.bot_username = me.get("username", "") or ""
            self.bot_id = me.get("id")
            logger.info("Bot identificado: @%s", self.bot_username)
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.warning("No se pudo identificar al bot (getMe): %s", exc)

    def _drain_backlog(self) -> None:
        """Descarta lo pendiente: pide lo último y avanza el offset tras ello."""
        try:
            r = self._get_session("poll").get(
                f"{self.base_url}/getUpdates",
                params={"offset": -1, "timeout": 0}, timeout=5,
            )
            r.raise_for_status()
            result = r.json().get("result", [])
            if result:
                self._offset = int(result[-1]["update_id"]) + 1
            logger.info("Backlog de Telegram descartado (offset=%d).", self._offset)
        except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
            logger.warning("No se pudo drenar el backlog de Telegram: %s", exc)

    def _poll_loop(self) -> None:
        self._identify_bot()
        self._drain_backlog()
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("Error en bucle de polling Telegram.")
            self._stop_event.wait(self.poll_interval)

    def _poll_once(self) -> None:
        try:
            response = self._get_session("poll").get(
                f"{self.base_url}/getUpdates",
                params={"offset": self._offset, "timeout": self.poll_timeout,
                        "allowed_updates": json.dumps(["message"])},
                timeout=self.poll_timeout + 5,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as exc:
            logger.warning("Fallo en getUpdates de Telegram: %s", exc)
            return
        except ValueError as exc:
            logger.warning("Telegram devolvió JSON inválido: %s", exc)
            return

        if not data.get("ok"):
            logger.warning("Telegram respondió con ok=false: %s", data)
            return

        for update in data.get("result", []):
            update_id = update.get("update_id")
            if update_id is not None:
                self._offset = int(update_id) + 1

            inc = parse_incoming(
                update.get("message") or {},
                bot_username=self.bot_username,
                bot_id=self.bot_id,
                allowed_chats=self.chat_ids,
                allowed_user_ids=self.allowed_user_ids,
                group_requires_mention=self.group_requires_mention,
            )
            if inc is None:
                continue

            if inc.kind == "help":
                self.send(HELP_TEXT.format(bot=self.bot_username or "bot"),
                          chat_id=inc.chat_id)
                continue

            command = {"forget": "forget", "history": "history"}.get(inc.kind)
            logger.info("Pregunta Telegram de %s: %r", inc.user_name, inc.text or command)
            # Un comando sin texto ("/olvidar") se encola como "/olvidar" para
            # que no sea una cadena vacía (el Reporter descarta las vacías).
            self._commands.put(UserQuestion(
                inc.text or f"/{command}", user_key=inc.user_key, user_name=inc.user_name,
                chat_id=inc.chat_id, command=command,
            ))

    # ---------------------------------------------------------- cola de comandos

    def has_command(self) -> bool:
        return not self._commands.empty()

    def pop_command(self) -> Optional[UserQuestion]:
        try:
            return self._commands.get_nowait()
        except Empty:
            return None

    def set_chat_id(self, chat_id: str) -> None:
        self.chat_id = str(chat_id or "").strip()
        if self.chat_id and self.chat_id not in self.chat_ids:
            self.chat_ids.insert(0, self.chat_id)
