# Bot de monitoreo SITRAD — contexto para revisión

Monitoreo de cavas de refrigeración (TC-900E) y demás instrumentos de SITRAD PRO
(Multipower, hornos, calderas...). Lee todos los canales, los agrupa por **lugar**,
procesa con código determinista en Python, redacta con un LLM local (Ollama) y
avisa por Telegram.

**No escribe en SITRAD. No controla ningún equipo. No abre puertos.**
**El LLM solo redacta: toda cifra la calcula Python.**

## Estado actual (noviembre 2026)
- 70 tests verdes (`python -m pytest tests -q`), 0 módulos huérfanos.
- Modo `sitrad` probado contra `tools/mock_sitrad_server.py`; modo `demo` funcionando.
- Multi-instrumento activado por defecto, con lugares (`locations` en el config).
- Preguntas y redacción del LLM en un hilo aparte; el ciclo principal no se bloquea.
- Respuestas al chat que preguntó; alertas a todos los chats configurados.
- Rotación diaria de observaciones y limpieza programada de datos (`storage.retention`).

## Bugs del listado anterior
| # | Bug | Estado |
|---|-----|--------|
| 1 | Demo roto | Resuelto (era `self._dev`, no el simulador) |
| 2 | Telegram truncaba a 4000 | Resuelto (divide en partes) |
| 3 | Reintentos al chat por defecto | Resuelto (respuesta y reintento al chat de origen) |
| 4 | Sesión HTTP compartida entre hilos | Resuelto (sesión por hilo; `_drain_backlog` arreglado) |
| 5 | Lecturas duplicadas al reiniciar | Resuelto (`SeriesStore` descarta `<=`) |
| 6 | Rotación diaria de logs | Resuelto (rota al cambiar el día, también tras reiniciar) |
| 7 | Ruta de `alarm_history` | Resuelto (fuente y agente usan `sitrad_data/` junto al programa) |

## Pendiente conocido
- `products.json` es global (no hay cargas de producto por lugar).
- `new_alarms` consulta una ventana fija de 15 min (se decidió no tocar).

## Orden de lectura
`README.md` → `CHANGELOG.md` → `run_reporter.py` → `core/reporter.py` →
`sources/sitrad_api.py` (lugares) → `agent/cava_agent.py`.
