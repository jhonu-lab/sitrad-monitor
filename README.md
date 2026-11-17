# Bot de cava (TC-900E + SITRAD PRO)

Sistema de monitoreo y diagnóstico asistido por LLM para una cava de refrigeración
controlada por un `TC-900E` a través de SITRAD PRO. **Lee, mapea y diagnostica**
lo que SITRAD expone; **no escribe** ni controla el compresor.

---

## Capas

```text
SITRAD API / simulador
        │
        ▼
sources/ (SITRAD cliente + descubrimiento)
        │
        ▼
core/SeriesStore (168 h en memoria) ──► logs/observations.jsonl (persistencia + replay)
        │
        ▼
analysis/ (determinista)
  engine · event_engine · anomaly_detector · baseline · diagnosis
  cooling_report · rise_watch · status_report · forecast · aggregations
  episode_history · quality · product_log · feedback_log · time_ranges
        │
        ▼
agent/cava_agent.py (21 tools + router determinista; el LLM solo redacta)
        │
        ▼
core/reporter.py (orquesta 10 ramas)
        │
        ▼
notifiers/ (Telegram con foto/documento · consola · reintentos)
```

**Principio rector**: las cifras y decisiones las calcula Python.
El LLM solo redacta bajo `numeric_anchor_ok` (descarta cifras inventadas) y
`strip_fake_media` (descarta URLs/imágenes inventadas). Fallback determinista en todo.

---

## Modos

`config.json → mode`:

| Modo | Fuente | Reloj |
|---|---|---|
| `sitrad` | API local de SITRAD PRO en vivo | real |
| `demo` | simulador `cava_random`, replay Excel o replay JSONL | acelerado (`speed`) |

---

## Canales del TC-900E (137 en total)

- **23 catalogados** (se mapean a canales del bot):
  `Temp Cámara` (sonda S1 «Ambiente»), `Setpoint`, `Desvío vs Setpoint`,
  `Diferencial`, `Estado Refrigeración` (REFR), `Estado Deshielo` (DEFR),
  `Estado Ventilador` (FANS), `Temp Evaporador`, `Puerta Abierta`,
  `Horas Compresor`, `Límite Alarma Temp`, `Duración Deshielo Config`,
  4 alarmas (`IsAlrHighTempS1`, `IsAlrLowTempS1`, `IsAlrmDoorOpn`, `IsDefrTimeOut`),
  3 errores de sonda (`IsErrorS1/S2/S3`), 4 canales del datalogger.

- **116 passthrough** (no catalogados, pero **sí ingestados** con `passthrough=true`):
  variables de configuración (`Fnc*`), flags internos (`Is*`), fecha/hora (`Day`, `Month`, `Year`...),
  estado de proceso (`ProcessStatusText`, `Proceso actual`), etc.
  Se pueden consultar con `get_stats`, `aggregate`, `top_variations`, `render_chart`.

- **2 canales deshabilitados** (llegan con `enabled=false` y el bot los descarta honestamente):
  `Puerta Abierta` (requiere `IsOpenDoorEnabled=true`) y `Temp Evaporador` (requiere `IsS2Enabled=true`).

**Multipower**: si está conectado, se detecta automáticamente (M1 + M2, 30 canales)
y se activan las 4 hipótesis eléctricas. **No está en la instalación actual**.

---

## Capacidades del diagnóstico

| Escenario | Hipótesis activas | Cobertura de fallas |
|---|---|---|
| **TC-900E base (hoy)** | **8** | ~55 % |
| + Puerta y S2 habilitados | 11 | ~75 % |
| + Multipower instalado | 15 | ~95 % |

**Activas hoy** (8): `sin_comunicacion`, `sensor_congelado`, `deshielo_programado`,
`deshielo_extendido`, `alarma_temp_alta_controlador`, `alarma_temp_baja_controlador`,
`sensor_error`, `datalogger_lleno` (como aviso, no como causa).

