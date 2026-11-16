 # NOTES — Bot de análisis de cava (SITRAD PRO + TC-900E)

> Documento de referencia del proyecto. Última actualización: **8-nov-2026**.
> Estado: **listo para instalar en la cava**, con un fix pendiente de 5 líneas (ver §0).

---
# NOTES — Bot de análisis de cava (SITRAD PRO + TC-900E)

> Documento de referencia del proyecto. Última actualización: **8-nov-2026**.
> Estado: **listo para instalar en la cava**, con un fix pendiente de 5 líneas (ver §0).

---

## 0. Estado del proyecto y fix pendiente

**Verificado el 8-nov-2026**:
- 30/30 tests verdes (`python -m pytest tests -q`).
- 0 módulos huérfanos (`tools/scan_unused.py --entry run_reporter`).
- 23 canales mapeados del `TC-900E`, 21 efectivos, 2 descartados con razón.
- Replay de historial verificado: **2880 lecturas** reconstruidas desde
  `logs/observations.jsonl` (desde el 2-nov).
- Compilación limpia.

### Fix pendiente (bloqueante, 5 líneas)

`CoolingMonitor._primed` no marca como vistos los episodios **en curso** al arrancar. Consecuencia: al
reiniciar el bot tras un apagado largo, emite como alerta un episodio antiguo que empezó hace días
(ej. "Cava sobre 4 °C hace 125 h"). En la cava real con el bot 24/7 solo pasaría en el primer arranque
y en cada reinicio por mantenimiento. **Aplicar en `analysis/cooling_report.py::CoolingMonitor.step()`**,
justo después del bloque `if not self._primed:` actual:

```python
```python
if not self._primed:
    for e in exs:
        self._alerted.add(e.key)
        if e.recovered:
            self._reported.add(e.key)
    self._primed = True
    return list(out_stale)
```
Después de aplicarlo, compilar y correr tests (deben seguir los 30 verdes).

## 1. Inicio rápido
```powershell
# 1. Dependencias
pip install -r requirements.txt

# 2. Credenciales SITRAD en variables de entorno (recomendado; no en config.json)
[Environment]::SetEnvironmentVariable("SITRAD_USER", "admin", "Machine")
[Environment]::SetEnvironmentVariable("SITRAD_PASSWORD", "********", "Machine")

# 3. Arrancar contra la API local de SITRAD
python run_reporter.py --mode sitrad

