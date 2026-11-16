from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from core.contracts import EventCandidate
from core.channel_catalog import format_channels


logger = logging.getLogger(__name__)


# ============================================================
# TIPOS DE MENSAJE
# ============================================================

class MessageKind(str, Enum):
    ALERTA = "ALERTA"
    EVENTO = "EVENTO"
    RESOLUCION = "RESOLUCION"
    RESUMEN = "RESUMEN"
    SALUD = "SALUD"


@dataclass(frozen=True)
class PolicyMessage:
    kind: MessageKind
    key: str
    text: str
    priority: int = 5
    payload: dict[str, Any] = field(default_factory=dict)


# ============================================================
# POLÍTICA
# ============================================================

class ReportPolicy:
    """
    Decide cuándo hablar.

    No envía. Devuelve PolicyMessage; el Reporter los emite.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self.config = config

        # --- Filtros de eventos (heredados de Reporter) ---
        self.trigger_channels = tuple(
            config.get(
                "channels_que_disparan_eventos",
                ("Temp Producto",),
            )
        )
        self.min_delta_primario = float(
            config.get("min_delta_canal_primario", 0.5)
        )
        self.min_event_duration_min = float(
            config.get("min_event_duration_min", 0.5)
        )
        self.max_event_duration_min = float(
            config.get("max_event_duration_min", 5.0)
        )
        self.min_event_magnitude = float(
            config.get("min_event_magnitude", 0.3)
        )
        self.require_return_to_base = bool(
            config.get("require_return_to_base", True)
        )

        # --- Settle / dedup de eventos ---
        self._last_event_fin: datetime | None = None
        self._settle = timedelta(
            seconds=float(config.get("event_settle_seconds", 60))
        )
        self._dedup_margin = timedelta(
            seconds=float(
                config.get("event_dedup_margin_seconds", 60)
            )
        )

        # --- Incidentes (agrupación de anomalías) ---
        self.incident_grouping_seconds = float(
            config.get("incident_grouping_seconds", 60)
        )

        # Tiempo mínimo desde el último avistamiento antes de cerrar
        # un incidente. Sin histéresis, cualquier tick sin anomalía
        # cierra el incidente y el siguiente lo reabre.
        self.incident_clear_seconds = float(
            config.get("incident_clear_seconds", 120)
        )
        self._open_incidents: dict[str, dict[str, Any]] = {}
        self._channel_to_incident: dict[str, str] = {}
        self._incident_counter: int = 0
        self.alert_escalation_ratio = float(
            config.get("alert_escalation_ratio", 1.5)
        )

        # --- Cola de resumen ---
        self._pending_events: list[EventCandidate] = []
        self._last_summary_at: datetime | None = None
        self.summary_hours_local = tuple(
            config.get("summary_hours_local", (6, 14, 22))
        )
        self.local_tz = ZoneInfo(
            config.get("local_tz", "America/Bogota")
        )

        # --- Rate limit ---
        self.max_messages_per_hour = int(
            config.get("max_messages_per_hour", 12)
        )
        self._sent_at: deque[datetime] = deque()

        # --- Dedup por key ---
        self._last_key_at: dict[str, datetime] = {}
        self.cooldown = timedelta(
            seconds=float(config.get("policy_cooldown_seconds", 300))
        )

    # ========================================================
    # EVENTOS
    # ========================================================
    def decide_events(
        self,
        events: list[EventCandidate],
        now: datetime,
    ) -> tuple[list[EventCandidate], list[PolicyMessage]]:
        """
        Los eventos transitorios NO generan ALERTA inmediata.

        Se aceptan (se persisten en events.jsonl) y se acumulan
        para el próximo RESUMEN del turno. La ALERTA en tiempo
        real se reserva para las anomalías sostenidas que maneja
        `decide_anomalies`.

        Esto cumple el objetivo del operador: diagnóstico del
        turno, no alarma por cada ciclo del compresor.
        """
        accepted: list[EventCandidate] = []

        for event in events:

            # Dedup primero. El EventEngine re-emite el mismo evento
            # en cada tick mientras la ventana lo contenga. Saltar
            # sin loguear.
            if self._last_event_fin is not None:
                if event.inicio <= self._last_event_fin:
                    continue

            accepted.append(event)
            self._last_event_fin = event.fin

            # Todo evento aceptado va al resumen.
            self._pending_events.append(event)

        return accepted, []


    # ========================================================
    # ANOMALÍAS
    # ========================================================
    def decide_anomalies(
        self,
        active_states: dict[str, Any],
        now: datetime,
    ) -> list[PolicyMessage]:
        """
        Agrupa los estados activos por incidente.

        Cambios respecto a la versión anterior:

        - No cierra por cambio de `kind`. Los tipos se acumulan
          dentro del mismo incidente. `persistent_drift` y
          `out_of_profile` que alternan no cierran/abren cada vez.
        - Histéresis de cierre. Un incidente solo se cierra cuando
          pasan `incident_clear_seconds` desde el último avistamiento.
        - Agrupación por actividad reciente, no por `opened_at`.
        """
        messages: list[PolicyMessage] = []
        set(active_states.keys())

        for channel, state in active_states.items():

            # Buscar un incidente abierto que cubra este canal o
            # haya tenido actividad en la ventana reciente.
            key = self._channel_to_incident.get(channel)

            if key is None or key not in self._open_incidents:
                key = self._recent_open_incident(now, state.kind)

            is_new = key is None
            if is_new:
                key = self._new_incident(state, now)

            self._channel_to_incident[channel] = key
            incident = self._open_incidents[key]
            incident["channels"].add(channel)
            incident["states"][channel] = state
            incident["last_seen_at"] = now
            incident["kinds"].add(state.kind)
            incident["severity_max"] = max(
                incident.get("severity_max", 0.0),
                state.severity,
            )

            if is_new:
                msg = self._emit_incident_open(key, incident, now)
                if msg is not None:
                    messages.append(msg)
            else:
                msg = self._maybe_emit_escalation(key, incident, now)
                if msg is not None:
                    messages.append(msg)

        # Cerrar incidentes con histéresis.
        for key in list(self._open_incidents.keys()):
            incident = self._open_incidents[key]
            last_seen = incident["last_seen_at"]
            idle = (now - last_seen).total_seconds()
            if idle >= self.incident_clear_seconds:
                messages.extend(
                    self._close_incident(key, incident, now)
                )

        return messages

    # Tipos de una misma familia se acumulan en un incidente. Una falla de
    # otra familia abre el suyo en vez de esconderse en uno ya abierto.
    _FAMILIES = {
        "out_of_profile": "estadistica",
        "persistent_drift": "estadistica",
        "missing": "comunicacion",
        "offline": "comunicacion",
        "err": "comunicacion",
    }

    @classmethod
    def _family(cls, kind: str) -> str:
        return cls._FAMILIES.get(kind, kind)

    def _recent_open_incident(
        self,
        now: datetime,
        kind: str | None = None,
    ) -> str | None:
        cutoff = now - timedelta(seconds=self.incident_clear_seconds)
        fam = self._family(kind) if kind is not None else None
        best_key: str | None = None
        best_seen: datetime | None = None
        for key, inc in self._open_incidents.items():
            if inc["last_seen_at"] < cutoff:
                continue
            if fam is not None and fam not in {
                self._family(k) for k in inc["kinds"]
            }:
                continue
            if best_seen is None or inc["last_seen_at"] > best_seen:
                best_seen = inc["last_seen_at"]
                best_key = key
        return best_key


    def _new_incident(self, state: Any, now: datetime) -> str:
        self._incident_counter += 1
        key = f"inc{self._incident_counter}"
        self._open_incidents[key] = {
            "kind": state.kind,
            "kinds": {state.kind},   # todos los tipos vistos
            "channels": set(),
            "states": {},
            "opened_at": now,
            "last_seen_at": now,
            "severity_max": state.severity,
            "last_emit_severity": None,
            "last_emit_at": None,
            "escalated_at": None,
            "opened_sent": False,    # ¿el OPEN pasó el rate limit?
        }
        return key
    def _emit_incident_open(
        self,
        key: str,
        incident: dict[str, Any],
        now: datetime,
    ) -> PolicyMessage | None:
        # SALUD sólo para problemas de comunicación reales.
        # Cualquier otra anomalía estructural o estadística (deriva,
        # fuera de perfil, señal plana) va como ALERTA.
        COMMS_KINDS = {"missing", "offline", "err"}

        kind = (
            MessageKind.SALUD
            if incident["kind"] in COMMS_KINDS
            else MessageKind.ALERTA
        )
        channels_sorted = sorted(incident["channels"])
        n = len(channels_sorted)
        text = (
            f"[INCIDENTE] {incident['kind']}\n"
            f"Canales: {n} ({format_channels(channels_sorted)})\n"
            f"Severidad máxima: {incident['severity_max']:.2f}\n"
            f"Desde: {incident['opened_at'].isoformat()}"
        )
        payload = {
            "incident_kind": incident["kind"],
            "channels": channels_sorted,
            "opened_at": incident["opened_at"].isoformat(),
            "severity_max": incident["severity_max"],
        }
        msg = self._make(
            kind=kind,
            key=f"incident_open:{key}",
            text=text,
            priority=1,
            now=now,
            payload=payload,
        )

        incident["last_emit_severity"] = incident["severity_max"]
        incident["last_emit_at"] = now

        if msg is not None:
            incident["opened_sent"] = True

        return msg

    def _maybe_emit_escalation(
        self,
        key: str,
        incident: dict[str, Any],
        now: datetime,
    ) -> PolicyMessage | None:
        if incident.get("escalated_at") is not None:
            return None

        prev_sev = incident.get("last_emit_severity") or 0.0
        if (
            incident["severity_max"]
            <= prev_sev * self.alert_escalation_ratio
        ):
            return None


        channels_sorted = sorted(incident["channels"])
        text = (
            f"[INCIDENTE] {incident['kind']} escala\n"
            f"Canales: {len(channels_sorted)} "
            f"({format_channels(channels_sorted)})\n"
            f"Severidad máxima: {incident['severity_max']:.2f}"
        )
        payload = {
            "incident_kind": incident["kind"],
            "channels": channels_sorted,
            "severity_max": incident["severity_max"],
            "opened_at": incident["opened_at"].isoformat(),
        }

        msg = self._make(
            kind=MessageKind.ALERTA,
            key=f"incident_escalate:{key}:{now.isoformat()}",
            text=text,
            priority=2,
            now=now,
            payload=payload,
        )
        incident["escalated_at"] = now
        incident["last_emit_severity"] = incident["severity_max"]
        incident["last_emit_at"] = now
        return msg

    def _close_incident(
        self,
        key: str,
        incident: dict[str, Any],
        now: datetime,
    ) -> list[PolicyMessage]:
        self._open_incidents.pop(key, None)
        for ch in incident["channels"]:
            if self._channel_to_incident.get(ch) == key:
                self._channel_to_incident.pop(ch, None)

        duration_min = max(
            0.0,
            (now - incident["opened_at"]).total_seconds() / 60,
        )


        # Sin OPEN enviado no hay RESOLUCION que emitir. Evita
        # resoluciones huérfanas cuando el rate limit o el
        # cooldown suprimieron la apertura.
        if not incident.get("opened_sent", False):
            return []

        channels_sorted = sorted(incident["channels"])
        text = (
            f"[INCIDENTE RESUELTO] {incident['kind']}\n"
            f"Canales: {len(channels_sorted)} "
            f"({format_channels(channels_sorted)})\n"
            f"Duración: {duration_min:.1f} min"
        )
        payload = {
            "incident_kind": incident["kind"],
            "channels": channels_sorted,
            "duration_min": duration_min,
        }
        msg = self._make(
            kind=MessageKind.RESOLUCION,
            key=f"incident_close:{key}",
            text=text,
            priority=2,
            now=now,
            payload=payload,
        )
        return [msg] if msg is not None else []

    # ========================================================
    # RESUMEN
    # ========================================================

    def decide_summary(self, now: datetime) -> list[PolicyMessage]:
        if not self._summary_is_due(now):
            return []
        if not self._pending_events:
            return []
        if self._last_summary_at is not None:
            if now - self._last_summary_at < timedelta(hours=1):
                return []

        key = (
            "summary:"
            + now.astimezone(self.local_tz).strftime("%Y-%m-%d-%H")
        )
        payload = {
            "events": [
                {
                    "patron": e.patron,
                    "duracion_min": e.duracion_min,
                    "variables_afectadas": list(e.variables_afectadas),
                }
                for e in self._pending_events
            ],
            "window_hours": 8.0,
        }
        msg = self._make(
            kind=MessageKind.RESUMEN,
            key=key,
            text=self._format_summary(now),
            priority=5,
            now=now,
            payload=payload,
        )
        if msg is None:
            return []

        self._pending_events.clear()
        self._last_summary_at = now
        return [msg]

    def _summary_is_due(self, now: datetime) -> bool:
        if not self.summary_hours_local:
            return False
        local = now.astimezone(self.local_tz)
        return local.hour in self.summary_hours_local

    def _format_summary(self, now: datetime) -> str:
        local = now.astimezone(self.local_tz)
        lines = [
            f"[RESUMEN] {local.strftime('%Y-%m-%d %H:%M')}",
            f"Eventos: {len(self._pending_events)}",
        ]
        top = sorted(
            self._pending_events,
            key=lambda e: -e.duracion_min,
        )[:5]
        for e in top:
            lines.append(
                f"  · {e.patron} {e.duracion_min:.1f} min "
                f"({', '.join(e.variables_afectadas)})"
            )
        if len(self._pending_events) > 5:
            lines.append(
                f"  ... y {len(self._pending_events) - 5} más"
            )
        return "\n".join(lines)

    # ========================================================
    # SALUD
    # ========================================================
    def decide_health(
        self,
        now: datetime,
        inactive_channels: list[str],
    ) -> list[PolicyMessage]:
        if not inactive_channels:
            return []

        # ----------------------------------------------------------
        # 1. No duplicar SALUD si ya hay un incidente abierto sobre
        #    esos canales. QualityGuard emite 'missing' vía
        #    decide_anomalies y ese incidente ya manda SALUD al abrir.
        # ----------------------------------------------------------
        channels_open = {
            ch
            for ch, key in self._channel_to_incident.items()
            if key in self._open_incidents
        }
        remaining = sorted(
            ch for ch in inactive_channels if ch not in channels_open
        )
        if not remaining:
            return []

        # ----------------------------------------------------------
        # 2. Clave estable: el conjunto de canales NO forma parte
        #    de la clave. Así {A} → {A,B} → {A,B,C} no burla el
        #    cooldown. El texto sí refleja el conjunto actual.
        # ----------------------------------------------------------
        key = "health:stale"
        msg = self._make(
            kind=MessageKind.SALUD,
            key=key,
            text=(
                "[SALUD] Sin datos de: "
                + ", ".join(remaining)
            ),
            priority=1,
            now=now,
        )
        return [msg] if msg is not None else []

    # ========================================================
    # RATE LIMIT Y DEDUP
    # ========================================================

    def _make(
        self,
        *,
        kind: MessageKind,
        key: str,
        text: str,
        priority: int,
        now: datetime,
        payload=None,
    ) -> PolicyMessage | None:
        if not self._key_allowed(key, now):
            return None
        self._last_key_at[key] = now
        self._sent_at.append(now)
        return PolicyMessage(
            kind=kind,
            key=key,
            text=text,
            priority=priority,
            payload=payload or {},
        )

    def _key_allowed(self, key: str, now: datetime) -> bool:
        if not self._within_rate_limit(now):
            return False
        last = self._last_key_at.get(key)
        if last is not None and now - last < self.cooldown:
            return False
        return True

    def _within_rate_limit(self, now: datetime) -> bool:
        cutoff = now - timedelta(hours=1)
        while self._sent_at and self._sent_at[0] < cutoff:
            self._sent_at.popleft()
        return len(self._sent_at) < self.max_messages_per_hour

    # ========================================================
    # INTROSPECCIÓN
    # ========================================================

    def reset(self) -> None:
        self._open_incidents.clear()
        self._channel_to_incident.clear()
        self._incident_counter = 0
        self._pending_events.clear()
        self._sent_at.clear()
        self._last_key_at.clear()
        self._last_event_fin = None
        self._last_summary_at = None

    @property
    def open_incidents(self) -> dict[str, dict[str, Any]]:
        return dict(self._open_incidents)

    @property
    def pending_events(self) -> list[EventCandidate]:
        return list(self._pending_events)