**Requieren hardware** (no código): `puerta_prolongada`, `entrada_producto`,
`capacidad_reducida` (necesitan puerta y/o evaporador habilitados);
`compresor_sin_arranque`, `fallo_refrigeracion`, `baja_tension`, `carga_grande`
(necesitan Multipower o cualquier medidor eléctrico).

Las hipótesis se ordenan por evidencia, con `a_favor`, `en_contra` y `faltan`
(datos que faltarían para confirmarlas). Nunca se afirma una causa: "compatible con",
"podría estar relacionado con", "no puede determinarse con estos datos".

---

## Herramientas expuestas al LLM (21)

| Categoría | Herramientas |
|---|---|
| **Lectura** | `list_channels`, `list_converters`, `get_stats`, `get_slope`, `get_events`, `correlate`, `energy` |
| **Estado** | `status_report`, `diagnose_now`, `controller_summary`, `data_coverage`, `data_quality`, `data_quality_canal` |
| **Historial** | `history_coverage`, `get_episodes`, `episode_summary`, `alarm_history` |
| **Análisis** | `top_variations`, `explain_temperature`, `compressor_relation`, `correlate_with_alarms` |
| **Predicción** | `forecast_temperature`, `temperature_report`, `load_report` |
| **Export** | `aggregate`, `export_csv`, `render_chart` (PNG) |
| **Memoria** | `historial_usuario` |

El router determinista resuelve antes del LLM: "temperatura actual", "qué hardware hay",
"canales del TC-900E", "cuántas alarmas", "hazme un gráfico", "qué pasará en 1 hora",
"por qué sube la temperatura", "hay producto", "cómo está la cava", "el de las 06:17 fue la puerta", etc.

---

## Alarmas de SITRAD

- **Captura**: `GET /alarms?startDate=…&endDate=…` cada **60 s** (`sitrad.alarms_every_seconds`).
- **Persistencia**: `sitrad_data/sitrad_alarms.jsonl` (una línea por alarma, append-only).
- **Chat**: se reenvían con hora, texto y la temperatura de la cámara del momento.
- **Historial**: `alarm_history(desde, hasta, instrumento, codigo)` para consultas.
- **Correlación**: `correlate_with_alarms(canal, desde, hasta)` cruza variaciones con alarmas.
- **Diagnóstico**: los flags `IsAlr*` / `IsError*` del controlador alimentan `_hyps` directamente.

**Limitación conocida**: SITRAD manda `instrumentId` numérico, no `instrumentName`; el campo
`instrument` del registro queda vacío hasta que se resuelva el mapa `instrumentId → nombre`.
Está documentado como deuda en `NOTES.md`.

---

## Historial y replay

- **Retención**: 168 h en `SeriesStore` (`storage.max_hours`).
- **Persistencia**: `logs/observations.jsonl` (y rotados diarios `observations.YYYYMMDD.jsonl`).
- **Replay al arrancar**: `FileLog.read_observations_since(cutoff)` reconstruye el store
  sin re-emitir alertas antiguas. Verificado: 402 lecturas recuperadas en el último arranque.
- **SITRAD no expone el datalogger por API**: 17 endpoints probados, todos 404. El historial
  del bot es **lo que él mismo grabó**. Para historia anterior al primer arranque: export manual
  desde la GUI de SITRAD y `ExcelReplaySource`.

---

## Seguridad

- **Solo lectura** contra SITRAD: `GET /instruments`, `GET /values`, `GET /alarms`, `GET /converters`.
  Cero `POST`, `PUT`, `DELETE`. **No manda comandos al compresor.**
- **LLM local** (Ollama en `localhost:11434`). Sin envío de datos a internet salvo Telegram
  (transporte de mensajes al chat autorizado).
- **Sin `exec`/`eval`/`subprocess`**. Las tools solo leen del `SeriesStore` y escriben en `logs/`.
- **Anclaje numérico**: cualquier cifra del LLM que no esté en el contexto se descarta.
- **`RedactTokens`**: filtro de logging que oculta el token de Telegram en excepciones de `requests`.
- **Credenciales por variable de entorno** (recomendado): `SITRAD_USER`, `SITRAD_PASSWORD`.
- **`verify_ssl=false`** por defecto (certificado autofirmado local). En producción: instalar
  certificado y poner `sitrad.verify_ssl: true`.