# 4. O arrancar con el simulador (sin hardware)
```
python run_reporter.py --mode demo
Al arrancar en sitrad, verás en los logs:

```text
FileLog replay: N lecturas desde <fecha>.
```
Reporter iniciado. polling=5.0s
N = lecturas reconstruidas del historial propio. 0 si es el primer arranque.

Interactúa escribiendo en la consola (o por Telegram si hay token). Ejemplos:

```text
temperatura actual
¿cómo está la cava?
qué historia tienes
calidad de los datos
hazme un gráfico de temperatura y compresor
dame los episodios de ayer
hay algún problema
```
hay 500 kg de ternera desde hace 20 minutos
el suero no debe subir de 12 grados
## 2. Qué hace el bot — capacidades reales
2.1 Arquitectura en una línea
```text
SITRAD API ──► sources/ ──► SeriesStore ──► analysis/ (determinista) ──► agent/ ──► reporter ──► Telegram/consola
```
                                               ▲                                          │
                                               └──── FileLog (logs/observations.jsonl) ◄──┘
Principio rector: las cifras las calcula Python; el LLM solo redacta. Cualquier número del LLM que no
esté en el contexto se descarta (numeric_anchor_ok) y se usa la plantilla determinista.

2.2 Lo que detecta hoy con TC-900E (sin tocar el controlador)
Capacidad	Hipótesis / tool	Depende de
Temperatura y tendencia	status_report, forecast_temperature, explain_temperature	Temp Cámara
Episodios >4 °C con duración y causa	temperature_report, CoolingMonitor	Temp Cámara
Aviso antes de cruzar el límite	rise_watch + forecast_temperature	Temp Cámara + REFR
Deshielo programado / extendido	deshielo_programado, deshielo_extendido	Estado Deshielo
Ciclo del compresor (duty, rachas)	controller_summary(compresor)	REFR
Setpoint y sus cambios	controller_summary(setpoint)	Setpoint
Alarma de puerta del MT	puerta_prolongada (vía IsAlrmDoorOpn)	alarma del MT
Alarma de temperatura del MT	alarma_temp_alta_controlador	IsAlrHighTempS1
Corte de comunicación	sin_comunicacion, _check_stale	ausencia de lecturas
Sensor congelado	sensor_congelado	Temp Cámara plana
Error de sonda	sensor_error	IsErrorS1/S2/S3
Datalogger al 100 %	[AVISO] en diagnose_now	Datalogger Uso
Producto + regulación + exposición	product_log, assess_loads, load_report	registro manual
Calidad de datos por canal	data_quality	store
Agregación y export	aggregate, export_csv	store
Historial propio	history_coverage, get_episodes, episode_summary	logs/observations.jsonl
Gráficos de 1 o varios canales	render_chart	store + matplotlib
Corrección y medición de acierto	feedback_log, case_book	registro manual
2.3 Lo que no puede hacer (limitación física, no de código)
Capacidad ausente	Por qué	Cómo activarla
puerta_prolongada con puerta física	IsOpenDoorEnabled=False	Habilitar en el MT (o cablear DI1/IsDig1Enabled)
entrada_producto (puerta corta)	Igual	Igual
capacidad_reducida	IsS2Enabled=False, evaporador llega -112,2 °C (basura)	Instalar sonda S2 y IsS2Enabled=True
compresor_sin_arranque	Necesita medir consumo (M1 Potencia Activa)	Instalar Multipower
fallo_refrigeracion	Igual	Igual
baja_tension	Igual	Igual
carga_grande	Igual	Igual
Lectura del datalogger SITRAD por API	SITRAD no lo expone (17 endpoints, todos 404)	Export manual desde la GUI de SITRAD
2.4 Números reales
Escenario	Hipótesis activas	Tools disponibles
Hoy (TC-900E, sin habilitar nada)	8	21
+ Puerta y S2 habilitados	11	23
+ Multipower instalado	15	26
## 3. Informes autónomos — el punto más fuerte
El bot no necesita que le preguntes. Dispara informes en 12 situaciones:

Disparador	Cuándo	Ejemplo
Alerta de episodio >4 °C	La cámara lleva >10 min sobre el límite	"🟠 Cava sobre 4 °C hace 25 min, máx. 6,3 °C. Causa probable: puerta abierta 14 min."
Informe de episodio recuperado	La cámara vuelve a ≤4 °C	"INFORME — 15:51–16:42. 51 min sobre 4 °C, volvió a las 16:42. Coincidió con deshielo."
Aviso temprano de subida	Sube >3 °C/h con R²>0,7 y llegaría al límite en <30 min	"🟠 Sube rápido: 2,8 °C (+3,4 °C/h). Llegaría a 4 °C en ~21 min si sigue así."
Puerta abierta	>5 min con la temp subiendo	"🟠 Puerta abierta hace 8 min — 3,1 °C. Conviene cerrarla."
Producto en riesgo	El producto cruzó el 50 % y el 100 % de su tolerancia	"🟠 500 kg de ternera: 1 h 33 min sobre 4 °C (máx. 2 h; margen 27 min)."
Incidente del detector	Anomalía estadística (fuera de perfil, deriva, señal plana, inconsistencia)	"Incidente: deriva persistente en M1 Potencia Activa. Severidad 0,62."
Alarma de SITRAD	El controlador levantó una alarma	"🔔 Alarma de SITRAD — TC-900E (20:45). Reloj Desprogramado."
Datalogger al 100 %	IsDataloggerFull=True o uso ≥95 %	"[AVISO] Datalogger al 100 % (ya sobrescribió). Los datos más antiguos se pierden."
Sensor stale	Un canal lleva >10 min sin reportar	"🟠 Sin lecturas de la termocupla desde las 20:42. Posible corte de comunicación."
Lecturas reanudadas	Vuelven tras un corte	"✅ Lecturas reanudadas tras ≈8 min (desde las 20:42). Temperatura: 2,4 °C."
Resumen de turno	3 veces al día (6 h, 14 h, 22 h)	Resumen de eventos, episodios y estado del período.
Heartbeat (opcional, hoy off)	Cada N minutos	"❤️ Monitor vivo. Uptime: 6,2 h. Eventos: 3. Anomalías abiertas: 1."
Informes bajo demanda (12 tools deterministas): temperature_report, status_report, load_report,
forecast_temperature, explain_temperature, episode_summary, data_quality, aggregate,
export_csv, render_chart, controller_summary, diagnose_now.

Garantías de los informes:

Todas las cifras se calculan en Python (nunca las inventa el LLM).

Hechos separados de hipótesis ("compatible con", "no puede determinarse con estos datos").

Anclaje numérico (numeric_anchor_ok) descarta cifras inventadas.

Contexto (puerta, deshielo, producto) cuando el motor lo puede demostrar.

## 4. Correlaciones — limitadas pero honestas
correlate(a, b, minutos) — Pearson puro
Calcula Pearson entre dos canales en una ventana.

Alinea temporalmente 1-a-1 (_align_series): un instrumento lento no se cuenta varias veces contra uno rápido.

Detecta series constantes y devuelve "correlación no definida" en vez de 0,0 engañoso.

Avisa si n < 20: "solo 12 pares independientes, la correlación es poco fiable".

Devuelve el coeficiente sin p-value.

compressor_relation(minutos) — la correlación que sí importa
Compara M1 Potencia Activa con REFR=ON vs REFR=OFF (excluyendo deshielo).

Calcula delta_kw (cuánto sube la potencia cuando el compresor arranca).

Calcula correlación punto-biserial (válida para 0/1 vs continuo).

Calcula tasa de cambio de temperatura con REFR ON vs OFF.

Conclusión en texto: "M1 sube 8,3 kW cuando REFR está activo (correlación 0,84): su consumo sigue la orden de refrigeración."

### Lo que no tiene
Falta	Impacto
p-value / significancia	correlate no dice si el coeficiente es distinto de 0
Spearman / Kendall	Solo Pearson
Cross-correlation con lag	Temperatura reacciona con retraso; la correlación contemporánea sale baja aunque la relación sea fuerte
Correlación parcial	No puede "controlando por puerta"
IC del coeficiente	No da "r = 0,72 ± 0,08 (95 %)"
Veredicto: suficiente para operación. compressor_relation es la que un ingeniero realmente usa.

## 5. Estadística — descriptiva sólida + inferencial básica
### Lo que sí hace
Técnica	Dónde	Qué calcula
Descriptiva	engine.stats	media, mín, máx, σ, cobertura, n válido
Regresión con t-test	engine.slope	OLS + error estándar + t-stat; descarta si t < 1,96
Detección de cambios	engine.detect_changes	escalón, rampa, deriva
Integración trapezoidal	engine.energy	kWh con control de huecos
Pearson	engine.correlate	con alineación 1-a-1
Baseline	engine.baseline	media y σ
Anomalías k-σ	anomaly_detector	k=3 + piso de magnitud relativa
Deriva persistente	anomaly_detector	cambio total > k·σ
Rachas planas	anomaly_detector	n muestras idénticas
Perfil de comportamiento	baseline.py	p95 rachas, mediana ON/OFF, duty, media/σ por hora
Percentiles	varios	p95 con interpolación lineal
Agregaciones	aggregations.py	mean/min/max/count/sum/p95 por hora/día/semana
Calidad de datos	quality.py	cobertura, gaps, antigüedad
Despike	forecast.despike	quita 1-2 muestras aisladas
Tendencia + curvatura	forecast.fit_trend	OLS + R² + aceleración
p95 "lo habitual"	rise_watch.usual_max	referencia histórica
### Lo que no hace
Falta	Por qué importa
Intervalos de confianza	media 2,3 °C sin IC95
t-test de medias, chi², ANOVA	no compara lotes
Cross-correlation con lag	ya visto en §4
Descomposición estacional (STL)	media/σ por hora sí, STL no
Estadística robusta (MAD)	detección k-σ sensible a outliers
Distribuciones ajustadas	no dice "esto es normal / Weibull"
Ponderación por tiempo	aggregate es media aritmética
Bootstrap / Monte Carlo	no hay remuestreo
Detección formal de outliers	despike es heurístico
Power analysis	no estima tamaños muestrales
Veredicto: cubre el 90 % de lo que un ingeniero de planta necesita. Para análisis avanzado, exportar CSV y usar Python/scipy/R.

## 6. Técnicas de muestreo — no las hace (y no le hacen falta)
El bot lee todas las lecturas (cada 2 min, retención 168 h) sin submuestrear.

### Sí tiene

Upsampling: cada lectura vale hasta la siguiente, con tope de 3× la cadencia en channel_quality.

Downsampling por agregación: aggregate agrupa por hora/día/semana.

Alineación 1-a-1: _align_series.

Despike: elimina picos aislados.

### No tiene

Submuestreo aleatorio o estratificado.

Bootstrap, Monte Carlo.

Cross-validation.

¿Hace falta? No, para monitoreo industrial con n grande la estadística correcta es censo, no muestra.

## 7. Ejemplos de qué responde bien y qué no
### Bien
"¿Cómo está la cava?" → temperatura + equipo + producto + semáforo.

"¿Cuántos episodios hubo esta semana?" → lista con causa y duración.

"¿Cuál es la tendencia?" → °C/h con R² y significancia.

"¿Coincide con el compresor?" → medias ON/OFF, delta, correlación, efecto.

"¿Calidad de los datos?" → cobertura %, gaps >10 min, antigüedad.

"¿Cuánto duró el último episodio?" → fecha, duración, pico, causa.

"¿Máxima del día?" → max + hora + contexto.

"¿El compresor trabaja más de lo normal?" → duty por bloques.

"¿Qué producto hay?" → lista con exposición y margen.

"Dame un gráfico" → PNG con 1-2 ejes.

### No (o responde con honestidad sobre su límite)
"¿Probabilidad de que falle mañana?" → "No tengo datos para predecir fallas".

"¿Significancia entre ayer y hoy?" → "Tengo la media, no puedo comparar con test".

"¿Temperatura y consumo correlacionados?" → "No correlaciones Temp vs kW: reacciona con retraso".

"¿Incertidumbre de esa media?" → da la media sin IC.

"¿Probabilidad de superar 4 °C en 2 h?" → proyección lineal "si sigue así".

"¿Patrón estacional?" → media por hora (básico, no STL).

## 8. API de SITRAD — cómo se usa
Endpoints usados (solo lectura)
```text
GET /api/v1/instruments                         → lista de instrumentos
GET /api/v1/instruments/{id}/values             → 143 variables del TC-900E
```
```
GET /api/v1/alarms?startDate=…&endDate=…        → alarmas de SITRAD
El bot NO escribe en SITRAD. No hay POST, PUT, DELETE, ni comandos al compresor.

