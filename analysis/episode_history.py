"""analysis/episode_history.py — historial persistente de episodios sobre el límite (JSONL)."""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from statistics import median

logger = logging.getLogger(__name__)

CHANNEL = "Temp Cámara"


def _dt(s):
    if s is None:
        return None
    t = datetime.fromisoformat(str(s))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class EpisodeHistory:
    def __init__(self, path: str = "logs/episodes.jsonl", tz_name: str = "America/Bogota") -> None:
        self.path, self.tz_name = path, tz_name
        self._eps: dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                            self._eps[r["id"]] = r
                        except Exception:
                            continue                       # una línea corrupta no impide leer el resto
            except OSError:
                logger.exception("No pude leer %s", path)

    def record(self, ep, cfg, now: datetime) -> None:
        """Guarda un episodio CERRADO (idempotente: el mismo id no se escribe dos veces)."""
        try:
            if ep.end is None or ep.key in self._eps:
                return
            from analysis.cooling_report import LABEL, causa_corta, classify
            kind = ep.kind or classify(ep, cfg)
            load = getattr(ep, "load", None)
            rec = {
                "id": ep.key, "canal": CHANNEL, "start": ep.start.isoformat(), "end": ep.end.isoformat(),
                "peak_c": round(float(ep.peak_c), 2),
                "prev_c": None if ep.prev_c is None else round(float(ep.prev_c), 2),
                "above_min": round(float(ep.above_min), 1), "kind": kind, "kind_label": LABEL.get(kind, kind),
                "limit_c": float(cfg.limit_c), "load": load.label() if hasattr(load, "label") else None,
                "cause": causa_corta(ep, cfg), "limit_who": getattr(cfg, "limit_who", None),
                "recorded_at": now.isoformat(),
            }
            self._eps[rec["id"]] = rec
            if self.path:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("No pude registrar el episodio en el historial.")

    def query(self, desde: datetime, hasta: datetime, canal: str | None = None, causa: str | None = None,
              min_min: float | None = None, limit: int = 20) -> list[dict]:
        out = []
        for r in self._eps.values():
            s, e = _dt(r["start"]), _dt(r["end"])
            if e < desde or s > hasta:
                continue
            if canal and r.get("canal", CHANNEL) != canal:
                continue
            if causa and causa.lower() not in f"{r.get('kind', '')} {r.get('kind_label', '')} {r.get('cause', '')}".lower():
                continue
            if min_min is not None and r["above_min"] < min_min:
                continue
            out.append(r)
        out.sort(key=lambda r: r["start"])
        return out[-limit:] if limit else out

    def summary(self, desde: datetime, hasta: datetime) -> dict:
        eps = self.query(desde, hasta, limit=0)
        if not eps:
            return {"n": 0, "por_causa": {}, "duracion_mediana": None, "tiempo_total_sobre_limite": 0.0,
                    "peor_episodio": None}
        worst = max(eps, key=lambda r: r["peak_c"])
        return {"n": len(eps),
                "por_causa": dict(Counter(r.get("kind_label") or r.get("kind") or "?" for r in eps)),
                "duracion_mediana": float(median(r["above_min"] for r in eps)),
                "tiempo_total_sobre_limite": float(sum(r["above_min"] for r in eps)),
                "peor_episodio": worst}
