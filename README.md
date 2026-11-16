# Bot de cava (TC-900E + SITRAD)

## Capas
```
SITRAD API / simulador ──► sources/  ──► core/SeriesStore
                                            │
        analysis/ (eventos, anomalías, diagnosis, cooling_report, rise_watch)   ← determinista
                                            │
        agent/cava_agent.py (herramientas + enrutador; el LLM solo redacta)
                                            │
        core/reporter.py ──► notifiers/ (Telegram / consola)
```
## Modos
`config.json → mode`: `sitrad` (API local en vivo) o demo (simulador `cava_random`).

## Canales del TC-900E (9)
Temp Cámara (sonda S1, «Ambiente»), Setpoint, Desvío vs Setpoint, Estado Refrigeración, Estado Deshielo,
Puerta Abierta, Salida HOT, Temp Evaporador, Estado Ventilador. No hay Multipower en esta instalación.

## Arranque
```
pip install -r requirements.txt
set SITRAD_USER=...   set SITRAD_PASSWORD=...
python run_reporter.py
```
