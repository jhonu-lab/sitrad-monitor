# Bot de monitoreo SITRAD (TC-900E, Multipower y otros instrumentos)

Sistema de monitoreo y diagnóstico asistido por LLM para instalaciones controladas
a través de SITRAD PRO: cavas de refrigeración (TC-900E), medidores eléctricos
(Multipower) y cualquier otro instrumento que SITRAD exponga (hornos, calderas...).
**Lee, mapea y diagnostica** lo que SITRAD expone; el bot **no escribe** ni controla equipos
(ver la nota sobre `tools/` en [Seguridad](#seguridad)).

---

## Capas

```text
SITRAD API / simulador / repetición de Excel o JSONL
        │
        ▼
sources/ (cliente SITRAD, descubrimiento, LUGARES multi-instrumento)
        │
        ▼
core/SeriesStore (en memoria, con lock) ──► logs/observations.jsonl (rotación diaria + replay)
        │
        ▼
analysis/ (determinista, por lugar)
  engine · event_engine · anomaly_detector · baseline · diagnosis
  cooling_report · rise_watch · status_report · forecast · aggregations
  episode_history · quality · product_log · feedback_log · time_ranges
        │
        ▼
core/reporter.py (ciclo principal: ingesta, eventos, anomalías, frío, alarmas, salud, limpieza)
        │                                   │
        │                                   ▼
        │                     core/worker.py (hilo del agente, prioridad baja)
        │                       agent/cava_agent.py (herramientas + router; el LLM solo redacta)
        ▼                                   │
notifiers/ (Telegram multi-chat · consola · reintentos por chat) ◄──┘
        │
core/retention.py (limpieza programada del disco)
```

**Principio rector**: las cifras y decisiones las calcula Python. El LLM solo
redacta bajo `numeric_anchor_ok` (descarta cifras inventadas) y `strip_fake_media`
(descarta URLs/imágenes inventadas). Hay respuesta determinista para todo.

**Hilos**: el ciclo principal nunca espera al LLM. Las preguntas, la redacción de
mensajes, el párrafo opcional del informe y el diagnóstico de alarmas van a un hilo
de trabajo con prioridad de CPU baja (`operations.agent_worker`, activo por defecto).

**Ciclo principal** (cada `polling_seconds`): reintentos de Telegram → ingesta →
eventos → anomalías → frío y alarmas de SITRAD → salud → latido → resumen de turno →
preguntas → limpieza programada.

---

## Modos

`config.json → mode` (o `--mode`):

| Modo | Fuente | Reloj |
|---|---|---|
| `sitrad` | API local de SITRAD PRO en vivo | real |
| `demo` | simulador `cava_random`, repetición de Excel o de JSONL | acelerado (`simulation.speed`) |

En modo `demo`, la llamada al LLM es síncrona: con `simulation.speed` alto y un LLM
lento se «saltan» minutos simulados de muestreo. Se recomienda `speed` entre 2 y 4.

---

## Multi-instrumento y lugares

Activado por defecto (`sitrad.multi_instrument: true`): el bot lee **todos** los
instrumentos de SITRAD y los agrupa en **lugares**. Cada lugar es un `device_id`
con su propio perfil aprendido, su informe de frío y sus incidentes; un lugar
puede **juntar varios instrumentos** (p. ej. el controlador de la cava y el
Multipower que mide su compresor).

```json
"locations": [
  {"name": "Cava Colanta", "kind": "cava",  "instruments": [1, "Multipower M1/M2"]},
  {"name": "Horno 1",      "kind": "horno", "instruments": [3], "device_id": 3001}
]
```

- `instruments`: id de SITRAD o nombre del instrumento (sin importar mayúsculas ni tildes).
- `kind`: `cava`, `camara`, `congelador`, `nevera`... activan el informe de temperatura;
  `horno`, `caldera`, `energia`, `otro` no. Se puede forzar con `"cooling": true|false`.
- `device_id` (opcional): por defecto `1000 + id del primer instrumento` (así la
  historia ya grabada sigue sirviendo).
- Instrumentos sin lugar asignado: un lugar automático cada uno
  (`sitrad.auto_locations: true`).
- Si dos instrumentos del mismo lugar traen el mismo canal, el segundo se publica como
  `"Canal [Nombre del instrumento]"`.
- `sitrad.multi_instrument: false` vuelve al modo de un solo instrumento (`sitrad.instrument_id`).

Si SITRAD no responde al arrancar, el bot reintenta el descubrimiento cada minuto y los
lugares aparecen solos cuando vuelve. Los avisos por fallo de lectura solo cuentan como
racha cuando fallan **todos** los instrumentos.

En el chat: «¿cómo está el horno?», «temperatura de la cava 2», «¿cuánto consume la
Cava Colanta?» resuelven el lugar por nombre, tipo o número. Si la pregunta es ambigua
(«la cava» con dos cavas) el bot lo indica en lugar de adivinar. Los mensajes
automáticos llevan el nombre del lugar cuando hay más de uno.

> **Hipótesis por confirmar:** se asume que M1 mide el circuito de la cava y M2 un
> circuito independiente. El simulador usa la misma suposición. Confírmela con el
> ingeniero de la instalación antes de interpretar diagnósticos eléctricos.

---

## Canales del TC-900E (137 en total)

- **23 catalogados** (se mapean a canales del bot): `Temp Cámara`, `Setpoint`,
  `Desvío vs Setpoint`, `Diferencial`, `Estado Refrigeración`, `Estado Deshielo`,
  `Estado Ventilador`, `Temp Evaporador`, `Puerta Abierta`, `Horas Compresor`,
  `Límite Alarma Temp`, `Duración Deshielo Config`, 4 alarmas, 3 errores de sonda,
  4 canales del datalogger. El `channel_map` del config se suma al del tipo.
- **116 passthrough** (no catalogados, ingeridos con `passthrough: true`): parámetros
  `Fnc*`, flags `Is*`, fecha/hora, estado de proceso... Se consultan con `get_stats`,
  `aggregate`, `top_variations`, `render_chart`. Guardan menos historia en memoria
  (`storage.passthrough_max_hours`, 24 h por defecto) y **no** entran en el análisis
  automático (ver `analysis.analysis_channels`).
- **Deshabilitados** (`enabled=false`, el bot los descarta): `Puerta Abierta`
  (requiere `IsOpenDoorEnabled`) y `Temp Evaporador` (requiere `IsS2Enabled`).

**Filtros de calidad antes de guardar una lectura**: temperatura plausible
(−60 a 150 °C), sensor habilitado en el controlador (`IsS*Enabled`) y deduplicación por
fecha. Lo descartado se marca `MISSING` y conserva el valor crudo. Un sensor que repite
el mismo valor 120 lecturas seguidas pasa a `FROZEN`.

**Multipower**: se detecta solo (M1 + M2) y activa las hipótesis eléctricas del lugar al que pertenece.

---

## Capacidades del diagnóstico

| Escenario | Hipótesis activas | Cobertura de fallas |
|---|---|---|
| **TC-900E base** | **8** | ~55 % |
| + Puerta y S2 habilitados | 11 | ~75 % |
| + Multipower en el mismo lugar | 15 | ~95 % |

Las hipótesis se ordenan por evidencia (`a_favor`, `en_contra`, `faltan`). Nunca se
afirma una causa: «compatible con», «podría estar relacionado con».

El diagnóstico de un incidente abierto se **reevalúa cada 240 s**: si la causa principal
cambia, se mantiene durante 2 revisiones seguidas y tiene confianza ≥ 0.5, el bot envía
una actualización. Las hipótesis benignas (operación normal, como un deshielo programado)
se silencian y no generan alerta.

---

## Herramientas del agente

| Categoría | Herramientas |
|---|---|
| **Lectura** | `list_channels`, `list_converters`, `get_stats`, `get_slope`, `get_events`, `correlate`, `energy` |
| **Estado** | `status_report`, `diagnose_now`, `controller_summary`, `data_coverage`, `data_quality`, `data_quality_canal` |
| **Historial** | `history_coverage`, `get_episodes`, `episode_summary`, `alarm_history`, `historial_usuario` |
| **Análisis** | `top_variations`, `explain_temperature`, `compressor_relation`, `correlate_with_alarms` |
| **Predicción** | `forecast_temperature`, `temperature_report`, `load_report` |
| **Export** | `aggregate`, `export_csv`, `render_chart` (PNG) |

El router determinista resuelve antes del LLM las preguntas habituales (alarmas,
gráficos, cobertura, CSV, agregados, episodios, pronósticos). El LLM no accede
directamente al almacén ni al motor: solo pide herramientas, y cualquier error de una
herramienta se devuelve como `{"ok": false, "error": ...}`.

---

## Modelo de lenguaje (Ollama)

`llm.provider` puede ser `ollama` (modelo local), `template` (solo plantillas, **no**
responde preguntas abiertas) o `demo` (LLM falso con regex, para pruebas).

Para crear el modelo local que usa el bot:

```powershell
ollama create cava-qwen -f Modelfile
```

El `Modelfile` parte de `qwen2.5:7b-instruct-q3_K_M` con `temperature 0.1`,
`num_thread 4` (bajo a propósito, para no competir con la lectura de SITRAD) y
`num_predict 1024`.

> **Revisar:** `Modelfile` fija `num_ctx 4096` mientras `config.json` pide
> `llm.num_ctx = 6144`. Alinee ambos valores y recree el modelo si sube el contexto.

- **Circuit breaker**: tras varios fallos seguidos el bot deja de llamar a Ollama durante
  unos minutos y responde con plantillas.
- **Arranque tardío**: si Ollama no estaba levantado, el cliente reintenta verificar el
  modelo y pasa a usarlo solo, sin reiniciar el bot.
- **Auditoría**: se registran modelo, latencia, tokens y hashes de entrada y salida, no el texto.

---

## Telegram

- `telegram.chat_id` (+ `telegram.chat_ids` para más chats): reciben las alertas y
  son los únicos chats desde los que se aceptan preguntas.
- **Cada respuesta vuelve al chat donde se hizo la pregunta** (texto y gráficos).
- Los reintentos se hacen por chat: si un chat falla, no se repite el mensaje a los demás.
  La cola de pendientes está limitada (`max_retries`, por defecto 100 mensajes, aunque el
  nombre sugiera «reintentos por mensaje»); al llenarse se descartan los más viejos.
- `telegram.allowed_user_ids` (opcional): lista blanca de usuarios.
- Token y chat por variable de entorno (recomendado): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
  Si el token estuvo alguna vez en un archivo compartido, revócalo con @BotFather.

**Comandos**

| Comando | Qué hace |
|---|---|
| `/start`, `/help`, `/ayuda` | Muestra la ayuda con ejemplos |
| `/preguntar`, `/ask`, `/p` + texto | Hace una pregunta |
| `/historial` | Muestra las últimas preguntas y respuestas de quien lo escribe |
| `/olvidar` | Borra la memoria de conversación de quien lo escribe |

En un chat privado basta escribir la pregunta. **En grupos** el bot solo responde si lo
mencionan (`@bot`), si responden a un mensaje suyo o si usan un comando; así no
contesta la conversación normal. La memoria de conversación es por usuario
(`chat_id:user_id`).

**Mensajes largos** (más de 4000 caracteres) se dividen y se numeran (1/n, 2/n...).

---

## Alarmas de SITRAD

- **Captura**: `GET /alarms` cada `sitrad.alarms_every_seconds` (ventana de 15 min).
  La primera consulta solo «prepara» y no anuncia alarmas ya existentes, salvo que
  `announce_existing_alarms` esté activo.
- **Persistencia**: `sitrad_data/sitrad_alarms.jsonl` (junto al programa).
- **Chat**: se reenvían con el lugar, la hora, el texto y la temperatura del lugar;
  si es de temperatura, con el diagnóstico de ESE lugar.
- **Historial y correlación**: `alarm_history`, `correlate_with_alarms`.

---

## Historial, replay y limpieza de datos

- **Memoria**: `storage.max_hours` (168 h) para canales catalogados;
  `storage.passthrough_max_hours` (24 h) para el resto.
- **Persistencia**: `logs/observations.jsonl`; con `operations.rotation.mode: "daily"`
  se rota a `observations.AAAAMMDD.jsonl` al cambiar el día local (también tras reiniciar).
- **Replay al arrancar** (modo sitrad): se lee en streaming solo lo posterior al corte. El
  replay **no** pasa por el motor de eventos ni por el informe de frío, para no reenviar
  alertas de días pasados.
- **Limpieza programada** (`storage.retention`): una vez al día a la hora local
  `run_at_hour_local` (03:00 por defecto) y al arrancar si quedó pendiente. Recorta los
  JSONL por antigüedad, borra rotados viejos, gráficos y exportaciones, y opcionalmente
  aplica un tope total (`max_total_mb`). Días por defecto:

  | Datos | Días | | Datos | Días |
  |---|---|---|---|---|
  | observations | `rotation.keep_days` (7) | | episodes | 365 |
  | raw_readings (lecturas crudas) | 3 | | conversations | 30 |
  | events | 90 | | alarms | 180 |
  | reports | 30 | | charts | 7 |
  | cases / cooling_cases | 180 | | exports | 30 |
  | feedback | 365 | | sim_truth | 7 |

  Las lecturas crudas de la API (`sitrad.save_raw`) se guardan en un archivo por día
  (`sitrad_data/lecturas.AAAAMMDD.jsonl`): son lo que más ocupa.

---

## Seguridad

- **El bot es de solo lectura** contra SITRAD (`GET`). No manda comandos a ningún equipo.
- **LLM local** (Ollama). Fuera de la máquina solo sale Telegram.
- **Sin `exec`/`eval`/`subprocess`** en el bot.
- **`RedactTokens`** oculta el token de Telegram en mensajes **y tracebacks** de los logs.
- **Credenciales por variable de entorno**: `SITRAD_USER`, `SITRAD_PASSWORD`, `TELEGRAM_BOT_TOKEN`.
  No escriba el token en `config.json`; si ya estuvo ahí, revóquelo y genere uno nuevo.
- **`verify_ssl=false`** por defecto (certificado autofirmado local).

> **Excepciones en `tools/`.** La garantía de solo lectura vale para el bot
> (`run_reporter.py`), no para todos los scripts de `tools/`:
> `fix_sitrad_db.py` modifica la base de datos de SITRAD, `force_poll.py` reinicia su
> servicio de Windows y `hack_sitrad_api.py` envía `DELETE` y `POST` a la API. Úselos solo
> de forma manual, con SITRAD detenido o con copia de seguridad. Además,
> `inspect_api_network.py`, `scan_gui_api.py` y `hack_sitrad_api.py` traen credenciales de
> prueba (`admin/admin12`) escritas en el código; cámbielas o muévalas a variables de entorno.

---

## Arranque

```powershell
pip install -r requirements.txt
$env:SITRAD_USER = "admin"
$env:SITRAD_PASSWORD = ""
$env:TELEGRAM_BOT_TOKEN = "<token>"
python run_reporter.py --mode sitrad
```

Opciones de inspección (no arrancan el bot):

```powershell
python run_reporter.py --inspect-api                    # qué devuelve SITRAD y cómo se mapea
python run_reporter.py --inspect-excel ruta.xlsx        # qué instrumento y canales trae un Excel
python run_reporter.py --inspect-excel ruta.xlsx --output informe.json
```

Al arrancar, en los logs:

```text
InstrumentRegistry: 0 convertidor(es), 2 instrumento(s): ... Lugares: Cava Colanta [cava, device_id=1001: ...]
  · lugar Cava Colanta [cava, informe de frío] device_id=1001 ← Cava Colanta (MT-512E), Multipower M1/M2
FileLog replay: N lecturas desde 2026-11-XXT…
Reporter iniciado. polling=5.0s
```

Si Ollama no está levantado al arrancar, el bot responde con las plantillas
deterministas y empieza a usar el modelo en cuanto Ollama responde.

---

## Configuración nueva (resumen)

Ver `config.example.json` (sin secretos). Claves añadidas en esta versión:

| Clave | Defecto | Para qué |
|---|---|---|
| `locations` | `[]` | Lugares y sus instrumentos |
| `sitrad.multi_instrument` | `true` | Leer todos los instrumentos |
| `sitrad.auto_locations` | `true` | Lugar automático para instrumentos sin asignar |
| `telegram.chat_ids` / `allowed_user_ids` | — | Más chats / lista blanca |
| `operations.agent_worker` | `true` | LLM y preguntas en hilo aparte |
| `analysis.analysis_channels` | `"catalog"` | `"all"` analiza también los passthrough |
| `analysis.profile_refit_minutes` | `30` | Reajuste del perfil aprendido |
| `analysis.profile_learning_refit_minutes` | `5` | Reajuste mientras aprende |
| `cooling.history_hours` | `24` | Historia que usa el informe de frío en cada paso |
| `storage.passthrough_max_hours` | `24` | Memoria para canales no catalogados |
| `storage.retention` | ver tabla | Limpieza programada |

---

## Estructura del proyecto

```text
sitrad-monitor/
├── run_reporter.py            # punto de entrada
├── config.json                # configuración local (no se versiona)
├── config.example.json        # plantilla sin secretos
├── products.json              # cargas de producto en cámara
├── Modelfile                  # receta del modelo Ollama (cava-qwen)
├── requirements.txt           # requests, urllib3, pandas, openpyxl, matplotlib, pytest
├── pyproject.toml             # configuración de pytest
├── agent/                     # CavaAgent, cliente LLM, renderer
├── analysis/                  # módulos deterministas
├── core/                      # contracts, series_store, reporter, worker, retention, policy, file_log
├── sources/                   # sitrad_api (cliente, descubrimiento, lugares, passthrough)
├── notifiers/                 # Telegram, consola, reintentos
├── simulation/                # simuladores y repetición
├── tools/                     # scripts de validación, inspección y mantenimiento (ver abajo)
└── tests/                     # pruebas automáticas con pytest
```

---

## Herramientas de línea de comandos (`tools/`)

Ninguna forma parte del ciclo normal del bot. Se ejecutan a mano.

| Propósito | Scripts |
|---|---|
| Validar que el diagnóstico acierta | `run_validation.py`, `case_report.py`, `run_multiseed.py`, `feedback_report.py`, `label_case.py` |
| Comprobar el código | `verify_python.py`, `smoke_reporter.py`, `scan_unused.py`, `find_duplicates.py` |
| Entender los datos de SITRAD | `audit_real_data.py`, `inspect_excel.py`, `check_excel.py`, `sitrad_excel.py`, `demo_informe.py` |
| Explorar la API (solo lectura) | `inspect_sitrad_api.py`, `diagnose_sitrad.py`, `inspect_api_network.py`, `find_swagger.py`, `scan_gui_api.py` |
| Probar sin SITRAD | `mock_sitrad_server.py` |
| **Reparar SITRAD (modifican algo)** | `find_sitrad_db.py`, `inspect_sitrad_db.py` (solo lectura), `fix_sitrad_db.py`, `force_poll.py`, `hack_sitrad_api.py` |

`TO_TEST.md` (en la raíz y en `tools/`) es un checklist manual de pruebas pendientes,
no código.

---

## Verificación

```powershell
python -m pytest tests -q                          # pruebas automáticas
python tools/scan_unused.py --entry run_reporter   # 0 huérfanos
python tools/verify_python.py                      # imports OK
python tools/smoke_reporter.py --hours 6           # ciclo completo con simulador
```

Prueba contra un SITRAD falso:

```powershell
python tools/mock_sitrad_server.py --excel exceles/controlador.xlsx --port 8099 --advance request
# config: "sitrad": {"base_url": "http://127.0.0.1:8099/api/v1", "user": "x", "password": "x"}
python run_reporter.py --mode sitrad
```

### ¿Acierta el diagnóstico?

El simulador `cava_random` genera fallas con causa conocida (`logs/sim_truth.jsonl`) y
el bot las diagnostica sin saberlo. Luego se comparan:

```powershell
python tools/run_validation.py --days 4          # simula y escribe logs/cases.jsonl
python tools/case_report.py                      # acierto, falsas alarmas, matriz de confusión
python tools/run_multiseed.py                  # repite con varias semillas y agrega
python tools/feedback_report.py                  # acierto según las correcciones del ingeniero
python tools/label_case.py --list                # etiquetar a mano la causa real de un caso
```

> Las opciones exactas de cada script (`--days`, `--seeds`, `--warmup-h`, `--target`...)
> están en su `--help`. Las métricas que se citen en informes o artículos deben salir de
> estas ejecuciones, no de valores estimados.

---

## Limitaciones conocidas

1. **Productos (`products.json`) globales**: las cargas y reglas de producto se aplican
   a todos los lugares con informe de frío; aún no hay cargas por lugar.
2. **Ventana de alarmas fija (15 min)**: si SITRAD o el PC están caídos más tiempo,
   las alarmas de ese hueco no se recuperan.
3. **Puerta y evaporador deshabilitados** en el TC-900E actual.
4. **Datalogger de SITRAD no expuesto por API**: el bot reconstruye solo lo que grabó.
5. **`cooling.llm_analysis`** apagado por defecto (párrafo del LLM sobre el informe).
6. **Aprendizaje del diagnóstico**: `feedback_log` solo registra las correcciones del
   ingeniero; todavía no modifican el comportamiento del bot.
7. **Evaluación cuantitativa con simulador**: hay pocos datos reales etiquetados para
   intervalos de confianza ajustados.