# CHANGELOG

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
- Enviar el CSV por Telegram (`sendDocument`): no lo pediste; hoy solo queda en `logs/exports/`.
- Ajustar `smoke_reporter.py` / `run_validation.py` a los canales nuevos: no se tocaron.
- Los 3 criterios que requieren cava real (ver `TO_TEST.md`).
