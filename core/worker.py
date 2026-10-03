"""Worker: orquesta cliente + motor de reglas + deduplicación + notificadores."""
import logging
from typing import List
from core.sitrad_client import SitradClient
from core.rule_engine import RuleEngine
from core.deduplication import DeduplicationManager
from core.models import Alert
from notifiers.base import BaseNotifier

logger = logging.getLogger(__name__)


class MonitorWorker:
    """
    Orquesta un ciclo completo:

      1. Lista instrumentos en Sitrad.
      2. Para cada uno, obtiene valores y ejecuta el motor de reglas.
      3. Aplica deduplicación e histéresis temporal.
      4. Despacha alertas a todos los notificadores.
      5. Detecta recuperaciones y las notifica.
    """

    def __init__(self, client: SitradClient, notifiers: List[BaseNotifier],
                 cooldown: int, required_consecutive: int):
        self.client = client
        self.notifiers = notifiers
        self.engine = RuleEngine()
        self.dedup = DeduplicationManager(cooldown, required_consecutive)
        self.cycle = 0
        # Estado expuesto para dashboards / métricas
        self.last_snapshot: List[dict] = []
        self.last_new_alerts: List[Alert] = []

    def run_cycle(self) -> List[Alert]:
        """Ejecuta un ciclo completo. Retorna las alertas NUEVAS enviadas."""
        self.cycle += 1
        logger.info(f"── Ciclo #{self.cycle} ──")

        instruments = self.client.get_instruments()
        if not instruments:
            logger.warning("No se obtuvieron instrumentos de Sitrad.")
            self.last_snapshot = []
            self.last_new_alerts = []
            return []

        # 1. Recolectar valores actuales (esto alimenta también al dashboard)
        values_map = {}
        for inst in instruments:
            values = self.client.get_values(inst["id"])
            if values:
                values_map[inst["id"]] = values
        self.last_snapshot = list(values_map.values())

        # 2. Evaluar cada instrumento y recolectar claves actuales
        current_keys = set()
        all_alerts: List[Alert] = []
        for values in values_map.values():
            alerts = self.engine.evaluate(values)
            for a in alerts:
                current_keys.add(a.key)
                all_alerts.append(a)

        # 3. Despachar solo las alertas que pasan deduplicación
        new_alerts: List[Alert] = []
        for alert in all_alerts:
            if self.dedup.should_send(alert.key):
                new_alerts.append(alert)
                self._dispatch(alert)

        # 4. Detectar recuperaciones (claves activas que ya no aparecen)
        absent = self.dedup.refresh_absent(current_keys)
        for key in absent:
            if self.dedup.mark_resolved(key):
                _, sensor, _ = key.split(":", 2)
                self._notify_recovery(key, "", sensor)
                logger.info(f"RECUPERACIÓN: {key}")

        self.last_new_alerts = new_alerts
        logger.info(
            f"Ciclo #{self.cycle} · {len(values_map)} instrumentos · "
            f"{len(all_alerts)} alertas evaluadas · {len(new_alerts)} emitidas"
        )
        return new_alerts

    # ------------------------------------------------------------------ #
    # DESPACHO
    # ------------------------------------------------------------------ #
    def _dispatch(self, alert: Alert) -> None:
        for n in self.notifiers:
            try:
                n.send(alert)
            except Exception as e:
                logger.error(f"Notificador '{n.name}' falló: {e}")

    def _notify_recovery(self, key: str, device_name: str,
                         sensor: str) -> None:
        for n in self.notifiers:
            try:
                n.send_recovery(key, device_name, sensor)
            except Exception as e:
                logger.error(f"Recovery en '{n.name}' falló: {e}")