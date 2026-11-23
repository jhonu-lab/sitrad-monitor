"""core/worker.py — hilo de trabajo para lo lento (LLM, gráficos, diagnósticos).

El ciclo principal (lectura de SITRAD, alarmas, informe de temperatura) no
debe esperar a que el modelo redacte: una respuesta del LLM local puede
tardar minutos. El Reporter encola aquí las preguntas de los usuarios y la
redacción de mensajes de la política; este hilo las ejecuta EN ORDEN, una a
una, con prioridad de CPU baja.

Regla de concurrencia: todas las llamadas al agente (CavaAgent) se hacen
desde este hilo. El ciclo principal solo lee/escribe el SeriesStore (que
tiene lock) y prepara los datos del mensaje.
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_priority_lowered: set[int] = set()


def lower_current_thread_priority() -> bool:
    """Baja la prioridad de CPU del HILO actual (best-effort, una sola vez por hilo).

    Windows: SetThreadPriority(BELOW_NORMAL). Linux: setpriority sobre el id
    nativo del hilo (en Linux la prioridad es por hilo). En otros sistemas no
    hace nada. El ciclo principal conserva su prioridad normal, así la
    lectura de SITRAD no compite con el trabajo pesado del bot.
    """
    tid = threading.get_ident()
    if tid in _priority_lowered:
        return True
    ok = False
    try:
        if sys.platform == "win32":
            import ctypes

            THREAD_PRIORITY_BELOW_NORMAL = -1
            k32 = ctypes.windll.kernel32
            ok = bool(k32.SetThreadPriority(k32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL))
        elif sys.platform.startswith("linux") and hasattr(os, "setpriority"):
            native = threading.get_native_id()
            current = os.getpriority(os.PRIO_PROCESS, native)
            os.setpriority(os.PRIO_PROCESS, native, min(19, current + 5))
            ok = True
    except Exception:
        logger.debug("No pude bajar la prioridad del hilo.", exc_info=True)
    if ok:
        _priority_lowered.add(tid)
    return ok


class InlineWorker:
    """Ejecuta los trabajos en el acto (tests, herramientas y simulaciones deterministas)."""

    is_async = False

    def submit(self, fn: Callable[[], None], label: str = "") -> None:
        try:
            fn()
        except Exception:
            logger.exception("Trabajo '%s' falló.", label or getattr(fn, "__name__", "?"))

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 5.0) -> None:
        pass

    def drain(self, timeout: float = 0.0) -> bool:
        return True

    @property
    def pending(self) -> int:
        return 0


class AgentWorker:
    """Un hilo, una cola FIFO. submit() nunca bloquea; los fallos se registran y no paran el hilo."""

    is_async = True

    def __init__(self, name: str = "agent-worker", max_queue: int = 200,
                 lower_priority: bool = True) -> None:
        self.name = name
        self.lower_priority = lower_priority
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None], label: str = "") -> None:
        if self._thread is None or not self._thread.is_alive():
            self.start()
        try:
            self._q.put_nowait((fn, label))
        except queue.Full:
            logger.error("Cola del agente llena (%d); se descarta '%s'.", self._q.maxsize, label)

    def _run(self) -> None:
        if self.lower_priority:
            lower_current_thread_priority()
        while not self._stop.is_set():
            try:
                fn, label = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception:
                logger.exception("Trabajo '%s' falló en el hilo del agente.", label)
            finally:
                self._q.task_done()

    def drain(self, timeout: float = 30.0) -> bool:
        """Espera a que la cola se vacíe (tests y apagado ordenado). True si se vació."""
        done = threading.Event()

        def _wait() -> None:
            self._q.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        return done.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self.drain(timeout)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def pending(self) -> int:
        return self._q.qsize()
