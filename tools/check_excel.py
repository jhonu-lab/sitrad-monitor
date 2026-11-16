"""
tools/check_excel.py — ¿el bot entiende tu exportación de SITRAD?

    python tools/check_excel.py ruta/al/historial.xlsx [--tz America/Bogota] [--date-order auto|mdy|dmy]

Imprime lo que detectó: columnas reconocidas e ignoradas, orden de fecha, primera y última fila, paso entre
filas, huecos, y los valores de texto de los estados. Si algo no cuadra con lo que esperas (p. ej. fechas),
pega esta salida y se ajusta.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")
from simulation.excel_replay import load_sitrad_table


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tz", default="America/Bogota")
    ap.add_argument("--date-order", default="auto", choices=["auto", "mdy", "dmy"])
    a = ap.parse_args()
    rows, info = load_sitrad_table(a.path, a.tz, a.date_order)
    f = "%d/%m/%Y %H:%M:%S"
    print(f"Filas: {info['filas']} (sin fecha válida: {info['filas_sin_fecha']})")
    print(f"Orden de fecha elegido: {info['orden_fecha']}  (mdy = mes/día/año, dmy = día/mes/año)")
    print(f"Primera: {info['primera']:{f}}   Última: {info['ultima']:{f}}   "
          f"Duración: {(info['ultima'] - info['primera']).total_seconds() / 86400:.2f} días")
    print(f"Paso mediano entre filas: {info['paso_mediano_s']} s · huecos > 10 min: {info['huecos_mayores_10min']}")
    print("\nColumnas reconocidas (canal del bot <- columna del archivo):")
    for ch, col in info["columnas_usadas"].items():
        miss = info["sin_valor"].get(ch, 0)
        print(f"  {ch:22s} <- {col!r}" + (f"   ⚠️ {miss} filas sin valor" if miss else ""))
    if info["columnas_ignoradas"]:
        print("Columnas ignoradas:", ", ".join(repr(c) for c in info["columnas_ignoradas"]))
    print("\nValores vistos en los estados (texto original):")
    for ch, vals in info["estados_vistos"].items():
        print(f"  {ch:22s} {vals}")
    temp = [v["Temp Cámara"] for _, v in rows if v.get("Temp Cámara") is not None]
    if temp:
        print(f"\nTemp Cámara: mín {min(temp):.1f} · máx {max(temp):.1f} · promedio {sum(temp) / len(temp):.1f} °C")
    print("\nPrimeras 3 filas:")
    for t, v in rows[:3]:
        print("  ", t.astimezone(info['primera'].tzinfo).strftime(f), {k: v[k] for k in list(v)[:8]})


if __name__ == "__main__":
    main()
