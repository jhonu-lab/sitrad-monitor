# Arquitectura del Monitor Sitrad PRO

## 1. Visión general

Sistema de monitoreo en tiempo real para controladores e instrumentos Full Gauge (PCT-3001 plus, MT-512E Log, RCK-862 plus, etc.) gestionados por **Sitrad PRO** en un servidor local.

El sistema se ejecuta como **daemon** y realiza **polling** a la API REST de Sitrad PRO, evalúa reglas deterministas y despacha alertas.

---

## 2. Diagrama de flujo de datos

+-----------------------------------------------------------------------+
  |                          FUENTES DE DATOS                             |
  |  [DEMO] simulator/fake_sitrad_api.py   [PROD] Sitrad PRO (Servidor)   |
  |         (Flask :8001)                         (HTTPS :8001)           |
  +-----------------------------------++----------------------------------+
                                      ||
                                      || HTTP / JSON (Polling)
                                      \/
  +-----------------------------------------------------------------------+
  |                           INGESTA Y CLIENTE                           |
  |                        core/sitrad_client.py                          |
  +-----------------------------------||----------------------------------+
                                      || SensorReading[]
                                      \/
  +-----------------------------------------------------------------------+
  |                         MOTOR DE REGLAS                               |
  |                        core/rule_engine.py                            |
  |  • R1 Sensor Error ("Err")             • R3 Out-of-Range             |
  |  • R2 Device Offline (null)            • R4 Critico                   |
  |-----------------------------------||----------------------------------|
                                      || List[Alert]
                                      \/
  |                    FILTRADO Y DEDUPLICACIÓN                           |
  |                       core/deduplication.py                           |
  |  • Histéresis N lecturas   • Cooldown   • Detección de recuperación   |
  +-----------------------------------||----------------------------------+
                                      || Alertas Confirmadas
             +------------------------++------------------------+
             |                        |                        |
             \/                       \/                       \/
  +--------------------+   +--------------------+   +--------------------+
  | notifiers/console  |   | notifiers/telegram |   | notifiers/file_log |
  | (Salida Terminal)  |   | (Bot API Telegram) |   | (logs/alerts.jsonl)|
  +--------------------+   +--------------------+   +--------------------+
                                      |
                                      \/
                           +--------------------+
                           | notifiers/callback |
                           | (Dashboards/Tests) |
                           +--------------------+
---

## 3. Principios de diseño

### 3.1. Inversión de dependencias
El `SitradClient` recibe una `base_url` en el constructor. No sabe si detrás hay un Flask de prueba o el Sitrad PRO real. El código que se ejecuta en la demo es el mismo que se ejecutará en producción.

### 3.2. Determinismo sobre heurística
El motor de reglas es explícito: cada alerta es trazable a una regla concreta. Auditable. La IA/ML se reserva para una fase futura de diagnóstico contextual (ver §7).

### 3.3. Idempotencia de alertas
Una falla persistente **no genera spam**. El `DeduplicationManager` mantiene estado por clave `(device_id, sensor, tipo_alerta)` y aplica histéresis temporal + cooldown + detección de recuperación.

### 3.4. Tolerancia a fallos
* Reintentos HTTP con backoff exponencial.
* Fail-fast inicial en producción.
* Cierre *graceful* con `SIGTERM`/`SIGINT`.
* Aislamiento por ciclo: una excepción no aborta el sistema.

---

## 4. Componentes

### 4.1. `simulator/`
| Archivo | Responsabilidad |
|---------|-----------------|
| `fake_sitrad_api.py` | Servidor Flask que replica endpoints de Sitrad PRO |
| `scenarios.py` | Guion de la demo por tick |
| `device_factory.py` | Fábrica de payloads JSON |

### 4.2. `core/`
| Archivo | Responsabilidad |
|---------|-----------------|
| `models.py` | Dataclasses `SensorReading`, `Alert`, enum `Severity` |
| `sitrad_client.py` | Cliente HTTP con reintentos |
| `rule_engine.py` | Reglas R1-R4 $\rightarrow$ `List[Alert]` |
| `deduplication.py` | Cooldown + histéresis + recuperación |
| `worker.py` | Orquesta el ciclo completo |

### 4.3. `notifiers/`
| Archivo | Responsabilidad |
|---------|-----------------|
| `base.py` | Interfaz `BaseNotifier` |
| `console.py` | Salida ANSI con colores |
| `file_log.py` | JSON Lines a `logs/alerts.jsonl` |
| `telegram.py` | Bot API de Telegram |
| `callback.py` | Redirige a un callback (dashboards, tests) |

### 4.4. `dashboard/`
| Archivo | Responsabilidad |
|---------|-----------------|
| `live_view.py` | Dashboard terminal con `rich` |

---

