"""
Controlador de demos disparadas desde Telegram.

- Solo permite UNA demo a la vez.
- Corre el simulador + worker en un thread separado.
- Envía todas las alertas al chat que solicitó la demo.
"""
import threading
import time
import logging
from datetime import datetime
from typing import Optional

from core.sitrad_client import SitradClient
from core.worker import MonitorWorker

logger = logging.getLogger(__name__)


# Estado global del simulador (solo se levanta una vez por proceso)
_simulator_lock = threading.Lock()
_simulator_started = False


def _ensure_simulator_running():
    """Levanta el simulador Flask si no está corriendo aún."""
    global _simulator_started
    from simulator.fake_sitrad_api import run_server, reset_ticks

    with _simulator_lock:
        if not _simulator_started:
            run_server(port=8001)
            time.sleep(1.5)
            _simulator_started = True
            logger.info("Simulador Flask iniciado en :8001")
        reset_ticks()


class DemoController:
    """
    Gestiona la ejecución de demos. Thread-safe.
    """

    def __init__(self, cfg: dict, sender):
        """
        Args:
            cfg: config.json completo.
            sender: TelegramSender para mensajes de control.
        """
        self.cfg = cfg
        self.sender = sender
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.state = "IDLE"           # "IDLE" | "RUNNING"
        self.requested_by: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.cycle = 0
        self.total_cycles = 0

    # ------------------------------------------------------------------ #
    # API PÚBLICA
    # ------------------------------------------------------------------ #
    def is_running(self) -> bool:
        with self.lock:
            return self.state == "RUNNING"

    def start(self, chat_id: str, total_cycles: int = 25,
              interval: float = 1.5) -> bool:
        """
        Inicia una demo. Retorna False si ya hay una en curso.
        """
        with self.lock:
            if self.state == "RUNNING":
                return False
            self.state = "RUNNING"
            self.requested_by = chat_id
            self.started_at = datetime.now()
            self.cycle = 0
            self.total_cycles = total_cycles
            self.stop_event.clear()

        self.thread = threading.Thread(
            target=self._run_demo,
            args=(chat_id, total_cycles, interval),
            daemon=True, name="DemoRunner",
        )
        self.thread.start()
        return True

    def stop(self) -> bool:
        """Solicita cancelar la demo en curso. Retorna True si había una."""
        if not self.is_running():
            return False
        self.stop_event.set()
        return True

    def status(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "cycle": self.cycle,
                "total": self.total_cycles,
                "started_at": self.started_at,
                "requested_by": self.requested_by,
            }

    # ------------------------------------------------------------------ #
    # INTERNO
    # ------------------------------------------------------------------ #
    def _run_demo(self, chat_id: str, total_cycles: int, interval: float):
        from notifiers.telegram import TelegramNotifier

        try:
            self.sender.send(
                chat_id,
                f"🎬 *Demo iniciada*\n"
                f"• Ciclos: {total_cycles}\n"
                f"• Intervalo: {interval}s\n"
                f"• Duración aprox: {int(total_cycles * interval)}s\n\n"
                f"Las alertas aparecerán aquí a medida que se detecten.",
            )

            # Asegurar simulador
            _ensure_simulator_running()

            # Cliente apuntando al simulador
            host = self.cfg["sitrad"]["demo_host"]
            client = SitradClient(host, verify_ssl=False, timeout=5)

            # Notificador Telegram apuntando al chat que pidió la demo
            n_cfg = self.cfg["notifiers"]
            notifier = TelegramNotifier(
                bot_token=n_cfg["telegram_token"],
                chat_id=chat_id,   # ← al chat solicitante
            )

            worker = MonitorWorker(
                client=client,
                notifiers=[notifier],
                cooldown=self.cfg["rules"]["cooldown_seconds"],
                required_consecutive=self.cfg["rules"]["required_consecutive"],
            )

            # Bucle de ciclos
            for i in range(1, total_cycles + 1):
                if self.stop_event.is_set():
                    self.sender.send(chat_id, "⏹️ Demo cancelada por el usuario.")
                    return

                with self.lock:
                    self.cycle = i

                try:
                    worker.run_cycle()
                except Exception as e:
                    logger.exception(f"Error en ciclo {i}: {e}")

                # Avanzar el tick del simulador
                from simulator.fake_sitrad_api import advance_tick
                advance_tick()

                # Espera interrumpible (10 pasos de 0.1s por cada unidad)
                steps = max(1, int(interval * 10))
                for _ in range(steps):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)

                if self.stop_event.is_set():
                    self.sender.send(chat_id, "⏹️ Demo cancelada por el usuario.")
                    return

            self.sender.send(
                chat_id,
                f"✅ *Demo completada*\n"
                f"• {total_cycles} ciclos ejecutados\n"
                f"• Revisa `logs/alerts.jsonl` para el histórico completo.",
            )

        except Exception as e:
            logger.exception(f"Error en demo: {e}")
            try:
                self.sender.send(chat_id, f"❌ Error ejecutando demo:\n{e}")
            except Exception:
                pass
        finally:
            with self.lock:
                self.state = "IDLE"
                self.requested_by = None
                self.started_at = None
                self.cycle = 0