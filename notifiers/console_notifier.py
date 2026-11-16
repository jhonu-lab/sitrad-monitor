from __future__ import annotations
# notifiers.console_notifier
import logging
import queue
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class ConsoleNotifier:
    """
    Sustituto de Telegram que imprime en consola.

    Implementa la misma interfaz que usa Reporter:

        send(text)
        start_polling() / stop_polling()
        has_command() / pop_command()

    Dos formas de recibir preguntas:

        - ask(texto): las encola por código (lo usa run_demo.py).
        - start_polling(): lee líneas de la terminal en un hilo, de modo
          que durante run_reporter.py puedes escribir preguntas.
    """

    def __init__(self, interactive: bool = False, prefix: str = "TELEGRAM") -> None:
        self.interactive = interactive
        self.prefix = prefix
        self._commands: queue.Queue[str] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.sent: list[str] = []

    # ---------------- envío ----------------

    def send(self, text: str, chat_id: Optional[str] = None) -> bool:
        if not text:
            return False
        self.sent.append(text)
        bar = "─" * 64
        print(f"\n┌{bar}\n│ >> {self.prefix}")
        for line in str(text).splitlines():
            print(f"│ {line}")
        print(f"└{bar}\n")
        return True

    # ---------------- preguntas ----------------

    def ask(self, question: str) -> None:
        print(f"\n[usuario] {question}")
        self._commands.put(question)

    def has_command(self) -> bool:
        return not self._commands.empty()

    def pop_command(self) -> Optional[str]:
        try:
            return self._commands.get_nowait()
        except queue.Empty:
            return None

    # ---------------- lectura interactiva ----------------

    def start_polling(self) -> None:
        if not self.interactive or self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._read_stdin, name="console-input", daemon=True
        )
        self._thread.start()
        logger.info("Escribe una pregunta y pulsa Enter (Ctrl+C para salir).")

    def stop_polling(self) -> None:
        self._running = False

    def _read_stdin(self) -> None:
        while self._running:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            line = line.strip()
            if line:
                self._commands.put(line)
