# CHANGELOG

## Revisión noviembre 2026 — arreglos, multi-lugar, hilo del agente y limpieza de datos

### Errores corregidos
- `run_reporter.py`: argumento `passthrough` repetido (SyntaxError: el bot no arrancaba).
- `agent/cava_agent.py`: `self._dev` era un atributo pero 27 métodos lo llamaban como `self._dev()`
  (todas las herramientas fallaban con `'NoneType' object is not callable`; era la causa del «demo roto»).
  Ahora `_dev_override` guarda el lugar de la pregunta y `_dev()` resuelve el lugar por defecto.
- `_has_multipower` estaba definido dos veces; queda uno solo (registro → flag → store).
- `_tool_diagnose_now` ignoraba el `device_id` que le pasa el Reporter.
- `notifiers/telegram_report.py`: `_drain_backlog` usaba `self._session` (inexistente) y mataba el hilo
  de polling al arrancar: el bot no recibía preguntas. Quitado el `from matplotlib import text` perdido.
- `agent/llm_client.py`: si Ollama no respondía al arrancar (o fallaba una vez), el LLM quedaba
  desactivado para siempre. `_ensure_startup` vuelve a comprobar mientras el circuit breaker esté cerrado.
  La bajada de prioridad por llamada (`os.nice` acumulativo sobre el propio bot) se sustituye por una
  bajada única del hilo del agente (`core.worker.lower_current_thread_priority`).
- `core/file_log.py`: la rotación diaria nunca rotaba; quitado el código de Telegram pegado por error;
  escritura por lotes; lecturas no finitas (NaN) ya no rompen el log.
- `core/reporter.py`: las respuestas y los gráficos iban al chat por defecto; ahora vuelven al chat que
  preguntó. El contador de «anomalías abiertas» del latido siempre valía 0. La salud ya ve canales muertos.
- `simulation/excel_replay.py`: KeyError con la columna «Salida HOT» (el Excel del controlador no cargaba).
- `analysis/product_log.py`: `products.json` se escribe de forma atómica.
- `core/logging_filters.py`: el token de Telegram también se oculta dentro de los tracebacks.
- Fallos de SITRAD multi-instrumento: el contador de fallos ya no se reinicia si todo falló.

### Multi-instrumento y lugares (activado por defecto)
- `sources/sitrad_api.py`: `LocationInfo` y `locations` en el config; un lugar agrupa varios instrumentos
  y publica bajo un solo `device_id`; colisiones de nombre → `"Canal [Instrumento]"`; lugares automáticos;
  `multi_instrument: false` para un solo instrumento; redescubrimiento si SITRAD no respondía al arrancar;
  el `channel_map` del config se suma al del tipo.
- Estado separado por lugar: perfil aprendido, monitor de frío, incidentes de la política, `QualityGuard`,
  contexto del informe y diagnóstico. Mensajes con el nombre del lugar cuando hay más de uno.
- El agente resuelve el lugar por nombre, tipo («el horno») o número («cava 2»).

### Hilo del agente
- `core/worker.py`: `AgentWorker` (FIFO, prioridad baja) e `InlineWorker` (tests). Preguntas,
  `render_policy_message`, párrafo LLM del informe y diagnóstico de alarmas van al hilo; el ciclo
  principal decide qué se envía y prepara el contexto. `SeriesStore` y `RetryNotifier` con lock.

### Rendimiento
- EventEngine, perfil, anomalías y salud solo sobre canales catalogados (`analysis.analysis_channels`);
  un lugar sin canales catalogados se analiza entero.
- `BehaviorProfile.fit_if_due`: reajuste cada 5 min mientras aprende y cada 30 min después.
  `warm_start` en un solo lote y por lugar.
- `SeriesStore`: búsqueda binaria, poda por lotes de la serie ingerida, `device_series`,
  retención corta para passthrough. `_build_period_context` cacheado por ciclo y lugar, reutiliza los
  eventos del último análisis y solo se construye si el mensaje sale.
  `read_observations_since` en streaming y saltando archivos rotados anteriores al corte.
- El informe de frío solo se reevalúa con temperatura nueva (o cada minuto) y con `cooling.history_hours`.
- Medido con el config real y 139 canales: 24 ms → 6 ms por ciclo; simulador de 12 h: 30 s → 18 s.

### Limpieza de datos
- `core/retention.py`: limpieza diaria programada (JSONL por antigüedad, rotados, gráficos, exportaciones,
  tope total opcional). Lecturas crudas de la API en un archivo por día.

