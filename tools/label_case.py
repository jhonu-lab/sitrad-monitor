"""
tools/label_case.py — registrar lo que REALMENTE pasó en un caso.

    python tools/label_case.py --list
    python tools/label_case.py "<case_id>" <causa_real> [--nota "texto"]

`causa_real` puede ser una de las hipótesis (compresor_sin_arranque,
fallo_refrigeracion, puerta_prolongada, baja_tension, deshielo_extendido,
sensor_congelado, sin_comunicacion), otra causa en texto libre, o
`sin_falla` si fue una falsa alarma.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("case_id", nargs="?")
    ap.add_argument("real_cause", nargs="?")
    ap.add_argument("--nota", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--cases", default="logs/cases.jsonl")
    ap.add_argument("--labels", default="logs/case_labels.jsonl")
    a = ap.parse_args()

    if a.list or not a.case_id:
        seen = {}
        if os.path.exists(a.cases):
            for line in open(a.cases, encoding="utf-8"):
                if line.strip():
                    r = json.loads(line)
                    seen.setdefault(r["case_id"], r)
        for cid, r in seen.items():
            print(f"{cid}\n    dijo: {r['principal']} ({r['confianza']:.2f})  "
                  f"canales: {', '.join(r['channels'][:3])}")
        return

    if not a.real_cause:
        raise SystemExit("Falta la causa real.")
    os.makedirs(os.path.dirname(a.labels) or ".", exist_ok=True)
    with open(a.labels, "a", encoding="utf-8", newline="\n") as fh:
        json.dump({"case_id": a.case_id, "real_cause": a.real_cause,
                   "nota": a.nota,
                   "ts": datetime.now(timezone.utc).isoformat()}, fh, ensure_ascii=False)
        fh.write("\n")
    print("Etiqueta registrada.")


if __name__ == "__main__":
    main()