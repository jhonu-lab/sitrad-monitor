
"""
tools/inspect_sitrad_api.py — qué devuelve SITRAD y a qué canal del bot se mapea cada variable.

    python tools/inspect_sitrad_api.py [--config config.json]

Credenciales: config.json (sitrad.user/password), o SITRAD_USER / SITRAD_PASSWORD, o las pregunta por consola.
Salida: una línea ✓ por canal válido, ⚠ por canal descartado,
✗ por variable sin canal y el estado de los sensores (IsS*Enabled).
"""
from __future__ import annotations

import argparse
from email.mime import base
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pandas import api

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.channel_catalog import CONTROLLER_SPECS
from sources.sitrad_api import (
    SENSOR_INDEX,
    TEMP_PLAUSIBLE,
    SitradApi,
    iter_variables,
    map_variables,
    resolve_credentials,
    sensor_enabled_flags,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    sc = cfg.get("sitrad", {})

    user, pwd = resolve_credentials(sc)

    api = SitradApi(
        sc.get("base_url", "https://localhost:8004/api/v1"),
        user,
        pwd,
        verify_ssl=sc.get("verify_ssl", False),
        timeout=sc.get("timeout", 30),
    )

    iid = sc.get("instrument_id")
    insts = api.instruments()

    print(
        "Instrumentos:",
        "; ".join(
            f"{i.get('id')}='{i.get('name', '?')}'"
            for i in insts
        ),
    )

    if iid is None:
        print("Define sitrad.instrument_id en config.json para ver sus variables.")
        return 1

    vs = list(iter_variables(api.values(iid)))
    mapped = map_variables(vs, sc.get("channel_map"))

    by_var = {id(v): ch for ch, v in mapped.items()}

    # Obtener el estado de los sensores antes de evaluar los canales.
    flags = sensor_enabled_flags(vs)

    print(
        f"\nInstrumento {iid}: "
        f"{len(vs)} variables, "
        f"{len(mapped)} canales mapeados\n"
    )

    # Mostrar los canales mapeados y señalar los que el bot descarta.
    for ch, v in sorted(mapped.items()):
        idx = SENSOR_INDEX.get(ch)

        sitrad_disabled = not v.enabled
        sensor_index_disabled = idx is not None and flags.get(idx) is False
        implausible = (
            v.unit == "°C"
            and isinstance(v.value, (int, float))
            and not (TEMP_PLAUSIBLE[0] <= v.value <= TEMP_PLAUSIBLE[1])
        )

        if sitrad_disabled:
            print(f"⚠ {v.label} -> {ch}   (valor={v.value!r})   [deshabilitado en SITRAD (enabled=False): el bot descarta]")
        elif sensor_index_disabled:
            print(f"⚠ {v.label} -> {ch}   (valor={v.value!r})   [S{idx} deshabilitado: el bot descarta]")
        elif implausible:
            print(f"⚠ {v.label} -> {ch}   (valor={v.value!r})   [fuera de rango físico: el bot descarta]")
        else:
            print(f"✓ {v.label} -> {ch}   (valor={v.value!r})")

    # Identificar canales del controlador que no tienen variable asignada.
    missing = [
        s.name
        for s in CONTROLLER_SPECS
        if s.name not in mapped
    ]

    for m in missing:
        print(f"✗ canal sin variable: {m}")

    # Mostrar el estado de habilitación de los sensores.
    print(
        "\nSensores habilitados (IsS*Enabled):",
        flags or "no se encontraron esas variables",
    )

    for ch, idx in SENSOR_INDEX.items():
        if flags.get(idx) is False:
            print(
                f"⚠ {ch}: sensor S{idx} deshabilitado, "
                "sus lecturas se descartan"
            )

    # Mostrar las variables que no se asociaron a ningún canal.
    others = [
        v.label
        for v in vs
        if id(v) not in by_var
    ]

    print(
        f"\nVariables sin canal ({len(others)}): "
        f"{', '.join(others[:40])}"
        f"{' …' if len(others) > 40 else ''}"
    )

    # Calcular los canales descartados por el bot.
    descartados = [
    ch
    for ch, v in mapped.items()
    if (not v.enabled)
    or (SENSOR_INDEX.get(ch) is not None and flags.get(SENSOR_INDEX[ch]) is False)
    or (v.unit == "°C" and isinstance(v.value, (int, float))
        and not (TEMP_PLAUSIBLE[0] <= v.value <= TEMP_PLAUSIBLE[1]))
]

    # Calcular los canales efectivos.
    efectivos = len(mapped) - len(descartados)

    print(
        f"\nResumen: {efectivos} canales efectivos "
        f"de {len(CONTROLLER_SPECS)} del controlador "
        f"({len(descartados)} mapeados pero descartados: "
        f"{', '.join(descartados) or '-'})."
    )

    candidatos = [
        f"/instruments/{iid}/logs",
        f"/instruments/{iid}/history",
        f"/instruments/{iid}/data",
        f"/instruments/{iid}/records",
        f"/instruments/{iid}/log",
        f"/instruments/{iid}/values/history",
    ]
    start = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("\n== Sondeo de endpoints de historial ==")
    for path in candidatos:
        try:
            r = api.session.get(
                f"{api.base_url}{path}",
                params={"startDate": start, "endDate": end},
                timeout=10,
            )
            print(f"  {r.status_code}  {path}")
            if r.ok:
                data = r.json()
                print(f"    → keys: {list(data)[:6]}, results len: {len(data.get('results', []))}")
        except Exception as exc:
            print(f"  ERR {path}: {exc}")

            # --- ronda 2: swagger + datalogger + eventos ---
    print("\n== Descubrimiento de la API ==")
    for path in ("/swagger.json", "/openapi.json", "/swagger",
                "/api-docs", "/api/v1/swagger.json", "/api/v1/openapi.json"):
        try:
            base = api.base_url[:-len("/api/v1")] if api.base_url.endswith("/api/v1") else api.base_url
            r = api.session.get(f"{base}{path}", timeout=5)
            print(f"  {r.status_code}  {path}")
            if r.ok and "json" in r.headers.get("content-type", ""):
                spec = r.json()
                print(f"    rutas declaradas: {sorted((spec.get('paths') or {}).keys())[:20]}")
                break
        except Exception as exc:
            print(f"  ERR {path}: {exc}")

    print("\n== Sondas SITRAD (datalogger/eventos) ==")
    candidatos2 = [
        "/instruments/1/datalogger",
        "/instruments/1/samples",
        "/instruments/1/events",
        "/instruments/1/historyValues",
        "/instruments/1/chartData",
        "/instruments/1/values/log",
        "/logs",
        "/events",
        "/history",
        "/values/history",
        "/datalogger/1",
    ]
    params = {"startDate": start, "endDate": end}
    for path in candidatos2:
        try:
            r = api.session.get(f"{api.base_url}{path}", params=params, timeout=10)
            extra = ""
            if r.ok:
                try:
                    d = r.json()
                    rs = d.get("results") or []
                    extra = f"  results={len(rs)}  keys={sorted(d)[:6]}"
                except Exception:
                    pass
            print(f"  {r.status_code}  {path}{extra}")
        except Exception as exc:
            print(f"  ERR {path}: {exc}")
    # --- árbol de convertidores → instrumentos → canales (si el endpoint /converters existe) ---
    try:
        from sources.sitrad_api import SitradApiSource
        src = SitradApiSource(api, device_id=1, channel_map=sc.get("channel_map"))
        tree = src.discover_all()
        print("\n== ÁRBOL SITRAD ==")
        print(f"Convertidores: {tree['n_converters']} · Instrumentos: {tree['n_instruments']} · "
            f"Canales efectivos: {tree['n_channels_effective']}/{tree['n_channels_total']}")
        for c in tree["converters"]:
            print(f"\nConvertidor {c['name']} (id {c['id']}, {c['status'] or '?'}):")
            for ins in c["instruments"]:
                print(f"  · {ins['name']} (id {ins['id']}) — "
                    f"{ins['n_channels_effective']}/{ins['n_channels_total']} canales efectivos")
                for ch in ins["channels"]:
                    mark = "✓" if ch["accepted"] else "⚠"
                    extra = "" if ch["accepted"] else f"  [{ch['reason']}]"
                    print(f"      {mark} {ch['canal']:<22} = {ch['value']!r:<18} {ch['unit'] or ''}{extra}")
        if tree["unassigned_instruments"]:
            print(f"\nSin convertidor: {len(tree['unassigned_instruments'])} instrumento(s)")
            for ins in tree["unassigned_instruments"]:
                print(f"  · {ins['name']} (id {ins['id']}) — "
                    f"{ins['n_channels_effective']}/{ins['n_channels_total']} canales")
    except Exception as exc:
        print(f"\n(no pude construir el árbol SITRAD: {exc})")
    return 0 if not missing else 2


if __name__ == "__main__":
    sys.exit(main())