### Código muerto eliminado
- Prompts y métodos sin uso (`EVENT_PROMPT`, `INCIDENT_PROMPT`, `_render_temperature`, `_looks_binary`,
  `get_baseline`, `get_recovery`, `update_baseline`, `get_typical_behavior`, `_hourly_behavior`,
  `_daily_behavior`, `_typical_events`, `parse_load`, `alarms_of`, `set_scenario`, `reset`, `report_now`,
  `_log_report`...), configuración que nadie leía e imports sin usar.

### Tests
- `tests/test_locations_worker_retention.py`: 19 tests nuevos (70 en total).

## Tarea 0 — inspect_sitrad_api
`tools/inspect_sitrad_api.py`: `datetime/timezone/timedelta` se importan al inicio desde `datetime` (no dentro de `main()`).

## Tarea 1 — Datalogger
- `core/channel_catalog.py`: 4 canales (`Datalogger Activo/Lleno/Uso/Ya Sobrescribió`). `config.json`: `channel_map` y alias.
- `analysis/diagnosis.py`: features `datalogger_*` y la hipótesis `datalogger_lleno`.
- **Desvío del pedido**: `datalogger_lleno` NO compite como causa. Va a `bundle.avisos` (`[AVISO] …` en `to_context()`), porque
  el datalogger está siempre al 100 % y, si compitiera, `is_unexplained` vería cualquier incidente como «explicado» y la
  hipótesis ganaría a las causas reales. Esto está en `INFORMATIVE_HYPOTHESES`.
- `flat_signal_exempt_channels` ampliado (constantes: Datalogger Uso, Límite Alarma Temp, Duración Deshielo Config, Diferencial, Horas Compresor).

## Tarea 2 — Historia
- `FileLog.read_observations_since(cutoff)`: lee el archivo vigente **y los rotados** (`observations.AAAAMMDD.jsonl`), solo VALID, ordenado, tolera líneas corruptas.
- `run_reporter.py`: replay al arrancar (solo `store.ingest`), **solo en modo sitrad** (en demo las fechas son simuladas) y con `storage.replay_on_start`.
- Agente: tool `history_coverage`, rutas («qué historia tienes»…), y el mensaje «Solo tengo datos desde…» en `temperature_report`, `get_stats`, `aggregate`, `export_csv` cuando la ventana pedida empieza antes de la historia (holgura: máx(10 min, 10 %)).
- Nuevo `analysis/time_ranges.py`: «ayer», «anteayer», «hoy», «esta semana», «la semana pasada», «última semana» (día calendario local).
- `config.json`: `storage.max_hours = 168`.

## Tarea 3 — Episodios
- `analysis/episode_history.py` (`record/query/summary`, JSONL idempotente por id), `CoolingMonitor.drain_closed()`, `Reporter._process_cooling` registra los cerrados. Tools `get_episodes`, `episode_summary` y rutas. `config.json`: `storage.episodes_log`.

## Tarea 4 — Gráficos
`_tool_render_chart` acepta `canales`, `desde/hasta`, `marcar_limite`, `marcar_episodios`; dos ejes (°C / estados); firma antigua (`canal`) sigue funcionando.

## Tarea 5 — Calidad de datos
`analysis/quality.py`, tools `data_quality` y `data_quality_canal`; `get_stats` añade `advertencia` si la cobertura < 80 %.

## Tarea 6 — Agregación y CSV
`analysis/aggregations.py` (`aggregate`, `export_csv`) y tools `aggregate` / `export_csv`.

## Tarea 7 — Bugs
7.1 foto del LLM (`photo_result`) · 7.2 «res» fuera de `KNOWN_PRODUCTS` (el `\b` ya estaba) · 7.3 caption honesto al recortar minutos ·
7.4 `_CHART_ASK` · 7.5 ya estaba (`mode`) · 7.6 mensaje de `door_summary` · 7.7 no aplicaba (no existe `Estado Proceso`) · 7.8 `pytest>=7`.
Bug propio hallado por los tests: `read_observations_since` leía el archivo vigente dos veces; corregido.

## Tarea 8 — Tests
`tests/`: units, diagnosis, filelog, route, episodes/agg/quality (30 tests).

## Pendiente
- Enviar el CSV por Telegram (`sendDocument`); hoy solo queda en `logs/exports/`.
- Cargas de producto por lugar (hoy `products.json` es global).
- Los criterios que requieren cava real (ver `TO_TEST.md`).
