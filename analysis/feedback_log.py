"""
analysis/feedback_log.py — el ingeniero corrige al bot por chat y eso queda registrado.

Dos archivos JSONL (cooling.cases_path / cooling.feedback_path):

  cooling_cases.jsonl  lo que el bot PROPUSO: un caso por episodio o aviso, con su causa probable
                       y las características usadas (pendiente, compresor, puerta, hora...).
  feedback.jsonl       lo que el ingeniero DIJO: "el de las 06:17 fue la puerta", "acertaste".

Con ambos se calcula el acierto real del bot por tipo de causa (la meta del proyecto: ~80% sobre
~20 casos) y se obtienen los datos para calibrar umbrales o entrenar un árbol más adelante.
Por ahora la corrección SOLO se registra: no cambia el comportamiento del bot.

Todo determinista (sin LLM): el texto del usuario solo rellena campos.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from core.clock import local_tz
from analysis.product_log import KNOWN_PRODUCTS, _norm

# ----------------------------------------------------------------------
# Vocabulario de causas reales (lo que el ingeniero puede decir)
# ----------------------------------------------------------------------

REAL_LABEL = {
    "puerta": "puerta abierta",
    "ingreso_producto": "ingreso de producto",
    "deshielo": "deshielo",
    "falla_compresor": "falla del compresor",
    "perdida_refrigerante": "pérdida de refrigerante",
    "evaporador_hielo": "hielo en el evaporador",
    "ventilador": "ventilador del evaporador",
    "setpoint": "cambio de setpoint",
    "corte_energia": "corte de energía o comunicación",
    "sensor": "sensor o termocupla",
    "calor": "calor ambiente",
    "nada": "nada anormal (falsa alarma)",
}

# Qué causas reales dan por buena cada hipótesis del bot ("acertó").
BOT_TO_REAL = {
    "deshielo": {"deshielo"},
    "deshielo_largo": {"deshielo", "evaporador_hielo", "ventilador"},
    "puerta": {"puerta"},
    "carga_con_puerta": {"puerta", "ingreso_producto"},
    "carga_termica": {"ingreso_producto", "calor"},
    "capacidad": {"falla_compresor", "perdida_refrigerante", "evaporador_hielo", "ventilador"},
    "sin_frio": {"setpoint", "falla_compresor"},
    "corte_datos": {"corte_energia"},
    "lectura_espuria": {"sensor"},
    "setpoint": {"setpoint"},
    "ingreso_producto": {"ingreso_producto"},
    "carga_o_capacidad": {"ingreso_producto", "calor", "falla_compresor", "perdida_refrigerante",
                          "evaporador_hielo", "ventilador"},
    "sin_causa": set(), "en_curso": set(), "sin_explicacion": set(),
}

BOT_LABEL = {
    "carga_o_capacidad": "carga alta o pérdida de capacidad",
    "sin_explicacion": "sin explicación",
}


def bot_label(kind: str) -> str:
    from analysis.cooling_report import LABEL
    return LABEL.get(kind) or BOT_LABEL.get(kind) or kind.replace("_", " ")


def verdict_of(bot_kind: str, real: str | None) -> str:
    """acerto | fallo | falsa_alarma | sin_hipotesis | sin_causa_real"""
    if real is None:
        return "sin_causa_real"
    ok = BOT_TO_REAL.get(bot_kind)
    if not ok:
        return "sin_hipotesis"
    if real == "nada":
        return "acerto" if bot_kind == "deshielo" else "falsa_alarma"
    return "acerto" if real in ok else "fallo"


# ----------------------------------------------------------------------
# Lenguaje natural -> corrección
# ----------------------------------------------------------------------

_CUE = re.compile(r"\b(fue|fueron|era|eran|se debio|se debe|debido a|acert\w*|correct\w*|equivoc\w*|"
                  r"incorrect\w*|tenias razon|tienes razon|falsa alarma|no paso nada|exacto)\b")
_WRONG = re.compile(r"\b(no acert\w*|incorrect\w*|equivoc\w*|no es correct\w*|no era asi|error)\b")
_RIGHT = re.compile(r"\b(acert\w*|correct\w*|exacto|tenias razon|tienes razon|asi es|bien dicho)\b")
_HM = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_AT = re.compile(r"\blas\s+(\d{1,2})\b(?!:)")
_LAST = re.compile(r"\b(ultim[oa]s?|esa|ese|eso|esta alerta|este episodio|el anterior|la anterior)\b")
_NEG = re.compile(r"(no\s+(?:era|fue|es|se\s+debio\s+a|se\s+debio)|nada\s+de)\s*$")

_CAUSES: dict[str, list[str]] = {
    "falla_compresor": [r"(?:falla|fallo|averia|dano|danado|no\s+arranc\w*|se\s+apago)\s+(?:del?\s+|en\s+el\s+)?compresor",
                        r"compresor\s+(?:fallo|danado|no\s+arranc\w*|se\s+apago|con\s+falla)"],
    "perdida_refrigerante": [r"perdida", r"fuga", r"refrigerante", r"\bgas\b"],
    "evaporador_hielo": [r"hielo\s+en\s+el\s+evaporador", r"evaporador\s+(?:con\s+hielo|congelado)", r"escarcha"],
    "ventilador": [r"ventilador"],
    "setpoint": [r"setpoint", r"punto\s+de\s+ajuste", r"consigna"],
    "corte_energia": [r"corte\s+de\s+(?:energia|luz)", r"se\s+fue\s+la\s+luz", r"apagon", r"sin\s+energia",
                      r"sin\s+comunicacion", r"se\s+cayo\s+la\s+comunicacion"],
    "sensor": [r"falso\s+contacto", r"sensor", r"termocupla", r"cableado"],
    "deshielo": [r"deshielo", r"desescarche", r"defrost"],
    "puerta": [r"puerta", r"abrieron", r"se\s+abrio", r"apertura"],
    "calor": [r"calor", r"dia\s+caluroso", r"temperatura\s+ambiente"],
    "nada": [r"falsa\s+alarma", r"no\s+paso\s+nada", r"nada\s+raro", r"sin\s+novedad"],
}


def _ingreso_patterns(known) -> list[str]:
    prods = "|".join(re.escape(p) for p in sorted({_norm(k) for k in known} | set(KNOWN_PRODUCTS), key=len, reverse=True))
    return [r"producto\s+nuevo", r"nuevo\s+producto", r"carga\s+nueva", r"nueva\s+carga", r"mercancia",
            r"(?:entro|entraron|ingreso|ingresaron|llego|llegaron|cargaron|descargaron|metieron)\s+(?:de\s+)?"
            r"(?:el\s+|la\s+|los\s+|las\s+|un\s+|una\s+)?(?:producto|carga|" + prods + r")",
            r"ingreso\s+de\s+producto"]


def parse_feedback(text: str, known=()) -> dict | None:
    """
    Reconoce una corrección del ingeniero. Devuelve
      {"hhmm": (h, m) | None, "last": bool, "real": causa | None, "rejected": [causas], "verdict": "right"|"wrong"|None}
    o None si no es una corrección (las preguntas con "?" nunca lo son).
    """
    if "?" in text or "¿" in text:
        return None
    t = _norm(text)
    if not _CUE.search(t):
        return None
    mentions: list[tuple[int, str, bool]] = []          # (posición, causa, negada)
    pats = dict(_CAUSES)
    pats["ingreso_producto"] = _ingreso_patterns(known)
    for cause, plist in pats.items():
        for pat in plist:
            for m in re.finditer(pat, t):
                neg = bool(_NEG.search(t[max(0, m.start() - 22):m.start()]))
                mentions.append((m.start(), cause, neg))
    mentions.sort()
    real = None
    rejected: list[str] = []
    for _, cause, neg in mentions:
        if neg:
            if cause not in rejected:
                rejected.append(cause)
        else:
            real = cause
    if real in rejected:
        rejected.remove(real)
    verdict = "wrong" if _WRONG.search(t) else "right" if _RIGHT.search(t) else None
    if real is None and verdict is None and not rejected:
        return None
    hhmm = None
    m = _HM.search(t)
    if m:
        hhmm = (int(m.group(1)) % 24, int(m.group(2)))
    else:
        m = _AT.search(t)
        if m:
            hhmm = (int(m.group(1)) % 24, 0)
    return {"hhmm": hhmm, "last": bool(_LAST.search(t)), "real": real, "rejected": rejected, "verdict": verdict}


# ----------------------------------------------------------------------
# Libro de casos
# ----------------------------------------------------------------------

class CaseBook:
    def __init__(self, cases_path: str | None = "logs/cooling_cases.jsonl",
                 feedback_path: str | None = "logs/feedback.jsonl",
                 tz_name: str = "America/Bogota") -> None:
        self.cases_path, self.feedback_path, self.tz_name = cases_path, feedback_path, tz_name
        self.cases: dict[str, dict] = {}
        self.labels: dict[str, dict] = {}
        for path, apply in ((cases_path, self._apply_case), (feedback_path, self._apply_feedback)):
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            apply(json.loads(line))
                        except Exception:
                            pass

    # ---- persistencia ----
    @staticmethod
    def _append(path, rec) -> None:
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _apply_case(self, rec: dict) -> None:
        old = self.cases.get(rec["id"], {})
        new = {k: v for k, v in rec.items() if v is not None}
        if old.get("reported_at"):
            new.pop("reported_at", None)        # se conserva la primera vez que se avisó
        self.cases[rec["id"]] = {**old, **new}

    def _apply_feedback(self, rec: dict) -> None:
        cur = self.labels.get(rec["case_id"], {"rejected": []})
        merged = {**cur, **{k: v for k, v in rec.items() if v not in (None, [])}}
        merged["rejected"] = sorted(set(cur.get("rejected", [])) | set(rec.get("rejected") or []))
        self.labels[rec["case_id"]] = merged

    # ---- lo que propone el bot ----
    def record_case(self, rec: dict) -> None:
        self._apply_case(rec)
        self._append(self.cases_path, rec)

    # ---- localizar un caso ----
    def _local(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone(local_tz(self.tz_name))

    def find(self, hhmm: tuple[int, int], now: datetime, window_min: float = 25.0) -> dict | None:
        """Caso cuyo inicio (o fin, o aviso) está más cerca de esa hora local, en las últimas 36 h."""
        tz = local_tz(self.tz_name)
        base = now.astimezone(tz)
        h, m = hhmm
        targets = []
        for day in (0, 1):
            for hh in {h, (h + 12) % 24} if h <= 12 else {h}:
                t = base.replace(hour=hh, minute=m, second=0, microsecond=0) - timedelta(days=day)
                if t <= base + timedelta(minutes=5):
                    targets.append(t)
        best, best_d = None, None
        for c in self.cases.values():
            for key in ("start", "end", "reported_at"):
                if not c.get(key):
                    continue
                ct = self._local(c[key])
                for t in targets:
                    d = abs((ct - t).total_seconds()) / 60.0
                    if d <= window_min and (best_d is None or d < best_d):
                        best, best_d = c, d
        return best

    def get(self, case_id: str | None) -> dict | None:
        return self.cases.get(case_id) if case_id else None

    def last(self, now: datetime | None = None) -> dict | None:
        cs = [c for c in self.cases.values() if c.get("type") in ("episodio", "alerta_temprana")
              and (c.get("reported") or c.get("type") == "alerta_temprana")]
        return max(cs, key=lambda c: c.get("start", "")) if cs else None

    def recent(self, n: int = 4) -> list[dict]:
        cs = [c for c in self.cases.values() if c.get("reported") or c.get("type") == "alerta_temprana"]
        return sorted(cs, key=lambda c: c.get("start", ""), reverse=True)[:n]

    # ---- lo que dice el ingeniero ----
    def add_feedback(self, case_id: str, real: str | None, rejected: list[str], verdict_said: str | None,
                     text: str, user: str = "") -> dict:
        c = self.cases[case_id]
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "case_id": case_id, "real_cause": real,
               "rejected": rejected, "said": verdict_said, "text": text, "user": user,
               "bot_kind": c.get("bot_kind")}
        self._apply_feedback(rec)
        self._append(self.feedback_path, rec)
        return self.labels[case_id]

    def verdict(self, case_id: str) -> str:
        lab, c = self.labels.get(case_id, {}), self.cases.get(case_id, {})
        real = lab.get("real_cause")
        if real is None and lab.get("said") == "right":
            return "acerto"
        if real is None and lab.get("said") == "wrong":
            return "fallo"
        if real is None and c.get("bot_kind") in lab.get("rejected", []):
            return "fallo"
        return verdict_of(c.get("bot_kind", ""), real)

    # ---- acierto ----
    def accuracy(self) -> dict:
        res = Counter()
        wrong: Counter = Counter()
        for cid in self.labels:
            if cid not in self.cases:
                continue
            v = self.verdict(cid)
            res[v] += 1
            if v in ("fallo", "falsa_alarma"):
                real = self.labels[cid].get("real_cause") or "?"
                wrong[(self.cases[cid].get("bot_kind", "?"), real)] += 1
        con_hip = res["acerto"] + res["fallo"] + res["falsa_alarma"]
        return {"etiquetados": sum(res.values()), "con_hipotesis": con_hip, "acertados": res["acerto"],
                "acierto": (res["acerto"] / con_hip) if con_hip else None, "resultados": dict(res),
                "errores": wrong.most_common(5), "casos_totales": len(self.cases)}

    def accuracy_text(self, meta: int = 20) -> str:
        a = self.accuracy()
        if a["etiquetados"] == 0:
            return ("Todavía no tengo casos etiquetados. Dime, por ejemplo, \"el episodio de las 06:17 fue por "
                    "la puerta\" o \"acertaste con el de las 06:17\" y empiezo a medir mi acierto.")
        lines = []
        if a["acierto"] is not None:
            lines.append(f"Acierto: {a['acertados']}/{a['con_hipotesis']} ({a['acierto']:.0%}) en {a['etiquetados']} "
                         f"caso(s) etiquetado(s) de {a['casos_totales']}.")
        else:
            lines.append(f"{a['etiquetados']} caso(s) etiquetado(s), ninguno con hipótesis mía que evaluar.")
        if a["errores"]:
            lines.append("Fallos: " + "; ".join(f"dije {bot_label(b)} → era {REAL_LABEL.get(r, r)} ({n})"
                                                  for (b, r), n in a["errores"]))
        falta = max(0, meta - a["etiquetados"])
        lines.append(f"Meta del proyecto: ~{meta} casos" + (f" (faltan {falta})." if falta else " ✅ alcanzada."))
        return "\n".join(lines)
