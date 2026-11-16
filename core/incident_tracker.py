"""
core/incident_tracker.py — el diagnóstico de un incidente abierto no se congela.

El diagnóstico se calcula al abrir el incidente, con la evidencia mínima. Pero
la causa puede cambiar sin que el incidente cambie (una falla parcial que pasa
a total, una puerta que deja la cámara caliente y luego falla el compresor):
el incidente sigue "abierto", su severidad ya está saturada y no hay escalada.
Sin reevaluar, el bot queda callado o con un diagnóstico obsoleto.

DiagnosisTracker reevalúa cada `every_s` los incidentes abiertos y devuelve una
ACTUALIZACIÓN cuando la causa principal es distinta de la ya comunicada y se
mantiene en `confirm` revisiones seguidas (evita parpadeos). Si el incidente se
había silenciado (benigno / sin explicación) y pasa a una causa no benigna,
la actualización es el primer aviso (`first_notice=True`).

No envía nada ni conoce Telegram: devuelve datos; quien lo use decide.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from analysis.diagnosis import BENIGN_HYPOTHESES


@dataclass
class _Track:
    last_principal: str            # lo último que se le dijo al usuario (o lo silenciado)
    announced: bool                # ¿el usuario ya recibió aviso de este incidente?
    candidate: str | None = None
    streak: int = 0
    last_check: datetime | None = None


@dataclass(frozen=True)
class DiagnosisUpdate:
    ikey: str
    previous: str
    assessment: Any                # IncidentAssessment
    first_notice: bool
    incident: dict


class DiagnosisTracker:
    def __init__(self, every_s: float = 240.0, confirm: int = 2,
                 min_confidence: float = 0.5) -> None:
        self.every = timedelta(seconds=float(every_s))
        self.confirm = max(1, int(confirm))
        self.min_conf = float(min_confidence)
        self._tracks: dict[str, _Track] = {}

    def register(self, ikey: str, principal: str, announced: bool) -> None:
        """Llamar al abrir/escalar un incidente con el diagnóstico calculado."""
        t = self._tracks.get(ikey)
        if t is None:
            self._tracks[ikey] = _Track(principal, announced)
        else:
            if announced:
                t.announced = True
            if announced or principal != "indeterminado":
                t.last_principal = principal

    def drop(self, ikey: str) -> None:
        self._tracks.pop(ikey, None)

    def check(self, now: datetime, open_incidents: Iterable[str],
              assess: Callable[[str], Any],
              incident_of: Callable[[str], dict] | None = None
              ) -> list[DiagnosisUpdate]:
        """
        `assess(ikey)` devuelve un IncidentAssessment del incidente.
        Devuelve las actualizaciones que corresponde comunicar ahora.
        """
        out: list[DiagnosisUpdate] = []
        live = set(open_incidents)
        for k in list(self._tracks):
            if k not in live:
                del self._tracks[k]
        for ikey in live:
            t = self._tracks.get(ikey)
            if t is None:
                continue
            if t.last_check is not None and now - t.last_check < self.every:
                continue
            t.last_check = now
            res = assess(ikey)
            b = res.bundle
            p = b.principal
            if (p == "indeterminado" or not b.hipotesis or p in BENIGN_HYPOTHESES
                    or b.confianza < self.min_conf or p == t.last_principal):
                t.candidate, t.streak = None, 0
                continue
            if p == t.candidate:
                t.streak += 1
            else:
                t.candidate, t.streak = p, 1
            if t.streak < self.confirm:
                continue
            # Una causa benigna no justifica por sí sola un aviso nuevo.
            if res.silenced and res.reason == "benigna":
                t.last_principal = p
                t.candidate, t.streak = None, 0
                continue
            first = not t.announced
            out.append(DiagnosisUpdate(
                ikey=ikey, previous=t.last_principal, assessment=res,
                first_notice=first,
                incident=(incident_of(ikey) if incident_of else {})))
            t.last_principal, t.announced = p, True
            t.candidate, t.streak = None, 0
        return out
