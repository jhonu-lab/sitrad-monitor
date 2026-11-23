from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime

from core.clock import Clock

logger = logging.getLogger(__name__)


class RetryNotifier:
    """
    Envuelve un notificador existente y reintenta mensajes que fallan.

    Contrato:
        - send(text) intenta enviar de inmediato. Si falla, encola.
        - tick() drena la cola. Se llama desde el ciclo del Reporter,
          independientemente de que haya mensajes nuevos.
        - start_polling() / stop_polling() delegan al notificador interno
          para que la rama de preguntas interactivas funcione.
        - La cola tiene tope FIFO: si se llena, descarta los más viejos.
        - Nunca propaga excepción al ciclo principal.
    """

    def __init__(
        self,
        inner,
        max_retries: int = 100,
        retry_interval_seconds: float = 30.0,
        clock: Clock | None = None,
    ) -> None:
        self.inner = inner
        self.max_retries = max_retries
        self.retry_interval_s = retry_interval_seconds
        self.clock = clock or Clock()

        # (encolado_en, texto, chat_id). Se usa desde el ciclo principal y
        # desde el hilo del agente: todo acceso pasa por el lock.
        self._queue: deque[tuple[datetime, str, str | None]] = deque()
        self._lock = threading.RLock()
        self._last_attempt: datetime | None = None

    # ------------------------------------------------------------
    # ENVÍO
    # ------------------------------------------------------------

    def _targets(self, chat_id: str | None) -> list[str | None]:
        """chat_id explícito -> solo ese. Sin chat_id -> todos los chats de alertas del notificador."""
        if chat_id is not None:
            return [chat_id]
        chats = list(getattr(self.inner, "chat_ids", None) or [])
        return chats or [None]

    def _send_one(self, text: str, chat_id: str | None) -> bool:
        try:
            return bool(self.inner.send(text, chat_id=chat_id) if chat_id is not None
                        else self.inner.send(text))
        except Exception:
            logger.exception("Notificador subyacente falló.")
            return False

    def send(self, text: str, chat_id: str | None = None) -> bool:
        ok_all = True
        for target in self._targets(chat_id):
            if self._send_one(text, target):
                continue
            ok_all = False
            with self._lock:
                self._queue.append((self.clock.now(), text, target))
                while len(self._queue) > self.max_retries:
                    self._queue.popleft()
                n = len(self._queue)
            logger.warning("Mensaje encolado para reintento (chat=%s). Cola=%d", target, n)
        return ok_all

    # ------------------------------------------------------------
    # DRENAJE
    # ------------------------------------------------------------

    def tick(self) -> None:
        """
        Drena la cola si toca. Llamar una vez por ciclo del Reporter.

        No bloquea. No lanza. Si la cola está vacía o no ha pasado
        retry_interval_seconds, retorna sin tocar el notificador.
        """
        self._maybe_drain()

    def _maybe_drain(self) -> None:
        with self._lock:
            if not self._queue:
                return
            now = self.clock.now()
            if self._last_attempt is not None:
                if (now - self._last_attempt).total_seconds() < self.retry_interval_s:
                    return
            self._last_attempt = now
        self._drain_once()

    def _drain_once(self) -> None:
        with self._lock:
            pending = len(self._queue)
        for _ in range(pending):
            with self._lock:
                if not self._queue:
                    break
                _, text, chat_id = self._queue.popleft()
            if not self._send_one(text, chat_id):
                with self._lock:
                    self._queue.appendleft((self.clock.now(), text, chat_id))
                    n = len(self._queue)
                logger.warning("Reintento fallido; quedan %d en cola.", n)
                return

    # ------------------------------------------------------------
    # POLLING DE PREGUNTAS
    # ------------------------------------------------------------

    def start_polling(self) -> None:
        """Delega al notificador interno si lo implementa."""
        inner = getattr(self.inner, "start_polling", None)
        if inner is None:
            return
        try:
            inner()
        except Exception:
            logger.exception("RetryNotifier: start_polling falló.")

    def stop_polling(self) -> None:
        """Delega al notificador interno si lo implementa."""
        inner = getattr(self.inner, "stop_polling", None)
        if inner is None:
            return
        try:
            inner()
        except Exception:
            logger.exception("RetryNotifier: stop_polling falló.")

    # ------------------------------------------------------------
    # INTROSPECCIÓN Y COMPATIBILIDAD
    # ------------------------------------------------------------

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def sent(self):
        return getattr(self.inner, "sent", [])

    def ask(self, text: str) -> None:
        if hasattr(self.inner, "ask"):
            self.inner.ask(text)

    def has_command(self) -> bool:
        return bool(
            hasattr(self.inner, "has_command")
            and self.inner.has_command()
        )

    def pop_command(self):
        if hasattr(self.inner, "pop_command"):
            return self.inner.pop_command()
        return None

    def send_photo(self, path: str, caption: str = "", chat_id: str | None = None) -> bool:
        fn = getattr(self.inner, "send_photo", None)
        if fn is None:
            return False
        try:
            if chat_id is not None:
                return bool(fn(path, caption, chat_id=chat_id))
            return bool(fn(path, caption))
        except Exception:
            logger.exception("RetryNotifier: send_photo falló.")
            return False