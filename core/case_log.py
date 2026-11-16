"""
core/case_log.py — registro de casos diagnosticados.

Cada vez que el sistema abre o escala un incidente y calcula un diagnóstico,
se guarda un registro en JSONL: qué se observó, qué hipótesis se plantearon
(con su evidencia y lo que faltaba) y cuándo. Después se contrastan con lo
que realmente ocurrió (verdad del simulador o etiqueta manual) para medir el
acierto. Ver tools/case_report.py y tools/label_case.py.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


class CaseLog:
    def __init__(self, path: str = "logs/cases.jsonl") -> None:
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def record(self, *, key, payload, bundle, now, suppressed: bool = False) -> None:
        k = str(key)
        stage = ("update" if k.startswith("incident_update")
                 else "escalate" if k.startswith("incident_escalate") else "open")
        opened = payload.get("opened_at", "")
        incident_opened = opened
        if stage == "update":
            # Una actualización es un aviso nuevo: se evalúa en su propio instante.
            opened = now.isoformat()
        rec = {
            "ts": now.isoformat(),
            "stage": stage,
            "case_id": f"{opened}|{payload.get('incident_kind', '')}"
                       + ("|upd" if stage == "update" else ""),
            "incident_opened_at": incident_opened,
            "incident_kind": payload.get("incident_kind"),
            "channels": list(payload.get("channels", [])),
            "opened_at": opened,
            "severity_max": payload.get("severity_max"),
            "principal": bundle.principal,
            "confianza": round(float(bundle.confianza), 3),
            "provisional": bool(bundle.provisional),
            "suppressed": bool(suppressed),
            "hipotesis": [
                {"nombre": h.nombre, "score": round(float(h.score), 3),
                 "a_favor": list(h.a_favor), "en_contra": list(h.en_contra),
                 "faltan": list(h.faltan)}
                for h in bundle.hipotesis[:3]
            ],
            "rasgos": {
                k: v for k, v in bundle.rasgos.items()
                if isinstance(v, (int, float, str, bool))
            },
        }
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            json.dump(rec, fh, ensure_ascii=False, default=str)
            fh.write("\n")