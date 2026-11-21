# Bot de cava — contexto para revisión

Sistema de monitoreo de una cava de refrigeración controlada por un TC-900E
a través de SITRAD PRO. Lee 137 canales (23 catalogados + 116 passthrough),
procesa con código determinista en Python, redacta con un LLM local (Ollama)
y manda mensajes por Telegram.

**No escribe en SITRAD. No controla el compresor. No abre puertos.**
**El LLM solo redacta: toda cifra la calcula Python.**

## Estado actual
- 30 tests verdes.
- Descubrimiento de convertidores/instrumentos/canales funciona.
- Passthrough: lee los 137 canales del TC-900E.
- El modo demo está roto (bug del simulador, se está arreglando).

## Bugs en curso
1. Demo mode roto: `simulation/cava_random.py` no emite los canales nuevos.
2. Telegram trunca mensajes largos a 4000 chars sin avisar.
3. Reintentos van al chat por defecto, no al original.
4. Sesión HTTP compartida entre hilos (race condition).
5. Lecturas duplicadas al reiniciar (`SeriesStore` acepta `==` timestamp).
6. Rotación diaria de logs no funciona al arrancar otro día.
7. `alarm_history` lee `logs/` pero `_save_alarm` escribe en `sitrad_data/`.

## Orden de lectura
Bloques 1-16: entender. Bloques 17-20: bugs.