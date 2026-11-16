"""
simulation/jsonl_replay.py — repite las capturas de tu extractor (sitrad_data/lecturas.jsonl) como si fueran en vivo.

Cada línea del archivo es {"collectedAt": ..., "instruments": [{"instrument": {...}, "values": [...]}]}. Se usa la
misma conversión de variables que la API en vivo (sources/sitrad_api.py), así que sirve también para comprobar con
datos REALES que los nombres de variables de tu servidor se reconocen.
"""
from __future__ import annotations

import json
import logging
from datetime import timezone

from core.sitrad_mapping import key, parse_datetime_any, parse_number, parse_state
from core.channel_catalog import CONTROLLER_SPECS
from simulation.excel_replay import RowsReplaySource
from sources.sitrad_api import iter_variables, map_variables

logger = logging.getLogger(__name__)


def load_lecturas(path: str, instrument_id=None, instrument_name=None, channel_map=None):
    spec = {s.name: s for s in CONTROLLER_SPECS}
    rows, bad, chosen, seen_names = [], 0, None, {}
    last_t = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                snap = json.loads(line)
                collected = parse_datetime_any(snap.get("collectedAt"))
                for entry in snap.get("instruments", []):
                    ins = entry.get("instrument") or {}
                    if "values" not in entry:
                        continue
                    seen_names[str(ins.get("id"))] = ins.get("name")
                    vs = list(iter_variables(entry["values"]))
                    mapped = map_variables(vs, channel_map)
                    if instrument_id is not None and str(ins.get("id")) != str(instrument_id):
                        continue
                    if instrument_name and key(instrument_name) not in key(ins.get("name", "")):
                        continue
                    if chosen is None and len(mapped) >= 3:
                        chosen = str(ins.get("id"))
                    if chosen is not None and str(ins.get("id")) != chosen:
                        continue
                    if len(mapped) < 3:
                        continue
                    ts = [v.ts for v in mapped.values() if v.ts is not None]
                    t = max(ts) if ts else collected
                    if t is None or (last_t is not None and t <= last_t):
                        continue                                  # el instrumento no actualizó: fila repetida
                    vals = {}
                    for ch, v in mapped.items():
                        if v.enabled and not v.error:
                            vals[ch] = parse_state(v.value) if spec[ch].kind == "state" else parse_number(v.value)
                        else:
                            vals[ch] = None
                    rows.append((t.astimezone(timezone.utc), vals))
                    last_t = t
            except Exception:
                bad += 1
    if not rows:
        raise ValueError("No encontré lecturas del controlador en " + path + ". Instrumentos vistos: "
                         + str(seen_names) + ". Corre tools/inspect_sitrad_api.py o indica instrument_id/channel_map.")
    steps = sorted((b[0] - a[0]).total_seconds() for a, b in zip(rows, rows[1:]))
    info = {"filas": len(rows), "filas_sin_fecha": bad, "orden_fecha": "iso", "instrumento": chosen,
            "primera": rows[0][0], "ultima": rows[-1][0], "paso_mediano_s": steps[len(steps) // 2] if steps else None}
    return rows, info


class JsonlReplaySource(RowsReplaySource):
    def __init__(self, path: str, device_id: int, instrument_id=None, instrument_name=None, channel_map=None,
                 stop_at_end: bool = True, end_grace_polls: int = 4) -> None:
        rows, info = load_lecturas(path, instrument_id, instrument_name, channel_map)
        super().__init__(rows, info, device_id, stop_at_end, end_grace_polls)