## 5. Contrato de datos (JSON de Sitrad PRO)

### 5.1. `GET /api/instruments`
```json
{
  "instruments": [
    { "id": 101, "name": "Compresor Cámara 1", "model": "PCT-3001 plus" },
    { "id": 102, "name": "Cámara Congelados",  "model": "MT-512E Log" },
    { "id": 103, "name": "Rack Refrigeración", "model": "RCK-862 plus" }
  ]
}
```

### 5.2. `GET /api/instruments/{id}/values`
```json
{
  "id": 102,
  "name": "Cámara Congelados",
  "model": "MT-512E Log",
  "status": "Online",
  "sensors": [
    { "name": "Temp Ambiente",   "value": -18.2,  "unit": "°C", "setpoint": -18.0, "hysteresis": 2.0 },
    { "name": "Temp Evaporador", "value": "Err",  "unit": "°C", "setpoint": -22.0, "hysteresis": 3.0 }
  ]
}
```

`value` puede ser:
* **Número** $\rightarrow$ lectura normal.
* **String `"Err"`** $\rightarrow$ sensor con error.
* **`null`** $\rightarrow$ sin lectura (device offline).

---

## 6. Flujo temporal de la demo

| Tick | Escenario | Alerta esperada |
|------|-----------|-----------------|
| 0-4 | Todo normal | (ninguna) |
| 5-9 | MT-512E sensor Evaporador = `"Err"` | 🔴 SENSOR_ERROR |
| 10-14 | PCT-3001 = Offline | 🔴 OFFLINE |
| 15-19 | RCK-862 presión 95 psi (SP=70 $\pm$ 3) | 🔴 OUT_OF_RANGE |
| 20+ | Todo recuperado | 🟢 RECUPERACIÓN (x3) |

---

## 7. Fase futura: módulo de IA/ML (opcional)

El motor determinista cubre el 100% de los casos actuales. Un módulo complementario podría:
* Filtrar falsos positivos aprendiendo el ruido de cada sensor.
* Correlacionar sensores para inferir causa raíz.
* Agrupar alertas relacionadas en un solo incidente.

Se ejecutaría como *sidecar* paralelo, marcando sus salidas como **"sugerencias"**, nunca como detección primaria.

---

## 8. Decisiones técnicas

| Decisión | Alternativa | Motivo |
|----------|-------------|--------|
| **API REST de Sitrad** | Modbus TCP directo | Sitrad ya abstrae RS-485 |
| **REST vs BD local** | Lectura directa de BD | BD no documentada |
| **Python** | Node.js | Mejor ecosistema IoT industrial |
| **requests directo** | python-telegram-bot | Menos dependencias |
| **Motor determinista** | ML desde el inicio | Latencia, auditabilidad |

---

## 9. Métricas de calidad

* **Latencia de detección:** $\le$ intervalo de polling + $< 100\text{ ms}$ evaluación.
* **Falsos positivos:** Mitigados por histéresis configurable.
* **Spam:** Máximo 1 alerta por clave por ventana de *cooldown*.
* **Recuperación:** Notificada automáticamente.
* **Persistencia:** `logs/alerts.jsonl` para auditoría.


---

## Verificación de Coherencia

Todos los archivos ahora:

1. **`worker.py`** usa `BaseNotifier` (contrato) y expone `last_snapshot` y `last_new_alerts` para el dashboard.
2. **`scenarios.py`** usa `DeviceFactory` (no construye dicts a mano).
3. **`console.py` / `file_log.py` / `telegram.py` / `callback.py`** heredan de `BaseNotifier`.
4. **`run_demo.py`** usa `LiveDashboard` + `CallbackNotifier` + `FileLogNotifier` (sin `ConsoleNotifier` para no ensuciar la pantalla del dashboard).
5. **`run_production.py`** usa `ConsoleNotifier` + `FileLogNotifier` + opcionalmente `TelegramNotifier`.
6. **`deduplication.py`** expone `refresh_absent()` que limpia contadores y devuelve claves ausentes (candidatas a recuperación).
7. **`live_view.py`** acepta `bump_counter(severity)` y `bump_recovery()` desde callbacks externos.
8. **`README.txt`** refleja la estructura final real.
9. **`ARCHITECTURE.md`** refleja los componentes reales.

### Cómo probar que todo funciona

```bash
# 1. Crear la estructura de carpetas
mkdir -p sitrad-monitor/{simulator,core,notifiers,dashboard,logs}
cd sitrad-monitor

# 2. Crear los 4 __init__.py vacíos
touch simulator/__init__.py core/__init__.py notifiers/__init__.py dashboard/__init__.py

# 3. Pegar todos los archivos de arriba en sus rutas

# 4. Instalar dependencias
pip install -r requirements.txt

# 5. Ejecutar la demo
python run_demo.py