Detalle completo en `NOTES.md` §9.

---

## Arranque

```powershell
# 1. Dependencias
pip install -r requirements.txt

# 2. Credenciales SITRAD (recomendado: variables de entorno, no en config.json)
$env:SITRAD_USER = "admin"
$env:SITRAD_PASSWORD = ""

# 3. Arrancar contra SITRAD
python run_reporter.py --mode sitrad

# 4. Verificación previa (opcional, 30 s)
python tools/verify_python.py
python tools/scan_unused.py --entry run_reporter
```

Al arrancar, en los logs:

```text
SITRAD: 1 convertidor(es), 1 instrumento(s), 137 canales efectivos (23 catalogados + 116 passthrough).
FileLog replay: N lecturas desde 2026-11-XXT…
Reporter iniciado. polling=5.0s
```

En consola (o Telegram si hay token) puedes escribir las preguntas habituales.

---

## Estructura del proyecto

```text
sitrad-monitor/
├── run_reporter.py            # entry point
├── config.json                # configuración (sin secretos)
├── requirements.txt
├── README.md                  # este archivo
├── NOTES.md                   # capacidades reales, decisiones, deuda técnica
├── paper_cava.tex             # artículo para revisión
│
├── agent/                     # CavaAgent, LLM client, renderer
├── analysis/                  # 16 módulos deterministas (engine, diagnosis, cooling...)
├── core/                      # contracts, series_store, reporter, policy, file_log
├── sources/                   # sitrad_api (cliente + discover_all + passthrough)
├── notifiers/                 # telegram (foto/documento), consola, reintentos
├── simulation/                # simuladores y replay
├── tools/                     # inspect_sitrad_api, inspect_excel, verify_python, scan_unused
└── tests/                     # 30 tests unitarios
```

---

## Verificación

```powershell
python -m pytest tests -q                          # 30 verdes
python tools/scan_unused.py --entry run_reporter   # 0 huérfanos
python tools/verify_python.py                      # 12 checks OK
```

---

## Limitaciones conocidas

1. **Multi-instrumento**: cataloga todos los convertidores e instrumentos de SITRAD
   (`discover_all`), pero monitoriza solo el instrumento activo (`self.instrument_id`).
   Para monitorizar N cavas simultáneamente hay que añadir multi-source (~1-2 días).
   Está documentado en `NOTES.md` como deuda.
2. **Puerta y evaporador deshabilitados** en el TC-900E: el bot los descarta honestamente.
   Para desbloquear `puerta_prolongada`, `entrada_producto`, `capacidad_reducida`: habilitar
   `IsOpenDoorEnabled=true` e `IsS2Enabled=true` en el controlador.
3. **Multipower no instalado**: 4 hipótesis eléctricas inactivas hasta que haya un medidor.
4. **Datalogger de SITRAD no expuesto por API**: el bot solo reconstruye lo que grabó él mismo.
5. **`alarm_history` con atribución parcial**: el campo `instrument` del registro puede quedar
   vacío si SITRAD manda solo `instrumentId`. Pendiente de resolver el mapa en `describe_alarm`.
6. **`llm_analysis` desactivado por defecto**: el párrafo de análisis LLM encima del informe
   es opcional y está apagado (`cooling.llm_analysis: false`).

---

## Documentación adicional

- `NOTES.md` — capacidades reales, decisiones de diseño, deuda técnica, respuestas
  para el ingeniero, checklist de pruebas manuales.
- `paper_cava.tex` — artículo completo en inglés para revisión (Overleaf-ready).
- `TO_TEST.md` — pruebas manuales pendientes (cuando haya cava real).
- `config.json` — cada sección tiene comentarios en `NOTES.md`.
