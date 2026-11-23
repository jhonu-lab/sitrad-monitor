from __future__ import annotations
# core.reporter
import logging
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable

from analysis.anomaly_detector import AnomalyState
from core.contracts import ChannelReading
from core.worker import AgentWorker, InlineWorker

logger = logging.getLogger(__name__)

# Lugares en los que aplica el informe de temperatura cuando el registro no
# trae lugares (compatibilidad: solo instrumentos).
_COOLING_TYPES = ("tc900e", "generic")


class Reporter:
    """
    Orquestador principal del sistema de monitoreo.

    Reporter NO interpreta los datos:

        DataSource → SeriesStore → EventEngine / AnomalyDetector / CoolingMonitor
                   → ReportPolicy → CavaAgent (redacción) → Telegram / FileLog

    Multi-lugar: cada lugar (cava, horno...) es un device_id con su propio
    perfil de comportamiento, su monitor de frío y sus incidentes. Un lugar
    puede agrupar varios instrumentos de SITRAD (ver sources.sitrad_api).

    Hilos: el ciclo principal nunca espera al LLM. Las preguntas de los
    usuarios, la redacción de mensajes de la política, el párrafo de análisis
    del informe y el diagnóstico de alarmas se encolan en un hilo de trabajo
    (core.worker.AgentWorker) si `async_agent` es True; si no, se ejecutan
    en línea (tests y simulaciones deterministas).
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
        profile=None,
        detector=None,
        quality_guard=None,
        policy=None,
    ) -> None:
        for name, value in (("una fuente de datos", source), ("un SeriesStore", store),
                            ("un EventEngine", event_engine), ("un Agent", agent),
                            ("un ContextSerializer", context_serializer), ("un Clock", clock)):
            if value is None:
                raise ValueError(f"Reporter requiere {name}.")

        self.source = source
        self.store = store
        self.event_engine = event_engine
        self.agent = agent
        self.context_serializer = context_serializer
        self.telegram = telegram
        self.filelog = filelog
        self.clock = clock
        self.config = config
        self.registry = config.get("registry")  # InstrumentRegistry | None
        self._monitored_device_ids: list[int] = list(config.get("monitored_device_ids") or [])
        self.device_name = config.get("device_name", "Sistema Sitrad")

        # ---------------- Eventos (filtro del contexto del informe) ----------------
        self.trigger_channels = tuple(config.get("channels_que_disparan_eventos", ("Temp Producto",)))
        self.min_delta_primario = float(config.get("min_delta_canal_primario", 0.5))
        self.min_event_duration_min = float(config.get("min_event_duration_min", 0.5))
        self.min_event_magnitude = float(config.get("min_event_magnitude", 0.3))
        self.max_event_duration_min = float(config.get("max_event_duration_min", 5.0))
        self.require_return_to_base = bool(config.get("require_return_to_base", True))

        # ---------------- Frecuencias ----------------
        self.poll_seconds = float(config.get("polling_seconds", 5))
        # EventEngine reanaliza la ventana completa: se limita su frecuencia.
        self.analysis_every_seconds = float(config.get("analysis_every_seconds", 15))
        self.anomaly_every_seconds = float(config.get("anomaly_every_seconds", 30))
        self.window_minutes = float(config.get("report_window_minutes", 30))
        self.heartbeat_every_minutes = float(config.get("heartbeat_every_minutes", 0))
        self._last_analysis_at: dict[int, datetime] = {}
        self._last_anomaly_at: dict[int, datetime] = {}
        self._last_heartbeat: datetime | None = None
        self._started_at = self.clock.now()

        # ---------------- Canales que se analizan ----------------
        # "catalog" (defecto): EventEngine, perfil, anomalías y salud solo
        # sobre los canales catalogados (y los del channel_map). Un lugar sin
        # ningún canal catalogado (p. ej. un horno genérico) se analiza
        # entero, para no dejarlo ciego. "all": todos los canales.
        from core.channel_catalog import make_catalog_predicate
        self.analysis_channels = str(config.get("analysis_channels", "catalog")).lower()
        self._is_catalogued = make_catalog_predicate(config.get("catalog_extra_channels") or ())

        # ---------------- Fase 2/3: perfil, detector, calidad, política ----------------
        self.profile = profile
        self.detector = detector
        self.quality_guard = quality_guard
        self.policy = policy
        self._profiles: dict[int, Any] = {}           # un perfil aprendido por lugar

        # ---------------- Informe de temperatura ----------------
        from analysis.cooling_report import CoolingConfig, CoolingMonitor
        _c = dict(config.get("cooling", {}) or {})
        _c.setdefault("camera_name", config.get("device_name", "la cámara"))
        _c.setdefault("timezone", (config.get("operations", {}) or {}).get("timezone", "America/Bogota"))
        self._cooling_enabled = bool(_c.get("enabled", True))
        self.cooling_cfg = CoolingConfig.from_dict(_c)
        self._llm_analysis = bool(_c.get("llm_analysis", False))
        # Historia que recibe el monitor de frío en cada paso (no la retención completa del store).
        self.cooling_history_minutes = float(_c.get("history_hours", 24.0)) * 60.0
        self.cooling_monitor = CoolingMonitor(self.cooling_cfg)
        self._cooling_monitors: dict[int, Any] = {}
        self._cooling_last: dict[int, tuple[datetime | None, datetime]] = {}
        self._CoolingMonitor = CoolingMonitor

        from analysis.product_log import ProductLog
        self.product_log = ProductLog(
            _c.get("products_path", _c.get("product_log_path", "products.json")),
            tz_name=_c["timezone"])
        from analysis.feedback_log import CaseBook
        self.case_book = CaseBook(_c.get("cases_path", "logs/cooling_cases.jsonl"),
                                  _c.get("feedback_path", "logs/feedback.jsonl"), tz_name=_c["timezone"])
        self.cooling_monitor.on_case = self.case_book.record_case
        from analysis.episode_history import EpisodeHistory
        self.episode_history = EpisodeHistory(
            (config.get("storage", {}) or {}).get("episodes_log", "logs/episodes.jsonl"), tz_name=_c["timezone"])
        self.sitrad_alarms: deque = deque(maxlen=50)

        self.agent.case_book = self.case_book
        self.agent.episode_history = self.episode_history
        self.agent.sitrad_alarms = self.sitrad_alarms
        self.agent.sitrad_live = hasattr(self.source, "new_alarms")
        self.agent.product_log = self.product_log
        # "agent_scope": "todo" en el bloque cooling devuelve Multipower.
        self.agent.scope_controller_only = (_c.get("agent_scope", "controlador") != "todo")
        self.agent.cooling_cfg = self.cooling_cfg

        # Con el informe activo, los mensajes técnicos de incidente se apagan
        # (siguen en el registro de casos) salvo que se pidan explícitamente.
        _a = config.get("analysis", {}) or {}
        self._tech_messages = bool(_a.get("technical_incident_messages", not self._cooling_enabled))

        # Reevaluación del diagnóstico de incidentes abiertos.
        from core.incident_tracker import DiagnosisTracker
        self._diagnosis_updates = bool(_a.get("diagnosis_updates", True))
        self.diagnosis_tracker = DiagnosisTracker(
            every_s=float(_a.get("diagnosis_update_every_seconds", 240)),
            confirm=int(_a.get("diagnosis_update_confirm", 2)),
            min_confidence=float(_a.get("diagnosis_update_min_confidence", 0.5)),
        )
        self._silenced_incidents: set[str] = set()
        self.case_log = None                         # lo asigna run_reporter
        self.retention = None                        # core.retention.RetentionManager (opcional)

        # ---------------- Hilo de trabajo del agente ----------------
        worker = config.get("worker")
        if worker is None:
            worker = AgentWorker() if config.get("async_agent", False) else InlineWorker()
        self._worker = worker

        # Caché por ciclo: eventos del último análisis y contexto del informe.
        self._recent_events: dict[int, list] = {}
        self._context_cache: dict[int, tuple[datetime, str]] = {}

        if config.get("warmup_on_start", False):
            self._warmup_history()

    # =============================================================
    # CICLO PRINCIPAL
    # =============================================================

    def run_forever(self) -> None:
        logger.info("Reporter iniciado. polling=%ss", self.poll_seconds)
        self._worker.start()
        start_polling = getattr(self.telegram, "start_polling", None)
        if start_polling is not None:
            try:
                start_polling()
            except Exception:
                logger.exception("No fue posible iniciar el polling de Telegram.")
        try:
            while True:
                cycle_started = time.monotonic()
                try:
                    self.run_one_cycle(self.clock.now())
                except Exception:
                    logger.exception("Error no controlado en el ciclo principal.")
                sleep_seconds = self.poll_seconds - (time.monotonic() - cycle_started)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
        finally:
            stop_polling = getattr(self.telegram, "stop_polling", None)
            if stop_polling is not None:
                try:
                    stop_polling()
                except Exception:
                    logger.exception("Error deteniendo polling de Telegram.")
            self._worker.stop()

    def run_one_cycle(self, now: datetime) -> None:
        """
        Un ciclo completo (lo usan run_forever, los tests y las simulaciones):

            1. reintentos de Telegram   6. salud (canales sin datos)
            2. ingesta                  7. latido
            3. eventos transitorios     8. resumen de turno
            4. anomalías                9. preguntas (al hilo del agente)
            5. frío + alarmas SITRAD   10. limpieza programada de datos
        """
        self._context_cache.clear()
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
        self._process_retention(now)

    def drain_worker(self, timeout: float = 30.0) -> bool:
        """Espera a que el hilo del agente termine lo pendiente (tests / apagado)."""
        return self._worker.drain(timeout)

    # =============================================================
    # LUGARES / DISPOSITIVOS
    # =============================================================

    def _device_ids(self) -> list[int]:
        reg = getattr(self, "registry", None)
        if reg is not None and getattr(reg, "locations", None):
            return reg.location_ids()
        if self._monitored_device_ids:
            return list(self._monitored_device_ids)
        return [int(self.config["device_id"])]

    def _device_ids_len(self) -> int:
        return len(self._device_ids())

    def _location(self, device_id: int):
        reg = self.registry
        if reg is None:
            return None
        loc = reg.location(device_id) if hasattr(reg, "location") else None
        return loc if loc is not None else reg.by_device_id(device_id)

    def _label(self, device_id: int | None) -> str:
        """Nombre del lugar para prefijar mensajes; vacío si solo hay uno."""
        if device_id is None or self._device_ids_len() <= 1:
            return ""
        loc = self._location(device_id)
        return getattr(loc, "name", "") or str(device_id)

    def _device_display_name(self, device_id: int | None) -> str:
        return self._label(device_id) or self.device_name

    def _primary_device(self) -> int:
        dev = int(self.config.get("device_id", 0) or 0)
        ids = self._device_ids()
        return dev if dev in ids or not ids else ids[0]

    # =============================================================
    # INGESTIÓN
    # =============================================================

    def _ingest(self, now: datetime) -> None:
        """Snapshot de la fuente → QualityGuard → SeriesStore + FileLog (en lote)."""
        try:
            snapshot = self.source.poll(now)
        except Exception:
            logger.exception("Error durante el polling de la fuente.")
            return
        if snapshot is None or not snapshot.readings:
            return

        stored: list[ChannelReading] = []
        for reading in snapshot.readings:
            try:
                if self.quality_guard is not None:
                    reading = self.quality_guard.apply(reading)
                self.store.ingest(reading)
                stored.append(reading)
            except Exception:
                logger.exception("Error incorporando lectura al SeriesStore. channel=%s", reading.channel)
        if self.filelog is not None and stored:
            try:
                self.filelog.write_observations(stored)
            except Exception:
                logger.exception("Error escribiendo observaciones en FileLog.")

    # =============================================================
    # DATOS DEL STORE
    # =============================================================

    def _channel_filter(self, device_id: int) -> Callable[[str], bool] | None:
        """Canales que se analizan en este lugar (None = todos)."""
        if self.analysis_channels == "all":
            return None
        chans = self.store.channels(device_id=device_id)
        if any(self._is_catalogued(c) for c in chans):
            return self._is_catalogued
        return None                              # lugar sin canales conocidos: se analiza entero

    def _get_analysis_series(self, device_id: int | None = None) -> dict[str, list[ChannelReading]]:
        dev = int(device_id if device_id is not None else self._primary_device())
        minutes = float(self.config.get("report_window_minutes", 30))
        getter = getattr(self.store, "device_series", None)
        if getter is not None:
            return getter(dev, minutes=minutes, channels=self._channel_filter(dev))
        prefix = f"{dev}:"                        # store antiguo
        return {k[len(prefix):]: v for k, v in self.store.all_series(minutes=minutes).items()
                if k.startswith(prefix)}

    # =============================================================
    # EVENTOS TRANSITORIOS
    # =============================================================

    def _process_events(self, now: datetime) -> None:
        for device_id in self._device_ids():
            self._process_events_for(device_id, now)

    def _process_events_for(self, device_id: int, now: datetime) -> None:
        last = self._last_analysis_at.get(device_id)
        if (self.analysis_every_seconds > 0 and last is not None
                and (now - last).total_seconds() < self.analysis_every_seconds):
            return
        series = self._get_analysis_series(device_id)
        if not series:
            return
        try:
            events = self.event_engine.step(series)
        except Exception:
            logger.exception("Error ejecutando EventEngine para device %d.", device_id)
            return
        self._last_analysis_at[device_id] = now
        events = sorted(events, key=lambda event: event.inicio)
        self._recent_events[device_id] = events
        if self.policy is None:
            return
        label = self._label(device_id)
        accepted, messages = self.policy.decide_events(events, now, device_id=device_id, label=label)
        for event in accepted:
            self._record_event(event, device_id)
        for msg in messages:
            self._send_policy_message(msg, device_id)

    def _record_event(self, event, device_id: int | None = None) -> None:
        """Persiste el evento (events.jsonl) y lo añade a la memoria."""
        memory = self._memory()
        device = self._device_display_name(device_id)
        try:
            if memory is not None:
                memory.record_event(event, device=device)
            elif self.filelog is not None:
                self.filelog.write_event(event, device=device)
        except Exception:
            logger.exception("Error registrando el evento.")

    # =============================================================
    # ANOMALÍAS PERSISTENTES
    # =============================================================

    def _profile_for(self, device_id: int):
        """Perfil aprendido del lugar. El primero usa el perfil recibido; los demás, uno nuevo igual."""
        if self.profile is None:
            return None
        prof = self._profiles.get(device_id)
        if prof is None:
            if not self._profiles:
                prof = self.profile
            else:
                prof = type(self.profile)(config=self.profile.config, analysis=self.profile.analysis)
            self._profiles[device_id] = prof
        return prof

    def _process_anomalies(self, now: datetime) -> None:
        for device_id in self._device_ids():
            self._process_anomalies_for(device_id, now)
        self._check_diagnosis_updates(now)

    def _process_anomalies_for(self, device_id: int, now: datetime) -> None:
        if self.detector is None or self.profile is None:
            return
        last = self._last_anomaly_at.get(device_id)
        if (self.anomaly_every_seconds > 0 and last is not None
                and (now - last).total_seconds() < self.anomaly_every_seconds):
            return
        series = self._get_analysis_series(device_id)
        if not series:
            return

        profile = self._profile_for(device_id)
        try:
            profile.ingest_many(r for readings in series.values() for r in readings)
            fit_if_due = getattr(profile, "fit_if_due", None)
            if fit_if_due is not None:
                fit_if_due(now)
            else:
                profile.fit()
        except Exception:
            logger.exception("Error actualizando BehaviorProfile.")

        try:
            states = self.detector.detect(series, profile)
        except Exception:
            logger.exception("Error ejecutando AnomalyDetector.")
            return

        if self.quality_guard is not None:
            try:
                all_readings = [r for readings in series.values() for r in readings]
                for issue in self.quality_guard.issues(all_readings):
                    quals = {str(getattr(q, "value", q)).lower() for q in issue.qualities}
                    kind = "missing" if quals & {"missing", "offline", "err"} else "flat_signal"
                    states.append(AnomalyState(
                        channel=issue.channel,
                        kind=kind,
                        severity=min(1.0, issue.consecutive_bad / 10.0),
                        since=issue.since,
                        evidence={"consecutive_bad": issue.consecutive_bad,
                                  "qualities": list(issue.qualities)},
                    ))
            except Exception:
                logger.exception("Error consultando QualityGuard.")

        self._last_anomaly_at[device_id] = now
        if self.policy is None:
            return
        active_by_channel: dict[str, AnomalyState] = {}
        for state in states:
            prev = active_by_channel.get(state.channel)
            if prev is None or state.severity > prev.severity:
                active_by_channel[state.channel] = state
        for msg in self.policy.decide_anomalies(active_by_channel, now,
                                                device_id=device_id, label=self._label(device_id)):
            self._send_policy_message(msg, device_id)

    # =============================================================
    # INFORME DE TEMPERATURA
    # =============================================================

    def _cfg_now(self, now: datetime | None = None):
        """Configuración vigente: el límite es el del producto MÁS ESTRICTO presente."""
        base = self.cooling_cfg
        lim, _ = self.product_log.effective_limit(now or self.clock.now(), base.limit_c)
        return replace(base, limit_c=lim, product_rules={**base.product_rules, **self.product_log.rules_map()})

    def _cooling_applies(self, device_id: int) -> bool:
        loc = self._location(device_id)
        if loc is None:
            return True
        if hasattr(loc, "cooling"):
            return bool(loc.cooling)
        return getattr(loc, "type", "generic") in _COOLING_TYPES

    def _cooling_monitor_for(self, device_id: int):
        mon = self._cooling_monitors.get(device_id)
        if mon is None:
            if not self._cooling_monitors:
                mon = self.cooling_monitor
            else:
                mon = self._CoolingMonitor(self.cooling_cfg)
                mon.on_case = self.case_book.record_case
            self._cooling_monitors[device_id] = mon
        return mon

    def _process_cooling(self, now: datetime) -> None:
        if not self._cooling_enabled:
            return
        for device_id in self._device_ids():
            if not self._cooling_applies(device_id):
                continue
            try:
                self._process_cooling_for(device_id, now)
            except Exception:
                logger.exception("Fallo cooling device %d.", device_id)

    def _process_cooling_for(self, device_id: int, now: datetime) -> None:
        from analysis.diagnosis import DEFAULT_CFG

        dev = int(device_id)
        latest = self.store.latest(dev, DEFAULT_CFG["ch_temp"])
        if latest is None:
            return
        # Sin lectura nueva de temperatura no hay nada que reevaluar, salvo
        # el aviso de "sin datos": se revisa al menos una vez por minuto.
        prev = self._cooling_last.get(dev)
        if prev is not None and prev[0] == latest.event_time and (now - prev[1]).total_seconds() < 60:
            return
        self._cooling_last[dev] = (latest.event_time, now)

        span = min(self.cooling_history_minutes, float(getattr(self.store, "max_hours", 24.0)) * 60.0)

        def g(ch):
            return self.store.get_series(dev, ch, minutes=span)

        monitor = self._cooling_monitor_for(dev)
        cfg_now = self._cfg_now(now)
        monitor.set_cfg(cfg_now)
        msgs = monitor.step(
            g(DEFAULT_CFG["ch_temp"]), g(DEFAULT_CFG["ch_door"]), g(DEFAULT_CFG["ch_defr"]),
            g(DEFAULT_CFG["ch_refr"]), now=now,
            loads=self.product_log.active(now), loads_all=self.product_log.loads,
            setpoint=g(DEFAULT_CFG["ch_setpoint"]))
        try:
            for ep in monitor.drain_closed():
                self.episode_history.record(ep, cfg_now, now)
        except Exception:
            logger.exception("No pude registrar episodios cerrados.")

        label = self._label(dev)
        for kind, text in msgs:
            if label:
                text = f"[{label}] {text}"
            if kind == "informe" and self._llm_analysis:
                # El párrafo del LLM es lento: se redacta en el hilo del agente.
                def job(text=text, kind=kind):
                    from agent.narrative import add_analysis
                    self._send(add_analysis(text, getattr(self.agent, "llm", None)), reason=f"temperatura_{kind}")
                self._worker.submit(job, "informe_temperatura")
            else:
                self._send(text, reason=f"temperatura_{kind}")

    # =============================================================
    # ALARMAS DE SITRAD
    # =============================================================

    def _alarm_device(self, al: dict) -> int | None:
        reg = self.registry
        if reg is None:
            return None
        inst_id = al.get("instrument_id")
        if inst_id is not None:
            if hasattr(reg, "location_of_instrument"):
                loc = reg.location_of_instrument(inst_id)
                if loc is not None:
                    return loc.device_id
            info = next((i for i in reg.instruments.values() if i.instrument_id == inst_id), None)
            if info is not None:
                return info.device_id
        cooling = [d for d in self._device_ids() if self._cooling_applies(d)]
        return cooling[0] if cooling else None

    def _process_sitrad_alarms(self, now: datetime) -> None:
        fn = getattr(self.source, "new_alarms", None)
        if fn is None:
            return
        try:
            fresh = fn(now)
        except Exception:
            logger.exception("Fallo consultando las alarmas de SITRAD.")
            return
        if not fresh:
            return
        from analysis.diagnosis import DEFAULT_CFG
        from core.clock import local_tz
        tz = local_tz(self.cooling_cfg.tz_name)

        for al in fresh:
            try:
                self.sitrad_alarms.append(al)
                target = self._alarm_device(al)
                when = al["when"].astimezone(tz).strftime("%H:%M") if al.get("when") else None
                where = [x for x in (self._label(target), al.get("instrument", "")) if x]
                header = ("🔔 Alarma de SITRAD"
                          + (f" — {' / '.join(dict.fromkeys(where))}" if where else "")
                          + (f" ({when})" if when else ""))
                lines = [header, str(al.get("text") or "")]
                if target is not None:
                    temp = self.store.latest_value(target, DEFAULT_CFG["ch_temp"])
                    if temp is not None:
                        lines.append(f"Temperatura actual: {str(round(temp, 1)).replace('.', ',')} °C.")
                text_lower = str(al.get("text") or "").lower()
                is_temp_alarm = any(k in text_lower for k in ("temperatura", "temp", "high temp", "calor"))

                if (is_temp_alarm and self._cooling_enabled and target is not None
                        and hasattr(self.agent, "_tool_diagnose_now")):
                    def job(lines=lines, target=target):
                        try:
                            r = self.agent._tool_diagnose_now({"minutos": 60, "device_id": target})
                            diag = r.get("respuesta_base") if r.get("ok") else ""
                        except Exception:
                            logger.exception("Diagnóstico de la alarma falló.")
                            diag = ""
                        out = list(lines)
                        if diag:
                            out.extend(["", "Diagnóstico: " + diag])
                        self._send("\n".join(out), reason="alarma_sitrad_temp")
                    self._worker.submit(job, "alarma_sitrad")
                else:
                    self._send("\n".join(lines), reason="alarma_sitrad")
            except Exception:
                logger.exception("Fallo procesando una alarma de SITRAD: %r", al)

    # =============================================================
    # SALUD, LATIDO, RESUMEN
    # =============================================================

    def _process_health(self, now: datetime) -> None:
        if self.policy is None or not self._tech_messages:
            return
        decide = getattr(self.policy, "decide_health", None)
        if decide is None:
            return
        threshold = timedelta(minutes=float(self.config.get("stale_channel_minutes", 5)))
        for device_id in self._device_ids():
            latest = self.store.latest_times(device_id) if hasattr(self.store, "latest_times") else {}
            if not latest:
                continue
            keep = self._channel_filter(device_id)
            stale = sorted(ch for ch, t in latest.items()
                           if (keep is None or keep(ch)) and now - t > threshold)
            if not stale:
                continue
            logger.warning("HEALTH_CHECK device=%d stale=%s threshold=%s", device_id, stale, threshold)
            try:
                for msg in decide(now, stale, device_id=device_id, label=self._label(device_id)):
                    self._send_policy_message(msg, device_id)
            except Exception:
                logger.exception("Error ejecutando policy.decide_health.")

    def _process_heartbeat(self, now: datetime) -> None:
        if self.heartbeat_every_minutes <= 0:
            return
        if self._last_heartbeat is None:
            self._last_heartbeat = self._started_at
            return
        if (now - self._last_heartbeat).total_seconds() / 60.0 < self.heartbeat_every_minutes:
            return
        self._last_heartbeat = now
        pending = getattr(self.telegram, "pending", 0) if self.telegram is not None else 0
        open_incidents = len(self.policy.open_incidents) if self.policy is not None else 0
        n_events = 0
        if self.filelog is not None:
            try:
                n_events = len(self.filelog.read_events())
            except Exception:
                pass
        text = (
            f"❤️ Monitor vivo\n"
            f"Uptime: {(now - self._started_at).total_seconds() / 3600:.1f} h\n"
            f"Lugares: {self._device_ids_len()}\n"
            f"Eventos registrados: {n_events}\n"
            f"Incidentes abiertos: {open_incidents}\n"
            f"Mensajes pendientes: {pending}\n"
            f"Trabajos del agente en cola: {self._worker.pending}"
        )
        self._send(text, reason="heartbeat")

    def _process_periodic_report(self, now: datetime) -> None:
        """Resumen de turno: lo decide la política."""
        if self.policy is None:
            return
        for msg in self.policy.decide_summary(now):
            self._send_policy_message(msg)

    def _process_retention(self, now: datetime) -> None:
        if self.retention is None:
            return
        try:
            self.retention.maybe_run(now, submit=self._worker.submit)
        except Exception:
            logger.exception("Fallo en la limpieza programada de datos.")

    # =============================================================
    # CONSULTAS INTERACTIVAS (hilo del agente)
    # =============================================================

    def _process_commands(self) -> None:
        """Saca las preguntas que el notificador ya recibió y las encola al hilo del agente."""
        if self.telegram is None:
            return
        has_command = getattr(self.telegram, "has_command", None)
        pop_command = getattr(self.telegram, "pop_command", None)
        if has_command is None or pop_command is None:
            return
        while True:
            try:
                if not has_command():
                    break
                question = pop_command()
            except Exception:
                logger.exception("Error obteniendo pregunta de Telegram.")
                break
            if not question:
                continue
            logger.info("Pregunta encolada: %s", question)
            self._worker.submit(lambda q=question: self._answer(q), "pregunta")

    def _answer(self, question) -> None:
        """Responde una pregunta (se ejecuta en el hilo del agente) y contesta en SU chat."""
        user_key = getattr(question, "user_key", "") or ""
        user_name = getattr(question, "user_name", "") or ""
        chat_id = getattr(question, "chat_id", "") or None
        command = getattr(question, "command", None)
        prefix = f"{user_name}: " if user_name else ""

        if command == "forget":
            n = self.agent.forget_user(user_key)
            self._send(f"{prefix}listo, olvidé {n} conversación(es) tuya(s).", reason="consulta", chat_id=chat_id)
            return
        if command == "history":
            convs = getattr(self.agent, "conversations", None)
            turns = convs.recent(user_key, 5) if convs else []
            body = ("\n".join(f"• {t.question}" for t in turns)
                    if turns else "Aún no tengo preguntas tuyas guardadas.")
            self._send(f"{prefix}tus últimas preguntas:\n{body}", reason="consulta", chat_id=chat_id)
            return

        try:
            if user_key:
                answer = self.agent.answer_question(question, user_key=user_key, user_name=user_name)
            else:
                answer = self.agent.answer_question(question)
        except Exception:
            logger.exception("Error respondiendo pregunta mediante CavaAgent.")
            self._send("No pude procesar la pregunta en este momento.", reason="consulta_error", chat_id=chat_id)
            return

        if not answer:
            logger.warning("CavaAgent no produjo respuesta.")
            self._send("No pude procesar la pregunta en este momento. Intenta reformularla o usa "
                       "nombres exactos de canal (por ejemplo: 'Temp Cámara').",
                       reason="consulta_sin_respuesta", chat_id=chat_id)
            return

        if isinstance(answer, dict):
            if self._handle_photo_answer(answer, chat_id=chat_id):
                return
            answer = answer.get("caption") or str(answer)
        self._send(f"{prefix}{answer}", reason="consulta", chat_id=chat_id)

    def _handle_photo_answer(self, answer: dict, chat_id: str | None = None) -> bool:
        """Envía answer["__photo__"] al chat que preguntó. False si no hay foto o no se pudo (se responde con texto)."""
        path = answer.get("__photo__")
        caption = answer.get("caption") or ""
        send_photo = getattr(self.telegram, "send_photo", None)
        if not path or send_photo is None:
            return False
        try:
            if chat_id:
                return bool(send_photo(path, caption, chat_id=chat_id))
            return bool(send_photo(path, caption))
        except Exception:
            logger.exception("Fallo enviando foto; se envía texto.")
            return False

    # =============================================================
    # MEMORIA Y CONTEXTO DEL INFORME
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
                logger.exception("Error consultando memoria histórica.")
                result.append({})
        return result

    def _is_significant(self, event) -> bool:
        if not (self.min_event_duration_min <= event.duracion_min <= self.max_event_duration_min):
            return False
        if sum(abs(float(v)) for v in event.magnitud.values()) < self.min_event_magnitude:
            return False
        primary = 0.0
        for canal in self.trigger_channels:
            value = event.magnitud.get("delta_" + canal.lower().replace(" ", "_"))
            if value is not None:
                primary = max(primary, abs(float(value)))
        if primary < self.min_delta_primario:
            return False
        return not (self.require_return_to_base and not event.volvio_a_base)

    def _calculate_context_metrics(self, series: dict[str, list]) -> dict[str, Any]:
        """Estadísticas por canal (las matemáticas pertenecen a AnalysisEngine)."""
        engine = getattr(self.agent, "engine", None)
        if engine is None:
            return {}
        metrics: dict[str, Any] = {"stats": {}, "baselines": {}}
        first = last = None
        for channel, readings in series.items():
            try:
                metrics["stats"][channel] = engine.stats(readings)
            except Exception:
                logger.exception("Error calculando stats para %s.", channel)
            try:
                metrics["baselines"][channel] = engine.baseline(readings, hours=self.window_minutes / 60.0)
            except Exception:
                logger.debug("No fue posible calcular baseline para %s.", channel, exc_info=True)
            if readings:
                a, b = readings[0].event_time, readings[-1].event_time
                first = a if first is None or a < first else first
                last = b if last is None or b > last else last
        if first is not None:
            metrics["periodo_inicio"] = first
            metrics["periodo_fin"] = last
            metrics["duracion_minutos"] = (last - first).total_seconds() / 60.0
        return metrics

    def _build_period_context(self, device_id: int | None = None) -> str:
        """Contexto del lugar para que el LLM redacte. Se calcula una vez por ciclo y lugar,
        y reutiliza los eventos del último análisis en vez de volver a correr EventEngine."""
        dev = int(device_id if device_id is not None else self._primary_device())
        now = self.clock.now()
        cached = self._context_cache.get(dev)
        if cached is not None and cached[0] == now:
            return cached[1]
        series = self._get_analysis_series(dev)
        if not series:
            return ""
        events = self._recent_events.get(dev)
        if events is None:
            try:
                events = self.event_engine.step(series)
            except Exception:
                logger.exception("Error obteniendo eventos recientes.")
                events = []
            self._recent_events[dev] = events
        events = sorted((e for e in events if self._is_significant(e)), key=lambda e: e.inicio)
        ctx = self.context_serializer.build_context(
            metrics=self._calculate_context_metrics(series),
            events=events,
            all_series=series,
            device_name=self._device_display_name(dev),
            history=self._history_for(events),
        )
        self._context_cache[dev] = (now, ctx)
        return ctx

    # =============================================================
    # INCIDENTES
    # =============================================================

    def _assess_incident(self, kind, severity, series=None, device_id: int | None = None):
        """diagnose + decisión de silenciar: la MISMA función que usa la validación."""
        from analysis.diagnosis import assess_incident

        dev = int(device_id if device_id is not None else self._primary_device())
        if series is None:
            series = self._get_analysis_series(dev)
        refs = None
        profile = self._profile_for(dev)
        if profile is not None:
            bp = profile.get_binary(self.config.get("compressor_channel", "Estado Refrigeración"))
            if bp is not None:
                refs = (bp.on_p95_min, bp.off_p95_min)
        levels = self.detector.consistency_levels() if self.detector is not None else None
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
            series_by_dev: dict[int, dict] = {}

            def series_of(k):
                dev = opened[k].get("device_id")
                dev = self._primary_device() if dev is None else int(dev)
                if dev not in series_by_dev:
                    series_by_dev[dev] = self._get_analysis_series(dev)
                return dev, series_by_dev[dev]

            def assess(k):
                dev, series = series_of(k)
                return self._assess_incident(opened[k]["kind"], opened[k]["severity_max"], series, dev)

            updates = self.diagnosis_tracker.check(now, opened.keys(), assess, lambda k: opened[k])
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
                if inc.get("device_id") is not None:
                    payload["device_id"] = inc["device_id"]
                if inc.get("label"):
                    payload["location"] = inc["label"]
                self._silenced_incidents.discard(u.ikey)
                tag = f"[{inc['label']}] " if inc.get("label") else ""
                self._send_policy_message(PolicyMessage(
                    kind=MessageKind.ALERTA,
                    key=f"incident_update:{u.ikey}:{now.isoformat()}",
                    text=f"{tag}[INCIDENTE] {inc['kind']} actualización",
                    priority=2,
                    payload=payload,
                ), inc.get("device_id"))
        except Exception:
            logger.exception("Fallo reevaluando el diagnóstico de incidentes.")

    def _send_policy_message(self, msg, device_id: int | None = None) -> None:
        """Decide (ciclo principal) si el mensaje sale y con qué contexto; el LLM redacta en el hilo del agente."""
        if not (msg.payload and hasattr(self.agent, "render_policy_message")):
            self._send(msg.text, reason=msg.kind.value)
            return

        dev = msg.payload.get("device_id", device_id)
        try:
            extra = ""
            if "incident_kind" in msg.payload:
                is_resolution = getattr(msg.kind, "value", str(msg.kind)) == "RESOLUCION"
                key = str(msg.key)
                ikey = key.split(":")[1] if ":" in key else key

                # Un incidente silenciado no envía tampoco su resolución.
                if is_resolution:
                    self.diagnosis_tracker.drop(ikey)
                    if not self._tech_messages or ikey in self._silenced_incidents:
                        self._silenced_incidents.discard(ikey)
                        return
                else:
                    pre = msg.payload.get("diagnosis")
                    if pre is not None:                     # actualización: ya viene evaluado
                        bundle, silenced = pre, False
                    else:
                        res = self._assess_incident(msg.payload.get("incident_kind"),
                                                    msg.payload.get("severity_max"), device_id=dev)
                        bundle, silenced = res.bundle, res.silenced
                    msg.payload["diagnosis"] = bundle
                    self.diagnosis_tracker.register(ikey, bundle.principal, announced=not silenced)
                    if self.case_log is not None:
                        try:
                            self.case_log.record(key=msg.key, payload=msg.payload, bundle=bundle,
                                                 now=self.clock.now(), suppressed=silenced)
                        except Exception:
                            logger.exception("No se pudo registrar el caso.")
                    if not self._tech_messages:
                        # El caso queda en el registro; al ingeniero le llega el informe de temperatura.
                        self._silenced_incidents.add(ikey)
                        return
                    if silenced:
                        self._silenced_incidents.add(ikey)
                        logger.info("Incidente %s silenciado (diagnóstico: %s).", ikey, bundle.principal)
                        return
                    self._silenced_incidents.discard(ikey)
                    extra = "\n\nDIAGNÓSTICO DETERMINISTA\n" + "=" * 26 + "\n" + bundle.to_context()

            context = self._build_period_context(dev) + extra
        except Exception:
            logger.exception("Fallo preparando el mensaje. Se usa texto plano.")
            self._send(msg.text, reason=msg.kind.value)
            return

        def job(msg=msg, context=context):
            try:
                text = self.agent.render_policy_message(msg, context) or msg.text
            except Exception:
                logger.exception("Fallo renderizando mensaje. Se usa texto plano.")
                text = msg.text
            self._send(text, reason=msg.kind.value)

        self._worker.submit(job, f"mensaje {msg.kind.value}")

    # =============================================================
    # ENVÍO
    # =============================================================

    def _send(self, text: str, reason: str, chat_id: str | None = None) -> bool:
        """Envía al chat indicado; sin chat_id, a los chats de alertas configurados."""
        if self.telegram is None:
            logger.warning("Telegram no está configurado.")
            return False
        try:
            ok = self.telegram.send(text, chat_id=chat_id) if chat_id else self.telegram.send(text)
        except Exception:
            logger.exception("Error enviando mensaje de Telegram. reason=%s", reason)
            return False
        if not ok:
            logger.error("Telegram rechazó el mensaje. reason=%s", reason)
        return bool(ok)

    def _tick_telegram(self) -> None:
        """Deja al notificador drenar su cola de reintentos (si la tiene)."""
        tick = getattr(self.telegram, "tick", None) if self.telegram is not None else None
        if tick is None:
            return
        try:
            tick()
        except Exception:
            logger.exception("Error drenando cola de reintentos de Telegram.")

    # =============================================================
    # ARRANQUE
    # =============================================================

    def _warmup_history(self) -> None:
        """Alimenta el perfil de cada lugar y la memoria con lo ya persistido (nunca lanza)."""
        if self.filelog is None or self.profile is None:
            return
        since = self.clock.now() - timedelta(hours=float(getattr(self.store, "max_hours", 24.0)))
        memory = self._memory()
        for dev in self._device_ids():
            profile = self._profile_for(dev)
            try:
                n = profile.warm_start(self.filelog, device_id=dev, since=since)
                profile.fit()
                logger.info("BehaviorProfile warm start (device %d): %d lecturas.", dev, n)
            except TypeError:
                n = profile.warm_start(self.filelog)          # perfil antiguo
                profile.fit()
            except Exception:
                logger.exception("Fallo en warm start del profile.")
                continue
            if memory is not None:
                try:
                    profile.update_recovery(memory)
                except Exception:
                    logger.exception("Fallo en update_recovery del profile.")
