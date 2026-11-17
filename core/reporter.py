from __future__ import annotations
# core.reporter
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from core.contracts import ChannelReading
# ── FASE 2 ──────────────────────────────────────────────────────
# Importamos AnomalyState solo para tipado; el Reporter no
# construye instancias, solo las consume.
from analysis.anomaly_detector import AnomalyState
# ────────────────────────────────────────────────────────────────


logger = logging.getLogger(__name__)


class Reporter:
    """
    Orquestador principal del sistema de monitoreo.

    Reporter NO interpreta los datos.

    Sus responsabilidades son:

        DataSource
            ↓
        SeriesStore
            ↓
        EventEngine
            ↓
        CavaAgent
            ↓
        Telegram / FileLog

    Tiene cuatro disparadores:

        1. Evento transitorio relevante y cerrado
        2. Anomalía persistente (Fase 2)
        3. Informe periódico
        4. Consulta interactiva

    El análisis matemático pertenece a AnalysisEngine.
    La detección de patrones transitorios pertenece a EventEngine.
    La detección de estados anómalos pertenece a AnomalyDetector.
    La interpretación pertenece a CavaAgent.
    """

    def __init__(
        self,
        source,
        store,
        event_engine,
        agent,
        context_serializer,
        telegram,
        filelog,
        clock,
        config,
        # ── FASE 2 · opcionales ─────────────────────────────────
        profile=None,
        detector=None,
        quality_guard=None,
        policy=None,
        # ────────────────────────────────────────────────────────
    ) -> None:

        if source is None:
            raise ValueError(
                "Reporter requiere una fuente de datos."
            )

        if store is None:
            raise ValueError(
                "Reporter requiere un SeriesStore."
            )

        if event_engine is None:
            raise ValueError(
                "Reporter requiere un EventEngine."
            )

        if agent is None:
            raise ValueError(
                "Reporter requiere un Agent."
            )

        if context_serializer is None:
            raise ValueError(
                "Reporter requiere un ContextSerializer."
            )

        if clock is None:
            raise ValueError(
                "Reporter requiere un Clock."
            )

        self.source = source
        self.store = store
        self.event_engine = event_engine
        self.agent = agent
        self.context_serializer = context_serializer
        self.telegram = telegram
        self.filelog = filelog
        self.clock = clock
        self.config = config

        # =========================================================
        # Configuración de eventos
        # =========================================================
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
            config.get(
                "min_event_duration_min",
                0.5,
            )
        )

        self.min_event_magnitude = float(
            config.get(
                "min_event_magnitude",
                0.3,
            )
        )

        self.max_event_duration_min = float(
            config.get(
                "max_event_duration_min",
                5.0,
            )
        )
        self.heartbeat_every_minutes = float(
            config.get("heartbeat_every_minutes", 0)
        )
        self._last_heartbeat: datetime | None = None

        # ---------------------------------------------------------
        # Requisito de recuperación
        # ---------------------------------------------------------
        #
        # Algunos fenómenos (p. ej. una deriva térmica) son
        # cambios persistentes que no vuelven a la línea base.
        # Exigir volvio_a_base=True los descarta por diseño.
        #
        # Por defecto True para no alterar el comportamiento
        # histórico del Reporter.
        # ---------------------------------------------------------

        self.require_return_to_base = bool(
            config.get(
                "require_return_to_base",
                True,
            )
        )

        # =========================================================
        # Polling
        # =========================================================

        self.poll_seconds = float(
            config.get(
                "polling_seconds",
                5,
            )
        )

        # =========================================================
        # Frecuencia de análisis
        # =========================================================
        #
        # El ciclo principal corre cada polling_seconds (5 s), pero
        # EventEngine reanaliza toda la ventana (30 min) en cada paso.
        # Con analysis_every_seconds > 0 se limita la frecuencia del
        # análisis, manteniendo el polling original.
        #
        # analysis_every_seconds = 0 desactiva el throttle.
        # =========================================================

        self.analysis_every_seconds = float(
            config.get(
                "analysis_every_seconds",
                15,
            )
        )

        self._last_analysis_at: datetime | None = None

        # =========================================================
        # Informes periódicos
        # =========================================================

        self.window_minutes = float(
            config.get(
                "report_window_minutes",
                30,
            )
        )

        self.device_name = config.get(
            "device_name",
            "Sistema Sitrad",
        )

        # =========================================================
        # Estado temporal
        # =========================================================

        self._started_at = self.clock.now()

        self._last_periodic_report: datetime | None = None

        # =========================================================
        # Deduplicación de eventos
        # =========================================================
        #
        # Guardamos únicamente el final del último evento
        # reportado.
        #
        # Si un evento nuevo comienza antes de:
        #
        #     último_fin + margen
        #
        # se considera otra representación del evento anterior.
        #
        # Esto evita que diferentes ejecuciones de EventEngine
        # generen múltiples reportes del mismo fenómeno.
        # =========================================================

        self._last_processed_fin: datetime | None = None

        # Tiempo que debe transcurrir desde el 'fin' del evento antes
        # de reportarlo, para que el EventEngine disponga de datos
        # posteriores y pueda clasificar bien la forma (recuperación).
        self._settle = timedelta(
            seconds=float(
                config.get(
                    "event_settle_seconds",
                    60,
                )
            )
        )

        self._dedup_margin = timedelta(
            seconds=float(
                config.get(
                    "event_dedup_margin_seconds",
                    60,
                )
            )
        )

        # ── FASE 2 ──────────────────────────────────────────────
        # =========================================================
        # Anomalías persistentes
        # =========================================================
        #
        # Rama paralela a _process_events:
        #
        #   _process_events     → TRANSITORIOS (subidas, escalones)
        #   _process_anomalies  → ESTADOS (deriva, sensor congelado,
        #                                  caída de comunicación,
        #                                  fuera de perfil)
        #
        # Si profile o detector son None, la rama queda desactivada
        # y el Reporter se comporta exactamente como antes de Fase 2.
        # =========================================================

        self.profile = profile
        self.detector = detector
        self.quality_guard = quality_guard
        self.policy = policy

        # Informe de temperatura (solo termocupla): alertas e informes por episodio.
        from analysis.cooling_report import CoolingConfig, CoolingMonitor
        _c = dict(config.get("cooling", {}) or {})
        _c.setdefault("camera_name", config.get("device_name", "la cámara"))
        _c.setdefault("timezone", (config.get("operations", {}) or {}).get(
            "timezone", "America/Bogota"))
        self._cooling_enabled = bool(_c.get("enabled", True))
        self.cooling_cfg = CoolingConfig.from_dict(_c)
        self._llm_analysis = bool(_c.get("llm_analysis", False))   # párrafo de análisis por LLM (opcional)
        self.cooling_monitor = CoolingMonitor(self.cooling_cfg)
        # Producto que hay en la cámara (lo registra el operario por chat).
        from analysis.product_log import ProductLog
        self.product_log = ProductLog(
            _c.get("products_path", _c.get("product_log_path", "products.json")),
            tz_name=_c["timezone"])
        # Libro de casos: lo que el bot propone y lo que el ingeniero corrige por chat.
        from analysis.feedback_log import CaseBook
        self.case_book = CaseBook(_c.get("cases_path", "logs/cooling_cases.jsonl"),
                                  _c.get("feedback_path", "logs/feedback.jsonl"), tz_name=_c["timezone"])
        self.cooling_monitor.on_case = self.case_book.record_case
        # Historial persistente de episodios cerrados (preguntas «episodios de ayer», «resumen de episodios»).
        from analysis.episode_history import EpisodeHistory
        self.episode_history = EpisodeHistory(
            (config.get("storage", {}) or {}).get("episodes_log", "logs/episodes.jsonl"), tz_name=_c["timezone"])
        from collections import deque
        self.sitrad_alarms = deque(maxlen=50)          # alarmas de SITRAD recibidas (para preguntarle al bot)
        if getattr(self, "agent", None) is not None:
            self.agent.case_book = self.case_book
            self.agent.episode_history = self.episode_history
            self.agent.sitrad_alarms = self.sitrad_alarms
            self.agent.sitrad_live = hasattr(self.source, "new_alarms")
            self.agent.product_log = self.product_log
            # Por ahora el agente solo habla de la termocupla y el controlador.
            # "agent_scope": "todo" en el bloque cooling devuelve Multipower.
            self.agent.scope_controller_only = (_c.get("agent_scope", "controlador") != "todo")
        # Con el informe activo, los mensajes técnicos de incidente se apagan
        # (siguen en el registro de casos) salvo que se pidan explícitamente.
        self._tech_messages = bool(
            (config.get("analysis", {}) or {}).get(
                "technical_incident_messages", not self._cooling_enabled))
        if getattr(self, "agent", None) is not None:
            self.agent.cooling_cfg = self.cooling_cfg

        # El diagnóstico de un incidente abierto se reevalúa: si la causa
        # principal cambia de forma estable, se envía una ACTUALIZACIÓN.
        from core.incident_tracker import DiagnosisTracker
        _a = config.get("analysis", {}) or {}
        self._diagnosis_updates = bool(_a.get("diagnosis_updates", True))
        self.diagnosis_tracker = DiagnosisTracker(
            every_s=float(_a.get("diagnosis_update_every_seconds", 240)),
            confirm=int(_a.get("diagnosis_update_confirm", 2)),
            min_confidence=float(_a.get("diagnosis_update_min_confidence", 0.5)),
        )

        # Canal → dict con 'state' (AnomalyState) y 'opened_at'.
        self._open_anomalies: dict[str, dict[str, Any]] = {}

        # Frecuencia de análisis de anomalías.
        self.anomaly_every_seconds = float(
            config.get("anomaly_every_seconds", 30)
        )
        self._last_anomaly_at: datetime | None = None

        # Tiempo mínimo para cerrar una anomalía abierta, para
        # no cerrar por un tick bueno aislado.
        self.anomaly_clear_seconds = float(
            config.get("anomaly_clear_seconds", 60)
        )
        # ────────────────────────────────────────────────────────

        # =========================================================
        # Calentamiento del historial
        # =========================================================
        #
        # Si hay perfil y memoria, se alimentan del histórico ya
        # persistido. Solo se hace una vez, al construir el
        # Reporter. Es idempotente respecto al FileLog.
        #
        # En tests se desactiva con warmup_on_start=False para no
        # ralentizar la construcción.

        if config.get("warmup_on_start", False):
            self._warmup_history()

    # =============================================================
    # CICLO PRINCIPAL
    # =============================================================

    def run_forever(self) -> None:

        logger.info(
            "Reporter iniciado. polling=%ss",
            self.poll_seconds,
        )

        if self.telegram is not None:

            start_polling = getattr(
                self.telegram,
                "start_polling",
                None,
            )

            if start_polling is not None:

                try:

                    start_polling()

                except Exception:

                    logger.exception(
                        "No fue posible iniciar "
                        "el polling de Telegram."
                    )

        try:

            while True:

                cycle_started = time.monotonic()

                now = self.clock.now()

                try:
                    now = self.clock.now()
                    self.run_one_cycle(now)
                except Exception:
                    logger.exception(
                        "Error no controlado en el ciclo principal."
                    )

                elapsed = (
                    time.monotonic()
                    - cycle_started
                )

                sleep_seconds = max(
                    0.0,
                    self.poll_seconds - elapsed,
                )

                if sleep_seconds > 0:

                    time.sleep(
                        sleep_seconds
                    )

        finally:

            if self.telegram is not None:

                stop_polling = getattr(
                    self.telegram,
                    "stop_polling",
                    None,
                )

                if stop_polling is not None:

                    try:
                        stop_polling()
                    except Exception:
                        logger.exception(
                            "Error deteniendo polling de Telegram."
                        )

    # =============================================================
    # INGESTIÓN
    # =============================================================

    def _ingest(
        self,
        now: datetime,
    ) -> None:
        """
        Obtiene un Snapshot desde la fuente y lo almacena.

        No realiza análisis.
        """

        try:

            snapshot = self.source.poll(
                now
            )

        except Exception:

            logger.exception(
                "Error durante el polling de la fuente."
            )

            return

        if snapshot is None:

            logger.debug(
                "La fuente no produjo Snapshot."
            )

            return

        for reading in snapshot.readings:

            try:

                # ── FASE 3 ──────────────────────────────────
                # QualityGuard puede degradar a FROZEN si el
                # canal lleva N lecturas idénticas.
                if self.quality_guard is not None:
                    reading = self.quality_guard.apply(reading)
                # ────────────────────────────────────────────

                self.store.ingest(
                    reading
                )

                if self.filelog is not None:
                    self.filelog.write_observation(
                        reading
                    )

            except Exception:

                logger.exception(
                    "Error incorporando lectura "
                    "al SeriesStore. channel=%s",
                    reading.channel,
                )

    # =============================================================
    # EVENTOS TRANSITORIOS
    # =============================================================

    def _process_events(
        self,
        now: datetime,
    ) -> None:
        """
        Procesa los eventos detectados por EventEngine.

        El análisis se ejecuta como máximo una vez cada
        analysis_every_seconds. Los ticks intermedios no
        recalculan nada: los eventos son fenómenos que
        persisten varios ciclos.
        """

        # ---------------------------------------------------------
        # Throttle de análisis
        # ---------------------------------------------------------

        if self.analysis_every_seconds > 0:

            if (
                self._last_analysis_at is not None
                and (
                    now - self._last_analysis_at
                ).total_seconds()
                < self.analysis_every_seconds
            ):
                return

        # ---------------------------------------------------------
        # Obtener series
        # ---------------------------------------------------------

        series = self._get_analysis_series()

        if not series:
            return

        try:

            events = self.event_engine.step(
                series
            )

        except Exception:

            logger.exception(
                "Error ejecutando EventEngine."
            )

            return

        # ---------------------------------------------------------
        # Marcar la marca temporal del análisis
        # ---------------------------------------------------------
        #
        # Se hace después de step() para no penalizar un fallo
        # transitorio: si step() explota, el siguiente tick lo
        # reintenta.
        # ---------------------------------------------------------

        self._last_analysis_at = now

        if not events and self.policy is None:
            return

        # ---------------------------------------------------------
        # Orden cronológico
        # ---------------------------------------------------------
        #
        # El evento más antiguo se procesa primero.
        # Esto permite que _last_processed_fin avance
        # cronológicamente.
        # ---------------------------------------------------------

        events = sorted(
            events,
            key=lambda event: event.inicio,
        )

        # ── FASE 4 ─────────────────────────────────────────────
        if self.policy is not None:
            accepted, messages = self.policy.decide_events(events, now)
            for event in accepted:
                self._record_event(event)
            for msg in messages:
                self._send_policy_message(msg)
            return
        # ────────────────────────────────────────────────────────


    # =============================================================
    # ── FASE 2 ──────────────────────────────────────────────────
    # ANOMALÍAS PERSISTENTES
    # =============================================================

    def _process_anomalies(self, now: datetime) -> None:
        """
        Rama paralela a _process_events.

        _process_events detecta TRANSITORIOS (subidas, escalones).
        _process_anomalies detecta ESTADOS (deriva persistente,
        sensor congelado, caída de comunicación, fuera de perfil).

        Los estados se acumulan hasta que la condición desaparece.
        Se emite un mensaje al abrir la anomalía y otro al cerrarla.

        Si profile o detector son None, la rama queda desactivada.
        """

        if self.detector is None or self.profile is None:
            return

        # ---------------------------------------------------------
        # Throttle
        # ---------------------------------------------------------

        if self.anomaly_every_seconds > 0:
            if (
                self._last_anomaly_at is not None
                and (now - self._last_anomaly_at).total_seconds()
                < self.anomaly_every_seconds
            ):
                return

        series = self._get_analysis_series()
        if not series:
            return

        # ---------------------------------------------------------
        # Alimentar el perfil con las series recientes.
        # ---------------------------------------------------------

        try:
            for readings in series.values():
                self.profile.ingest_many(readings)
        except Exception:
            logger.exception(
                "Error alimentando BehaviorProfile."
            )

        # El perfil debe recalcularse periódicamente.
        try:
            self.profile.fit()
        except Exception:
            logger.exception(
                "Error ajustando BehaviorProfile."
            )
        # ---------------------------------------------------------
        # Detección principal.
        # ---------------------------------------------------------

        try:
            states: list[AnomalyState] = self.detector.detect(
                series, self.profile,
            )
        except Exception:
            logger.exception(
                "Error ejecutando AnomalyDetector."
            )
            return

        # ---------------------------------------------------------
        # Anomalías de calidad (QualityGuard).
        # ---------------------------------------------------------

        if self.quality_guard is not None:
            try:
                all_readings = [
                    r for readings in series.values() for r in readings
                ]
                for issue in self.quality_guard.issues(all_readings):
                    # Preservar el kind real del issue. NO forzar 'missing':
                    # QualityGuard también reporta señales planas ('frozen'),
                    # que no son fallos de comunicación y no deben ir a SALUD.
                    raw_kind = getattr(issue, "kind", None)

                    if raw_kind is None:
                        # Fallback: inferir desde las calidades observadas.
                        quals = {getattr(q, "value", q) for q in issue.qualities}
                        if quals & {"missing", "offline", "err"}:
                            raw_kind = "missing"
                        else:
                            raw_kind = "flat_signal"

                    # AnomalyKind sólo admite
                    # 'out_of_profile' | 'flat_signal' | 'missing' | 'persistent_drift'.
                    # 'frozen' u otros se mapean a flat_signal.
                    if raw_kind not in {
                        "out_of_profile", "flat_signal",
                        "missing", "persistent_drift",
                    }:
                        raw_kind = "flat_signal"

                    states.append(AnomalyState(
                        channel=issue.channel,
                        kind=raw_kind,
                        severity=min(1.0, issue.consecutive_bad / 10.0),
                        since=issue.since,
                        evidence={
                            "consecutive_bad": issue.consecutive_bad,
                            "qualities": list(issue.qualities),
                        },
                    ))
            except Exception:
                logger.exception("Error consultando QualityGuard.")

        self._last_anomaly_at = now

        # ── FASE 4 ─────────────────────────────────────────────
        if self.policy is not None:
            # Elegir el estado más severo por canal. El operador
            # decide por el peor estado, no por el último que llegó.
            active_by_channel: dict[str, AnomalyState] = {}
            for state in states:
                prev = active_by_channel.get(state.channel)
                if prev is None or state.severity > prev.severity:
                    active_by_channel[state.channel] = state

            for msg in self.policy.decide_anomalies(
                active_by_channel, now,
            ):
                self._send_policy_message(msg)
            self._check_diagnosis_updates(now)
            return


    # ────────────────────────────────────────────────────────────

    # =============================================================
    # MANEJO DE EVENTO
    # =============================================================


    # =============================================================
    # RELEVANCIA
    # =============================================================


    # =============================================================
    # INFORME PERIÓDICO
    # =============================================================

    def _process_periodic_report(self, now: datetime) -> None:
        """Resumen de turno: lo decide la política; el texto es el informe de temperatura."""
        if self.policy is None:
            return
        for msg in self.policy.decide_summary(now):
            self._send_policy_message(msg)

    # =============================================================
    # TEMPORIZADOR
    # =============================================================


    # =============================================================
    # CONSULTAS INTERACTIVAS
    # =============================================================

    def _process_commands(self) -> None:
        """
        Consume preguntas que TelegramClient ya recibió
        mediante getUpdates.

        Reporter no realiza comunicación HTTP con Telegram.

        Flujo:

            TelegramClient
                ↓
            cola interna
                ↓
            pop_command()
                ↓
            CavaAgent.answer_question()
                ↓
            _send()
        """

        if self.telegram is None:
            return

        has_command = getattr(
            self.telegram,
            "has_command",
            None,
        )

        pop_command = getattr(
            self.telegram,
            "pop_command",
            None,
        )

        if (
            has_command is None
            or pop_command is None
        ):
            logger.warning(
                "El cliente Telegram no implementa "
                "has_command()/pop_command()."
            )
            return

        while True:

            try:

                if not has_command():
                    break

                question = pop_command()

            except Exception:

                logger.exception(
                    "Error obteniendo pregunta de Telegram."
                )

                break

            if not question:
                continue

            logger.info(
                "Procesando pregunta del usuario: %s",
                question,
            )

            # Identidad del usuario (TelegramReporter la adjunta; con
            # ConsoleNotifier la pregunta es un str simple y no hay memoria).
            user_key = getattr(question, "user_key", "") or ""
            user_name = getattr(question, "user_name", "") or ""
            command = getattr(question, "command", None)
            prefix = f"{user_name}: " if user_name else ""

            if command == "forget":
                n = self.agent.forget_user(user_key)
                self._send(
                    f"{prefix}listo, olvidé {n} conversación(es) tuya(s).",
                    reason="consulta",
                )
                continue

            if command == "history":
                convs = getattr(self.agent, "conversations", None)
                turns = convs.recent(user_key, 5) if convs else []
                body = (
                    "\n".join(f"• {t.question}" for t in turns)
                    if turns else "Aún no tengo preguntas tuyas guardadas."
                )
                self._send(f"{prefix}tus últimas preguntas:\n{body}", reason="consulta")
                continue

            try:

                if user_key:
                    answer = self.agent.answer_question(
                        question, user_key=user_key, user_name=user_name,
                    )
                else:
                    answer = self.agent.answer_question(question)

            except Exception:

                logger.exception(
                    "Error respondiendo pregunta mediante CavaAgent."
                )

                self._send(
                    "No pude procesar la pregunta en este momento.",
                    reason="consulta_error",
                )

                continue

            if not answer:
                logger.warning("CavaAgent no produjo respuesta.")
                self._send(
                    "No pude procesar la pregunta en este momento. "
                    "Intenta reformularla o usa nombres exactos de canal "
                    "(por ejemplo: 'Temp Producto').",
                    reason="consulta_sin_respuesta",
                )
                continue

            # Respuesta con imagen: se envía como foto; si no se puede, cae a texto.
            if isinstance(answer, dict):
                if self._handle_photo_answer(answer):
                    continue
                answer = answer.get("caption") or str(answer)

            # En un grupo hay varios usuarios: se indica a quién se responde.
            self._send(
                f"{prefix}{answer}",
                reason="consulta",
            )

    def _handle_photo_answer(self, answer: dict) -> bool:
        """Envía answer["__photo__"] con su caption. True si se envió; False si no hay foto o el
        notificador no soporta fotos (ConsoleNotifier) o falló: el llamador responde entonces con texto."""
        path = answer.get("__photo__")
        caption = answer.get("caption") or ""
        if not path or not hasattr(self.telegram, "send_photo"):
            return False
        try:
            return bool(self.telegram.send_photo(path, caption))
        except Exception:
            logger.exception("Fallo enviando foto; se envía texto.")
            return False

    # =============================================================
    # CONTEXTO
    # =============================================================


    # =============================================================
    # MEMORIA HISTÓRICA
    # =============================================================

    def _memory(self):
        return getattr(self.agent, "memory", None)

    def _history_for(self, events) -> list[dict]:
        """history[i] corresponde a events[i]."""

        memory = self._memory()

        if memory is None:
            return []

        result = []

        for event in events:
            try:
                result.append(memory.find_similar(event))
            except Exception:
                logger.exception(
                    "Error consultando memoria histórica."
                )
                result.append({})

        return result

    def _record_event(self, event) -> None:
        """Persiste el evento (events.jsonl) y lo añade a la memoria."""

        memory = self._memory()

        try:
            if memory is not None:
                memory.record_event(
                    event,
                    device=self.device_name,
                )
            elif self.filelog is not None:
                self.filelog.write_event(
                    event,
                    device=self.device_name,
                )
        except Exception:
            logger.exception(
                "Error registrando el evento."
            )

    def _build_period_context(
        self,
    ) -> str:

        series = self._get_analysis_series()

        if not series:

            return ""

        metrics = self._calculate_context_metrics(
            series
        )

        events = self._get_recent_events(
            series
        )

        return self.context_serializer.build_context(
            metrics=metrics,
            events=events,
            all_series=series,
            device_name=self.device_name,
            history=self._history_for(events),
        )

    # =============================================================
    # DATOS DEL STORE
    # =============================================================

    def _get_analysis_series(
        self,
    ) -> dict[str, list[ChannelReading]]:
        """
        Obtiene las series del dispositivo que está siendo monitoreado.

        SeriesStore conserva las claves como:

            "{device_id}:{channel}"

        pero EventEngine trabaja únicamente con:

            "{channel}"
        """

        minutes = float(
            self.config.get(
                "report_window_minutes",
                30,
            )
        )

        device_id = int(
            self.config["device_id"]
        )

        all_series = self.store.all_series(
            minutes=minutes
        )

        prefix = f"{device_id}:"

        result: dict[
            str,
            list[ChannelReading],
        ] = {}

        for key, readings in all_series.items():

            if not key.startswith(prefix):
                continue

            channel = key[len(prefix):]

            result[channel] = readings

        return result

    # =============================================================
    # MÉTRICAS PARA CONTEXTO
    # =============================================================

    def _calculate_context_metrics(
        self,
        series: dict[str, list],
    ) -> dict[str, Any]:
        """
        Calcula información necesaria para construir contexto.

        Las operaciones matemáticas pertenecen a AnalysisEngine.
        """

        engine = getattr(
            self.agent,
            "engine",
            None,
        )

        if engine is None:

            return {}

        metrics: dict[str, Any] = {
            "stats": {},
            "baselines": {},
        }

        for channel, readings in series.items():

            try:

                metrics["stats"][channel] = (
                    engine.stats(
                        readings
                    )
                )

            except Exception:

                logger.exception(
                    "Error calculando stats para %s.",
                    channel,
                )

            try:

                metrics["baselines"][channel] = (
                    engine.baseline(
                        readings,
                        hours=self.window_minutes / 60.0,
                    )
                )

            except Exception:

                logger.debug(
                    "No fue posible calcular baseline "
                    "para %s.",
                    channel,
                    exc_info=True,
                )

        all_readings = [
            reading
            for readings in series.values()
            for reading in readings
        ]

        if all_readings:

            timestamps = [
                reading.event_time
                for reading in all_readings
            ]

            metrics["periodo_inicio"] = min(
                timestamps
            )

            metrics["periodo_fin"] = max(
                timestamps
            )

            metrics["duracion_minutos"] = (
                max(timestamps)
                - min(timestamps)
            ).total_seconds() / 60.0

        return metrics

    # =============================================================
    # EVENTOS RECIENTES
    # =============================================================

    def _is_significant(
        self,
        event,
    ) -> bool:

        if event.duracion_min < self.min_event_duration_min:
            return False

        if event.duracion_min > self.max_event_duration_min:
            return False

        total = sum(
            abs(float(value))
            for value in event.magnitud.values()
        )

        if total < self.min_event_magnitude:
            return False

        primary = 0.0

        for canal in self.trigger_channels:
            key = "delta_" + canal.lower().replace(" ", "_")
            value = event.magnitud.get(key)
            if value is not None:
                primary = max(primary, abs(float(value)))

        if primary < self.min_delta_primario:
            return False

        if (
            self.require_return_to_base
            and not event.volvio_a_base
        ):
            return False

        return True

    def _get_recent_events(
        self,
        series,
    ) -> list:

        try:

            events = self.event_engine.step(
                series
            )

        except Exception:

            logger.exception(
                "Error obteniendo eventos recientes."
            )

            return []

        # Mismos criterios de relevancia que el flujo en vivo:
        # el informe periódico no debe listar los ciclos normales
        # del compresor como si fueran eventos.
        return sorted(
            (e for e in events if self._is_significant(e)),
            key=lambda e: e.inicio,
        )

    # =============================================================
    # ENVÍO
        # =============================================================
    def _process_sitrad_alarms(self, now: datetime) -> None:
        fn = getattr(self.source, "new_alarms", None)
        if fn is None:
            return
        try:
            from core.clock import local_tz
            from analysis.diagnosis import DEFAULT_CFG

            fresh = fn(now)
            if not fresh:
                return
            tz = local_tz(self.cooling_cfg.tz_name)
            temp = self.store.get_series(int(self.config["device_id"]),
                                        DEFAULT_CFG["ch_temp"], minutes=30)
            tlast = next((float(x.value) for x in reversed(temp) if x.value is not None), None)

            for al in fresh:
                self.sitrad_alarms.append(al)
                when = al["when"].astimezone(tz).strftime("%H:%M") if al.get("when") else None
                inst = al.get("instrument", "")

                # ¿Es una alarma de temperatura?
                text_lower = (al.get("text") or "").lower()
                is_temp_alarm = any(k in text_lower for k in
                                    ("temperatura", "temp", "alta temp", "high temp", "calor"))

                if is_temp_alarm and self._cooling_enabled:
                    # Consolidar: alarma + diagnóstico en un solo mensaje
                    r = self.agent._tool_diagnose_now({"minutos": 60})
                    diag = r.get("respuesta_base") if r.get("ok") else ""
                    lines = [f"🔔 Alarma de SITRAD" + (f" — {inst}" if inst else "")
                            + (f" ({when})" if when else "")]
                    lines.append(al["text"])
                    if tlast is not None:
                        lines.append(f"Temperatura de la cámara ahora: "
                                    f"{str(round(tlast, 1)).replace('.', ',')} °C.")
                    if diag:
                        lines.append("")
                        lines.append("Diagnóstico: " + diag)
                    self._send("\n".join(lines), reason="alarma_sitrad_temp")
                else:
                    # Alarma no térmica: mensaje corto como antes
                    lines = [f"🔔 Alarma de SITRAD" + (f" — {inst}" if inst else "")
                            + (f" ({when})" if when else "")]
                    lines.append(al["text"])
                    if tlast is not None:
                        lines.append(f"Temperatura de la cámara ahora: "
                                    f"{str(round(tlast, 1)).replace('.', ',')} °C.")
                    self._send("\n".join(lines), reason="alarma_sitrad")
        except Exception:
            logger.exception("Fallo procesando las alarmas de SITRAD.")

    def _cfg_now(self):
        """Configuración vigente: el límite es el del producto MÁS ESTRICTO presente y las reglas, las del catálogo."""
        from dataclasses import replace
        base = self.cooling_cfg
        lim, _ = self.product_log.effective_limit(self.clock.now(), base.limit_c)
        return replace(base, limit_c=lim, product_rules={**base.product_rules, **self.product_log.rules_map()})

    def _process_cooling(self, now: datetime) -> None:
        """Alertas e informes de temperatura de la cámara (termocupla)."""
        if not self._cooling_enabled:
            return
        try:
            from analysis.diagnosis import DEFAULT_CFG

            dev = int(self.config["device_id"])
            span = float(getattr(self.store, "max_hours", 24.0)) * 60.0
            g = lambda ch: self.store.get_series(dev, ch, minutes=span)
            temp = g(DEFAULT_CFG["ch_temp"])
            if not temp:
                return
            self.cooling_monitor.set_cfg(self._cfg_now())
            msgs = self.cooling_monitor.step(
                temp, g(DEFAULT_CFG["ch_door"]), g(DEFAULT_CFG["ch_defr"]),
                g(DEFAULT_CFG["ch_refr"]), now=now,
                loads=self.product_log.active(now), loads_all=self.product_log.loads,
                setpoint=g(DEFAULT_CFG["ch_setpoint"]))
            try:
                cfg_now = self._cfg_now()
                for ep in self.cooling_monitor.drain_closed():
                    self.episode_history.record(ep, cfg_now, now)
            except Exception:
                logger.exception("No pude registrar episodios cerrados.")
            for kind, text in msgs:
                if kind == "informe" and self._llm_analysis:
                    from agent.narrative import add_analysis
                    text = add_analysis(text, getattr(self.agent, "llm", None))
                self._send(text, reason=f"temperatura_{kind}")
        except Exception:
            logger.exception("Fallo generando el informe de temperatura.")

    def _assess_incident(self, kind, severity, series=None):
        """diagnose + decisión de silenciar: la MISMA función que usa la validación."""
        from analysis.diagnosis import assess_incident

        if series is None:
            series = self._get_analysis_series()
        refs = None
        if self.profile is not None:
            bp = self.profile.get_binary(
                self.config.get("compressor_channel", "Estado Refrigeración"))
            if bp is not None:
                refs = (bp.on_p95_min, bp.off_p95_min)
        levels = (self.detector.consistency_levels()
                  if self.detector is not None else None)
        a = self.config.get("analysis", {}) or {}
        return assess_incident(
            series, incident_kind=kind, severity=severity,
            levels=levels, compressor_refs=refs,
            suppress_benign=bool(a.get("suppress_benign_incidents", False)),
            suppress_unexplained=bool(a.get("suppress_unexplained_incidents", True)),
            unexplained_max_severity=float(a.get("unexplained_max_severity", 0.8)),
        )

    def _check_diagnosis_updates(self, now) -> None:
        """Reevalúa los incidentes abiertos y avisa si la causa principal cambió."""
        if not self._diagnosis_updates or self.policy is None:
            return
        try:
            from core.report_policy import MessageKind, PolicyMessage

            opened = self.policy.open_incidents
            if not opened:
                return
            series = self._get_analysis_series()
            updates = self.diagnosis_tracker.check(
                now, opened.keys(),
                lambda k: self._assess_incident(
                    opened[k]["kind"], opened[k]["severity_max"], series),
                lambda k: opened[k],
            )
            for u in updates:
                inc = u.incident
                payload = {
                    "incident_kind": inc["kind"],
                    "channels": sorted(inc["channels"]),
                    "opened_at": inc["opened_at"].isoformat(),
                    "severity_max": inc["severity_max"],
                    "diagnosis": u.assessment.bundle,
                    "previous_principal": None if u.first_notice else u.previous,
                }
                self._silenced_incidents.discard(u.ikey) if hasattr(
                    self, "_silenced_incidents") else None
                self._send_policy_message(PolicyMessage(
                    kind=MessageKind.ALERTA,
                    key=f"incident_update:{u.ikey}:{now.isoformat()}",
                    text=f"[INCIDENTE] {inc['kind']} actualización",
                    priority=2,
                    payload=payload,
                ))
        except Exception:
            logger.exception("Fallo reevaluando el diagnóstico de incidentes.")

    def _send_policy_message(self, msg) -> None:
        text = msg.text

        if (
            self.agent is not None
            and hasattr(self.agent, "render_policy_message")
            and msg.payload
        ):
            try:
                context = self._build_period_context()

                # ── Hito B: capa determinista para incidentes ──
                if "incident_kind" in msg.payload:
                    is_resolution = (
                        getattr(msg.kind, "value", str(msg.kind))
                        == "RESOLUCION"
                    )
                    ikey = (
                        str(msg.key).split(":")[1]
                        if ":" in str(msg.key) else str(msg.key)
                    )
                    if not hasattr(self, "_silenced_incidents"):
                        self._silenced_incidents = set()

                    # Un incidente silenciado no envía tampoco su resolución.
                    if is_resolution:
                        self.diagnosis_tracker.drop(ikey)
                        if not self._tech_messages:
                            self._silenced_incidents.discard(ikey)
                            return
                    if is_resolution and ikey in self._silenced_incidents:
                        self._silenced_incidents.discard(ikey)
                        return

                    if not is_resolution:
                        try:
                            pre = msg.payload.get("diagnosis")
                            if pre is not None:
                                # Actualización de diagnóstico: ya viene evaluado.
                                bundle, silenced = pre, False
                            else:
                                res = self._assess_incident(
                                    msg.payload.get("incident_kind"),
                                    msg.payload.get("severity_max"),
                                )
                                bundle, silenced = res.bundle, res.silenced

                            context = (
                                context
                                + "\n\nDIAGNÓSTICO DETERMINISTA\n"
                                + "=" * 26
                                + "\n"
                                + bundle.to_context()
                            )
                            msg.payload["diagnosis"] = bundle

                            self.diagnosis_tracker.register(
                                ikey, bundle.principal, announced=not silenced,
                            )

                            try:
                                case_log = getattr(self, "case_log", None)
                                if case_log is not None:
                                    case_log.record(
                                        key=msg.key,
                                        payload=msg.payload,
                                        bundle=bundle,
                                        now=self.clock.now(),
                                        suppressed=silenced,
                                    )
                            except Exception:
                                logger.exception("No se pudo registrar el caso.")

                            if not self._tech_messages:
                                # El caso queda en el registro; al ingeniero le llega
                                # el informe de temperatura, no el dato técnico.
                                self._silenced_incidents.add(ikey)
                                return
                            if silenced:
                                self._silenced_incidents.add(ikey)
                                logger.info(
                                    "Incidente %s silenciado (diagnóstico: %s).",
                                    ikey, bundle.principal,
                                )
                                return
                            self._silenced_incidents.discard(ikey)

                        except Exception:
                            logger.exception(
                                "Fallo ejecutando diagnosis.diagnose."
                            )
                # ──────────────────────────────────────────────

                text = self.agent.render_policy_message(msg, context)

            except Exception:
                logger.exception(
                    "Fallo renderizando mensaje. Se usa texto plano."
                )
                text = msg.text

        self._send(text, reason=msg.kind.value)

    def _send(
        self,
        text: str,
        reason: str,
    ) -> bool:

        if self.telegram is None:

            logger.warning(
                "Telegram no está configurado."
            )

            return False

        try:

            ok = self.telegram.send(
                text
            )

        except Exception:

            logger.exception(
                "Error enviando mensaje de Telegram. "
                "reason=%s",
                reason,
            )

            return False

        if not ok:

            logger.error(
                "Telegram rechazó el mensaje. "
                "reason=%s",
                reason,
            )

        return bool(ok)

    # =============================================================
    # LOG
    # =============================================================

    def _log_report(
        self,
        report: str,
        timestamp: datetime,
        window_min: float,
        event=None,
    ) -> None:

        if self.filelog is None:

            return

        try:

            self.filelog.write(
                informe=report,
                timestamp=timestamp,
                device=self.device_name,
                window_min=window_min,
            )

        except Exception:

            logger.exception(
                "Error guardando informe en FileLog."
            )

    # =============================================================
    # INFORME MANUAL
    # =============================================================

    def report_now(self) -> str | None:
        """Genera y envía ya el informe de temperatura de las últimas 24 h."""
        if self.agent is None:
            return None
        try:
            r = self.agent._tool_temperature_report({"minutos": 1440.0})
        except Exception:
            logger.exception("Error generando el informe manual.")
            return None
        text = r.get("respuesta_base") if r.get("ok") else None
        if not text:
            logger.warning("No hay datos para el informe manual: %s", r.get("error"))
            return None
        self._send(text, reason="informe_manual")
        return text

    def _process_heartbeat(self, now: datetime) -> None:
        if self.heartbeat_every_minutes <= 0:
            return
        if self._last_heartbeat is None:
            self._last_heartbeat = self._started_at
            return
        elapsed = (now - self._last_heartbeat).total_seconds() / 60.0
        if elapsed < self.heartbeat_every_minutes:
            return
        self._last_heartbeat = now

        pending = 0
        if hasattr(self.telegram, "pending"):
            pending = self.telegram.pending

        open_anomalies = len(self._open_anomalies)
        n_events = 0
        if self.filelog is not None:
            try:
                n_events = len(self.filelog.read_events())
            except Exception:
                pass

        text = (
            f"❤️ Monitor vivo\n"
            f"Uptime: {(now - self._started_at).total_seconds() / 3600:.1f} h\n"
            f"Eventos registrados: {n_events}\n"
            f"Anomalías abiertas: {open_anomalies}\n"
            f"Mensajes pendientes: {pending}"
        )
        self._send(text, reason="heartbeat")


        # =============================================================
    # CICLO COMPLETO
    # =============================================================

    def run_one_cycle(self, now: datetime) -> None:
        """
        Ejecuta un ciclo completo del Reporter.

        Se usa desde run_forever y desde scripts de evaluación
        o integración. Orden:

            1. tick de Telegram      (drena cola de reintentos)
            2. ingest                (fuente → store + filelog)
            3. eventos               (transitorios)
            4. anomalías             (estados por canal)
            5. salud                 (canales sin datos)
            6. latido                (vida del proceso)
            7. informe periódico
            8. comandos interactivos
        """
        self._tick_telegram()
        self._ingest(now)
        self._process_events(now)
        self._process_anomalies(now)
        self._process_cooling(now)
        self._process_sitrad_alarms(now)
        self._process_health(now)
        self._process_heartbeat(now)
        self._process_periodic_report(now)
        self._process_commands()

    def _tick_telegram(self) -> None:
        """
        Da la oportunidad al notificador de drenar su cola de
        reintentos. Se llama en cada ciclo, haya o no mensajes
        nuevos. Si el notificador no implementa tick(), no pasa nada.
        """
        if self.telegram is None:
            return
        tick = getattr(self.telegram, "tick", None)
        if tick is None:
            return
        try:
            tick()
        except Exception:
            logger.exception(
                "Error drenando cola de reintentos de Telegram."
            )


    def _process_health(self, now: datetime) -> None:
        """
        Detecta canales sin lecturas recientes y, si la política
        lo soporta, emite SALUD.

        El umbral se controla con 'stale_channel_minutes' (default 5).
        """
        if self.policy is None or not self._tech_messages:
            return          # el aviso de "sin lecturas" lo da el informe de temperatura
        decide = getattr(self.policy, "decide_health", None)
        if decide is None:
            return

        series = self._get_analysis_series()
        if not series:
            return

        threshold = timedelta(
            minutes=float(self.config.get("stale_channel_minutes", 5))
        )

        stale = []
        for channel, readings in series.items():
            if not readings:
                stale.append(channel)
                continue
            last = readings[-1].event_time
            if now - last > threshold:
                stale.append(channel)

        if not stale:
            return
        logger.warning(
            "HEALTH_CHECK stale=%s threshold=%s",
            stale, threshold,
        )
        try:
            messages = decide(now, stale)
        except Exception:
            logger.exception("Error ejecutando policy.decide_health.")
            return

        for msg in messages:
            self._send_policy_message(msg)


    def _warmup_history(self) -> None:
        """
        Alimenta el perfil y la memoria con lo ya persistido.

        No lanza si algún paso falla: cada componente se salta
        individualmente.
        """
        if self.filelog is None:
            return

        if self.profile is not None:
            try:
                n = self.profile.warm_start(self.filelog)
                logger.info(
                    "BehaviorProfile warm start: %d lecturas.", n,
                )
                self.profile.fit()
            except Exception:
                logger.exception("Fallo en warm start del profile.")

            memory = self._memory()
            if memory is not None:
                try:
                    self.profile.update_recovery(memory)
                except Exception:
                    logger.exception(
                        "Fallo en update_recovery del profile."
                    )