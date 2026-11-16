# Para entregar cuando haya entorno
- [ ] Salida de `python tools/inspect_sitrad_api.py` (≥ 8 canales, incluye Evaporador y Salida FANS).
- [ ] Captura Telegram: «hazme un gráfico de las últimas 6 horas» → PNG + caption (requiere pegar `send_photo`).
- [ ] Captura: «¿qué instrumentos tienes?» → solo controlador; «¿cuánta energía se consumió?» → sin medidor.
- [ ] Captura: «¿por qué sube la temperatura?» → causa con evidencia, sin URLs.
- [ ] `python -m pytest tests -q` → todo verde.
- [ ] `python tools/scan_unused.py --entry run_reporter` → 0 huérfanos.
- [ ] recall/fpr de `run_validation.py --days 2` (recall ≥ 0.7, fpr ≤ 0.2) — herramienta aún por escribir.
- [ ] Duplicados: `Get-ChildItem -Recurse -Filter *.py | Group-Object Name | Where-Object Count -gt 1`