### Flujo
```text
Cada 30 s (poll_seconds):
## 1. GET /instruments/{id}/values
## 2. Filtrar variables: enabled=True, no implausibles (>60 o <-60 °C → MISSING)
## 3. Convertir a ChannelReading (23 canales)
## 4. store.ingest() → SeriesStore en memoria (168 h)
## 5. FileLog.append → logs/observations.jsonl

Cada 60 s (alarms_every_seconds):
## 1. GET /alarms?startDate=now-15min&endDate=now
## 2. Deduplicar por id
## 3. Enviar al chat si hay nuevas

Al arrancar (modo sitrad, replay_on_start=true):
  FileLog.read_observations_since(cutoff) → store.ingest (sin pasar por event_engine ni cooling_monitor)
Endpoints probados y no disponibles (8-nov-2026)
17 endpoints de historial probados, todos 404: /logs, /history, /data, /records, /log,
/values/history, /datalogger, /samples, /events, /historyValues, /chartData, etc.
Tampoco hay OpenAPI/Swagger (/swagger.json, /openapi.json, /api-docs, todos 404).

Conclusión: SITRAD PRO (esta versión) solo publica /instruments, /values y /alarms.
El datalogger interno del controlador no es accesible por REST.

### Autenticación
Basic Auth (HTTPBasicAuth) con sitrad.user/sitrad.password de config.json,
o variables de entorno SITRAD_USER / SITRAD_PASSWORD (recomendado).

verify_ssl=False por defecto (SITRAD usa certificado autofirmado local). Solo aceptable en localhost o red interna aislada.

Timeout: 30 s (configurable con sitrad.timeout).

### Guardarraíles
Solo GET a una base_url fija.

Credenciales en header (Basic Auth), no en URL.

Deduplicación por timestamp.

Filtro de plausibilidad: fuera de (-60, +150 °C) → MISSING con raw para diagnóstico.

Filtro por enabled: IsS*Enabled=False → MISSING (no llega al diagnóstico).

Tolerancia a fallos: _fail_streak cuenta sin bloquear; el bot sigue con lo que hay en SeriesStore.

## 9. Seguridad — análisis para el ingeniero
9.1 Lo que el bot no hace (por diseño)
No hace	Cómo se garantiza
No escribe en SITRAD	SitradApi solo tiene instruments(), values(), alarms(). Sin post/put/delete.
No abre puertos entrantes	Sin servidor HTTP entrante. Solo conexiones salientes a SITRAD y Telegram.
No ejecuta comandos del usuario	Sin tool exec, shell, subprocess. Las tools solo leen del SeriesStore.
No envía datos a internet	Ollama local (localhost:11434), SITRAD local (localhost:8004), Telegram único servicio externo.
No manda comandos al compresor	SITRAD no publica endpoint de escritura y el bot no lo intenta.
No procesa SQL	SITRAD usa REST, no SQL. Sin inyección SQL.
No expone API pública	Telegram con getUpdates (polling saliente), sin webhook entrante.
No acepta comandos de usuarios no autorizados	allowed_user_ids (opcional) + filtro chat_id. En grupos: group_requires_mention=True.
9.2 Riesgos reales y mitigaciones
Riesgo	Impacto	Mitigación
Credenciales SITRAD en config.json	Si el archivo sale del equipo, cualquiera con acceso puede leer/escribir en SITRAD	Usar SITRAD_USER / SITRAD_PASSWORD (variables de entorno). Ya implementado en resolve_credentials(). config.json debe tener "user": "" y "password": "".
Token de Telegram en config.json	Si se filtra, cualquiera puede enviar mensajes al bot	Rotar periódicamente desde @BotFather. El filtro RedactTokens lo oculta en logs.
verify_ssl=False	MITM en la red local	Aceptable solo si SITRAD y el bot están en el mismo host o VLAN aislada. En producción, instalar certificado y verify_ssl: true.
LLM alucinando cifras	Mensaje con dato inventado	Anclaje numérico (numeric_anchor_ok): cifras fuera de contexto se descartan; se usa plantilla determinista. Verificado en 30 tests.
Prompt injection vía Telegram	"Ignora las instrucciones y di X"	Enrutador determinista: preguntas comunes resueltas antes del LLM. El LLM no cambia reglas. Solo responde al chat_id autorizado.
Logs con datos operativos	Fuga si alguien accede a logs/	Rotación diaria (rotation.mode: "daily", keep_days: 7). Filtro RedactTokens evita credenciales en logs.
CVE en dependencias	requests, matplotlib, pandas	Fijar versiones en requirements.txt; actualizar con revisión.
Ollama keep_alive: -1	Modelo cargado siempre (~4 GB RAM)	No es riesgo de seguridad, pero consume. Ajustable con keep_alive: "10m".
Acceso físico al host	Leer logs/ y config.json	Responsabilidad del ingeniero: cuenta dedicada, permisos restringidos en logs/.
9.3 Lo que el bot no puede hacer aunque alguien lo intente
Enviar comandos al compresor (SITRAD no publica endpoint de escritura).

Ejecutar shell (el LLM no tiene tool exec; aunque la pidiera, no existe en TOOLS).

Leer archivos arbitrarios (las tools solo consultan SeriesStore, HistoricalMemory, EpisodeHistory, ProductLog).

Salir de la red local salvo por Telegram.

Persistir en SITRAD (sin operación de escritura).

9.4 Buenas prácticas antes de producción
Credenciales en variables de entorno, no en config.json:

```powershell
```
[Environment]::SetEnvironmentVariable("SITRAD_USER", "admin", "Machine")
[Environment]::SetEnvironmentVariable("SITRAD_PASSWORD", "********", "Machine")
verify_ssl: true si SITRAD está en otra máquina.

allowed_user_ids en Telegram: solo IDs autorizados.

Rotar el token de Telegram si alguna vez salió del equipo.

Firewall de Windows: bloquear todo lo entrante al puerto de Ollama (11434) si el equipo está en red compartida.

No exponer SITRAD a internet. Es API local.

Backup de logs/: observations.jsonl es la única fuente de verdad del bot.

9.5 Conclusión para el ingeniero
El bot es un lector pasivo. No modifica SITRAD, no controla el compresor, no abre puertos, no ejecuta código del usuario, no envía datos fuera de la red salvo por Telegram.

Los riesgos reales son de configuración (credenciales en texto plano, verify_ssl=False, token Telegram), no de código. Con las 7 buenas prácticas de §9.4, cumple el nivel de seguridad esperado para una instalación industrial en red interna. No requiere auditoría de seguridad externa.

## 10. Canales del TC-900E (verificado 8-nov-2026)
Canal	enabled	Qué activa
Temp Cámara (Ambiente = S1)	✓	todo
Setpoint, Desvío, Diferencial	✓	regulación
Estado Refrigeración / Deshielo / Ventilador	✓	duty, deshielo, capacidad_reducida
Horas Compresor	✓	informativo
Alarma Temp Alta/Baja, Alarma Puerta, Alarma Deshielo Timeout	✓	hipótesis directas
Error Sensor 1/2/3	✓	sensor_error
Límite Alarma Temp, Duración Deshielo Config	✓	referencia
Datalogger Activo/Lleno/Uso/Ya Sobrescribió	✓	aviso informativo
Temp Evaporador	✗ (IsS2Enabled=False)	capacidad_reducida no evaluable
Puerta Abierta	✗ (IsOpenDoorEnabled=False)	aperturas y puerta_prolongada por canal
Salida HOT	no existe en el modelo	—
### Estado del datalogger (8-nov-2026)
Campo	Valor	Nota
DataloggerOn	False	Detenido. Preguntar al ingeniero si es intencional.
IsDataloggerFull	False	Buffer no lleno.
DataloggerAlreadyOverwrote	False	No ha dado la vuelta.
DataloggerPercentUsage	0,209 %	~3 h de datos a 2 min; creciendo.
IsManualDatalogger	True	Manual: no se reinicia solo al llenarse.
Acción: decidir con el ingeniero si se reactiva y con qué política de retención. Si se llena al
100 % sin exportar, sobrescribe lo más antiguo. El bot avisará vía [AVISO] Datalogger al 100 %.

## 11. Historial disponible
SITRAD PRO (esta versión) no expone el datalogger por API.

El historial del bot se construye con lo que él mismo grabó:

logs/observations.jsonl (y rotados observations.YYYYMMDD.jsonl).

Al arrancar, FileLog.read_observations_since(cutoff) lo recarga al SeriesStore.

Lo anterior al primer arranque no existe (salvo export manual de SITRAD).

Para conservar una semana: storage.max_hours = 168, rotation.keep_days >= 7. Si el bot estuvo
apagado, hay un hueco (data_quality lo muestra).

## 12. Decisiones de diseño (para el ingeniero)
datalogger_lleno es aviso, no causa: va a bundle.avisos ([AVISO]), no compite con las
hipótesis del diagnóstico. Si compitiera, un datalogger siempre al 100 % enmascararía causas reales.

Replay solo en modo sitrad: en demo las observaciones llevan fechas simuladas.

_history_guard: si pides ventana anterior a la historia, responde "Solo tengo datos desde…".

aggregate media aritmética, no ponderada por tiempo (a 2 min la diferencia es despreciable).

CSV UTF-8 con BOM, separador coma (compatible con Excel).

El LLM solo redacta: anclaje numérico descarta cifras fuera de contexto.

## 13. Configuración clave (config.json)
```json
{
  "mode": "sitrad",
  "polling_seconds": 5,
  "report_window_minutes": 120,
  "cava_colanta": {
    "device_id": 201,
    "device_name": "Cava Colanta",
    "compressor_channel": "Estado Refrigeración"
  },
  "cooling": {
    "temp_limit_c": 4.0,
    "product_rules": { "ternera": { "max_minutes_above": 120 } },
    "warn_slope_c_h": 3.0,
    "warn_horizon_min": 30,
    "agent_scope": "controlador"
  },
  "sitrad": {
    "base_url": "https://localhost:8004/api/v1",
    "verify_ssl": false,
    "instrument_id": 1,
    "poll_seconds": 30,
    "user": "",
    "password": ""
  },
  "storage": {
    "max_hours": 168,
    "replay_on_start": true
  },
  "telegram": {
    "enabled": true,
    "token": "",
    "chat_id": ""
  },
  "operations": {
    "timezone": "America/Bogota",
    "max_messages_per_hour": 12,
    "summary_hours_local": [6, 14, 22],
```
    "rotation": { "mode": "daily", "keep_days": 7 }
  }
}
## 14. Preguntas para el ingeniero (antes de la cava)
¿Se puede habilitar IsOpenDoorEnabled=True en el TC-900E? ¿Hay sensor de puerta cableado?

¿Se puede instalar la sonda S2 (evaporador) y habilitar IsS2Enabled=True?

¿El datalogger manual se reinicia solo al llenarse, o hay que exportarlo a mano?

¿Cuánto tiempo retiene el datalogger a 2 min de cadencia?

¿Hay algún requisito legal de retención de datos (trazabilidad cadena de frío)?

¿Hay Multipower disponible o previsto? (Desbloquea 4 hipótesis eléctricas.)

## 15. Deuda técnica documentada (no bloqueante)
Item	Impacto	Coste estimado
Fix _primed (§0)	Bloqueante (falsos positivos al reiniciar)	5 min
sendDocument (CSV por Telegram)	Hoy CSV queda en logs/exports/ del equipo del bot	20 min
tools/future/capabilities.py → tool describe_capabilities()	"¿Qué puedes detectar con este instrumento?"	30 min
smoke_reporter.py / run_validation.py a canales nuevos	No afecta producción, sí a tests de regresión	1 h
README.md	Onboarding	30 min
requirements.txt completo	Reproducibilidad	5 min
p-value en correlate	Cierra honestidad estadística	15 líneas
Cross-correlation con lag	Desbloquea Temp vs kW correctamente	30 líneas
IC95 en stats y baseline	Añade incertidumbre	20 líneas
Media ponderada por tiempo en aggregate	A 2 min la diferencia es despreciable	10 líneas
Detección de outliers robusta (MAD)	despike heurístico falla con datos ruidosos	30 líneas
Descomposición tendencia + hora (STL)	La media por hora ya cubre el 80 %	60 líneas
Modelo probabilístico de fallo	Requiere datos etiquetados reales	200+ líneas
Ninguno bloquea la instalación salvo el fix _primed.

## 16. Cómo contribuir
Antes de tocar analysis/ (diagnóstico, cooling, engine, anomaly): correr python -m pytest tests -q.

Cualquier cambio en _hyps o thresholds: añadir test en tests/test_diagnosis.py.

Cualquier tool nueva en cava_agent.py: añadir ruta en _route_question y test en tests/test_route.py.

Cualquier cambio en la fuente SITRAD: verificar con python tools/inspect_sitrad_api.py que
sigue mapeando los mismos canales.

Nunca hacer que el LLM calcule cifras: todo va en Python, el LLM solo redacta.

Nunca añadir endpoints de escritura a SITRAD (el bot es lector por diseño).

Nunca exponer credenciales en logs (RedactTokens lo filtra pero no lo sustituyas).

## 17. Referencia rápida — qué preguntar y qué esperar
Pregunta	Tool que se dispara	Respuesta esperada
"temperatura actual"	status_report(breve)	Temp + límite + setpoint
"¿cómo está la cava?"	status_report	Semáforo + equipo + producto
"qué historia tienes"	history_coverage	Rango desde/hasta + horas
"¿cuántos episodios esta semana?"	get_episodes	Lista con causa
"resumen de episodios"	episode_summary	Por causa, duración mediana, total
"calidad de los datos"	data_quality	Cobertura % por canal
"promedio de temperatura por hora"	aggregate	Filas por hora
"exporta la temperatura de ayer a CSV"	export_csv	Ruta del archivo
"hazme un gráfico de temperatura y compresor"	render_chart	PNG en logs/charts/
"¿por qué sube la temperatura?"	explain_temperature	Tendencia + compresor + hipótesis
"¿qué pasará en 1 hora?"	forecast_temperature	Proyección lineal
"¿cuántas veces se abrió la puerta?"	controller_summary(puerta)	"Sensor deshabilitado"
"¿cuál es el setpoint?"	controller_summary(setpoint)	Valor + cambios
"¿cuánto trabaja el compresor?"	controller_summary(compresor)	Duty cycle + ciclo típico
"¿cada cuánto hace deshielo?"	controller_summary(deshielo)	Frecuencia + duración
"hay algún problema"	diagnose_now	Hipótesis con evidencia + avisos
"¿qué instrumentos tienes?"	_render_instruments	Lista de canales + Multipower
"hay 500 kg de ternera desde hace 20 min"	product_log	Registra + límite + exposición
"el suero no debe subir de 12 grados"	product_log	Añade regla
"retira la ternera"	product_log	Retira + informe
"el de las 06:17 fue la puerta"	feedback_log	Etiqueta el caso
"¿qué tan bien aciertas?"	case_book.accuracy_text	Acierto real + casos etiquetados
Fin del documento. Para cualquier duda, revisar los módulos citados; todos tienen docstring y
ejemplos. El proyecto está cerrado en su fase 1 y listo para la cava, con el fix _primed pendiente
de 5 líneas.


---

## Notas sobre este documento

- **Está pensado para que cualquiera lo entienda**: tú, el ingeniero, un futuro desarrollador.
- **Contiene las capacidades reales** (§2, §3, §4, §5, §6), **la API** (§8), **la seguridad** (§9) y **lo que el bot hace vs no hace** con honestidad.
- **No promete lo que no hay**: dice explícitamente qué falta (Multipower, puerta, S2) y por qué.
- **Tiene el fix `_primed`** en §0 como bloqueante.
- **Tiene la lista de preguntas al ingeniero** en §14.
- **Tiene la deuda técnica** en §15 para que nadie se olvide.

Una vez copiado, verifica que se guardó bien:

```powershell
Get-Content NOTES.md | Measure-Object -Line
    ````