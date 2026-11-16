"""
analysis/product_log.py — catálogo de productos con su regulación y cargas en la cámara.

Un JSON (cooling.products_path, por defecto products.json) guarda dos cosas:

  "products": reglas por producto, p. ej. el suero no debe pasar de 12 °C:
      {"suero": {"max_temp_c": 12, "max_minutes_above": 0},
       "ternera": {"max_temp_c": 4, "max_minutes_above": 120, "aliases": ["res", "carne de res"]}}
      max_temp_c           techo de temperatura del producto (°C)
      max_minutes_above    tolerancia: minutos que puede estar sobre el techo.
                           0 = no admite tolerancia; ausente = no hay plazo configurado.
  "loads": producto que hay dentro, con su hora de entrada:
      [{"product": "suero", "kg": 800, "t_in": "2026-10-13T12:00:00-05:00", "entry_temp_c": 8}]

Se puede escribir a mano ANTES de iniciar o dictárselo al bot ("el suero no debe subir
de 12 grados", "entró suero, hora de entrada 12:00 del 13/10/2026"). El límite que se
vigila en la cámara es el del producto MÁS ESTRICTO de los presentes.

La termocupla mide el aire, no el producto: la exposición que se informa es "tiempo
con el aire de la cámara sobre el techo del producto desde su entrada".
Todo es determinista (sin LLM): el texto del usuario solo rellena campos.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from core.clock import local_tz

GOOD = ("VALID", "FROZEN")

KNOWN_PRODUCTS = (
    "ternera", "carne", "carne de res", "pollo", "cerdo", "pescado", "pavo", "cordero",
    "embutidos", "jamon", "salchicha", "leche", "queso", "yogurt", "lacteos", "mantequilla",
    "suero", "crema", "kumis", "verduras", "frutas", "huevos",
)
_STOP = {"producto", "productos", "carga", "algo", "mas", "no", "que", "en", "a", "la", "el", "los", "las",
         "un", "una", "cava", "camara", "hora", "dia", "fecha"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


@dataclass
class Load:
    id: str
    product: str
    kg: float | None
    t_in: datetime                       # UTC
    t_out: datetime | None = None
    entry_temp_c: float | None = None    # temperatura del producto al entrar, si se dijo
    note: str = ""

    def active(self, now: datetime) -> bool:
        return self.t_out is None or self.t_out > now

    def label(self) -> str:
        q = f"{self.kg:g} kg de " if self.kg else ""
        return f"{q}{self.product}"


_UNSET = object()


def rule_for(product: str, rules: dict | None) -> dict:
    """Regla del producto: busca el nombre (o un alias) en el mapa de reglas."""
    rules = rules or {}
    p = _norm(product)
    for name, rule in rules.items():
        n = _norm(name)
        if n == p or n in p or p in n:
            return rule or {}
    return rules.get("*", {}) or {}


class ProductLog:
    """Catálogo de reglas + cargas, persistidos en un JSON (tolerante a archivos dañados)."""

    def __init__(self, path: str | None = None, tz_name: str = "America/Bogota") -> None:
        self.path = path
        self.tz_name = tz_name
        self.rules: dict[str, dict] = {}
        self.loads: list[Load] = []
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):                 # formato antiguo: solo cargas
                    data = {"products": {}, "loads": data}
                for name, rule in (data.get("products") or {}).items():
                    if isinstance(rule, dict):
                        self.rules[_norm(name).strip()] = dict(rule)
                for d in data.get("loads") or []:
                    self.loads.append(self._load_from_dict(d))
            except Exception:
                self.rules, self.loads = {}, []

    # ---------- persistencia ----------
    def _load_from_dict(self, d: dict) -> Load:
        def when(v):
            t = datetime.fromisoformat(v)
            return t.replace(tzinfo=local_tz(self.tz_name)) if t.tzinfo is None else t
        return Load(id=d.get("id") or uuid.uuid4().hex[:8],
                    product=self.canonical(d["product"]), kg=d.get("kg"),
                    t_in=when(d["t_in"]),
                    t_out=when(d["t_out"]) if d.get("t_out") else None,
                    entry_temp_c=d.get("entry_temp_c"), note=d.get("note", ""))

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            rows = []
            for l in self.loads:
                d = asdict(l)
                d["t_in"] = l.t_in.isoformat()
                d["t_out"] = l.t_out.isoformat() if l.t_out else None
                rows.append(d)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"products": self.rules, "loads": rows}, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ---------- catálogo ----------
    def canonical(self, name: str) -> str:
        n = _norm(name).strip()
        for key, rule in self.rules.items():
            if n == key or n in [_norm(a).strip() for a in rule.get("aliases", [])]:
                return key
        return n

    def known_names(self) -> list[str]:
        out = set(self.rules)
        for rule in self.rules.values():
            out.update(_norm(a).strip() for a in rule.get("aliases", []))
        return sorted(out)

    def set_rule(self, product: str, max_temp_c=_UNSET, max_minutes_above=_UNSET,
                 aliases: list[str] | None = None) -> dict:
        key = self.canonical(product)
        rule = self.rules.setdefault(key, {})
        if max_temp_c is not _UNSET:
            rule["max_temp_c"] = None if max_temp_c is None else float(max_temp_c)
        if max_minutes_above is not _UNSET:
            rule["max_minutes_above"] = None if max_minutes_above is None else float(max_minutes_above)
        if aliases:
            rule["aliases"] = sorted(set(rule.get("aliases", [])) | set(aliases))
        self._save()
        return rule

    def rules_map(self) -> dict:
        """Reglas expandidas por nombre y alias (lo que usa assess_loads)."""
        out = {}
        for key, rule in self.rules.items():
            out[key] = rule
            for a in rule.get("aliases", []):
                out[_norm(a).strip()] = rule
        return out

    def effective_limit(self, now: datetime, default: float):
        """
        (límite aplicable, producto que lo fija): el más estricto entre los productos presentes.
        Un producto sin regla usa el límite de la cámara. Sin productos, el de la cámara.
        Con solo suero (12 °C) el límite es 12 °C; con suero y ternera (4 °C), 4 °C.
        """
        rm = self.rules_map()
        best, who = None, None
        for l in self.active(now):
            lim = rule_for(l.product, rm).get("max_temp_c")
            lim = float(default) if lim is None else float(lim)
            if best is None or lim < best:
                best, who = lim, l.product
        if best is None:
            return float(default), None
        return best, who

    # ---------- cargas ----------
    def add(self, product: str, kg: float | None, t_in: datetime,
            entry_temp_c: float | None = None, note: str = "") -> Load:
        load = Load(id=uuid.uuid4().hex[:8], product=self.canonical(product), kg=kg, t_in=t_in,
                    entry_temp_c=entry_temp_c, note=note)
        self.loads.append(load)
        self._save()
        return load

    def active(self, now: datetime) -> list[Load]:
        return [l for l in self.loads if l.t_in <= now and l.active(now)]

    def remove(self, now: datetime, product: str | None = None) -> list[Load]:
        out = []
        want = self.canonical(product) if product else None
        for l in self.active(now):
            if want is None or want == l.product or _norm(want) in _norm(l.product):
                l.t_out = now
                out.append(l)
        if out:
            self._save()
        return out


# ----------------------------------------------------------------------
# Lenguaje natural -> campos
# ----------------------------------------------------------------------

_QTY = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kgs|kilo|kilos|kilogramos|tonelada|toneladas|ton|t)\b")
_AGO = re.compile(r"(?:desde\s+)?hace\s+(\d+(?:[.,]\d+)?)\s*(min\w*|h\b|horas?)")
_HM = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_AT = re.compile(r"(?:desde\s+)?(?:a\s+)?las\s+(\d{1,2})\b")
_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_TEMP = re.compile(r"\ba\s+(-?\d+(?:[.,]\d+)?)\s*(?:°|º|grados)")
_NUM_C = r"(-?\d+(?:[.,]\d+)?)\s*(?:°\s*c|º\s*c|°|º|grados|c(?![a-z]))"
_CEIL = re.compile(r"(?:no\s+(?:debe|puede|deberia)\s+(?:subir|pasar|superar|exceder|estar|llegar)|maximo|max\.?|"
                   r"limite|tope|techo)\D{0,25}?" + _NUM_C)
_TOL = re.compile(r"(?:tolerancia(?:\s+de)?|puede\s+estar)\s+(\d+(?:[.,]\d+)?)\s*(horas?|h\b|min\w*)"
                  r"|(\d+(?:[.,]\d+)?)\s*(horas?|h\b|min\w*)\s+(?:por\s+encima|sobre|arriba)")
_VERBS = ("entro", "entraron", "ingreso", "ingresaron", "ingresar", "cargue", "cargamos", "meti", "metimos",
          "llego", "llegaron", "recibi", "recibimos", "descargue", "descargamos", "acaban de", "acabo de")
_OUT = ("salio", "salieron", "retire", "retiramos", "despach", "sacamos", "saque", "ya no hay", "descargaron")


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _guess_product(t: str, m_qty, known) -> str | None:
    names = sorted({_norm(k).strip() for k in known} | set(KNOWN_PRODUCTS), key=len, reverse=True)
    for p in names:
        if p and re.search(r"\b" + re.escape(p) + r"\b", t):
            return p
    cands = []
    if m_qty:
        m = re.match(r"\s*(?:de|del)\s+([a-z]+(?:\s+(?:de|del)\s+[a-z]+)?)", t[m_qty.end():])
        if m:
            cands.append(m.group(1))
    m = re.search(r"(?:entro|entraron|ingreso|ingresaron|ingresar|llego|llegaron|cargue|cargamos|metimos|"
                  r"recibimos|hay|tenemos|para)\s+(?:de\s+)?(?:el|la|los|las|un|una|unos|unas)?\s*"
                  r"(?:\d+(?:[.,]\d+)?\s*(?:kg|kilos?|toneladas?)\s+de\s+)?([a-z]+(?:\s+(?:de|del)\s+[a-z]+)?)", t)
    if m:
        cands.append(m.group(1))
    m = re.search(r"(?:el|la|los|las)\s+([a-z]+)\s+no\s+(?:debe|puede|deberia)", t)
    if m:
        cands.append(m.group(1))
    for c in cands:
        first = c.split()[0]
        if first not in _STOP and len(first) > 2:
            return c.strip()
    return None


def _entry_time(t: str, now: datetime, tz_name: str) -> datetime:
    tz = local_tz(tz_name)
    m = _AGO.search(t)
    if m:
        v = _num(m.group(1))
        return now - timedelta(minutes=v * (60.0 if m.group(2).startswith("h") else 1.0))
    base = now.astimezone(tz)
    hm = _HM.search(t)
    at = None if hm else _AT.search(t)
    date = None
    md = _DATE.search(t)
    if md:
        d, mo = int(md.group(1)), int(md.group(2))
        y = int(md.group(3)) if md.group(3) else base.year
        y += 2000 if y < 100 else 0
        if 1 <= d <= 31 and 1 <= mo <= 12:
            date = (y, mo, d)
    h = mi = None
    if hm:
        h, mi = int(hm.group(1)) % 24, int(hm.group(2))
    elif at:
        h, mi = int(at.group(1)) % 24, 0
    try:
        if date:
            loc = datetime(date[0], date[1], date[2], h or 0, mi or 0, tzinfo=tz)
        elif h is not None:
            loc = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if "ayer" in t:
                loc -= timedelta(days=1)
            elif loc.astimezone(timezone.utc) > now:
                loc -= timedelta(days=1)
        else:
            return now
    except ValueError:
        return now
    return loc.astimezone(timezone.utc)


def parse_message(text: str, now: datetime, tz_name: str = "America/Bogota", known=()) -> dict | None:
    """
    Reconoce un mensaje que registra producto y/o su regulación. Devuelve
      {kind: "load" | "rule" | "both", product, kg, t_in, entry_temp_c, max_temp_c, max_minutes_above}
    o None si no es de esto (las preguntas no registran nada).
    """
    if "?" in text or "¿" in text:
        return None
    t = _norm(text)
    if any(w in t for w in _OUT):
        return None
    m_ceil = _CEIL.search(t)
    ceil = _num(m_ceil.group(1)) if m_ceil else None
    t2 = (t[:m_ceil.start()] + " " + t[m_ceil.end():]) if m_ceil else t
    tol = None
    mt = _TOL.search(t2)
    if mt:
        v, u = (mt.group(1), mt.group(2)) if mt.group(1) else (mt.group(3), mt.group(4))
        tol = _num(v) * (60.0 if u.startswith("h") else 1.0)
        t2 = t2[:mt.start()] + " " + t2[mt.end():]
    elif "sin tolerancia" in t:
        tol = 0.0
    if ceil is not None and tol is None:
        tol = 0.0                          # "no debe subir de X": techo sin tolerancia
    m_qty = _QTY.search(t2)
    prod = _guess_product(t2, m_qty, known)
    has_entry = bool(m_qty) or any(v in t for v in _VERBS)
    if prod is None or (not has_entry and ceil is None):
        return None
    out = {"kind": "rule" if (ceil is not None and not has_entry) else "both" if ceil is not None else "load",
           "product": prod, "kg": None, "t_in": now, "entry_temp_c": None,
           "max_temp_c": ceil, "max_minutes_above": tol if ceil is not None or tol is not None else None}
    if has_entry:
        if m_qty:
            out["kg"] = _num(m_qty.group(1)) * (1000.0 if m_qty.group(2) in ("t", "ton", "tonelada", "toneladas") else 1.0)
        out["t_in"] = _entry_time(t2, now, tz_name)
        mtmp = _TEMP.search(t2)
        out["entry_temp_c"] = _num(mtmp.group(1)) if mtmp else None
    return out


def parse_load(text: str, now: datetime, tz_name: str = "America/Bogota", known=()) -> dict | None:
    """Compatibilidad: solo si el mensaje registra una carga."""
    d = parse_message(text, now, tz_name, known)
    return d if d and d["kind"] in ("load", "both") else None


def parse_removal(text: str, known=()) -> tuple[bool, str | None]:
    """¿Dice que el producto ya salió? Devuelve (es_retiro, producto o None = todo)."""
    t = _norm(text)
    if not any(w in t for w in _OUT):
        return False, None
    return True, _guess_product(t, _QTY.search(t), known)


# ----------------------------------------------------------------------
# Exposición del producto frente a SU techo
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class LoadRisk:
    load: Load
    minutes_in: float
    minutes_above: float        # tiempo con el aire sobre el techo del producto desde que entró
    max_c: float | None         # máxima temperatura del aire desde que entró
    max_minutes: float | None   # tolerancia (0 = sin tolerancia; None = no configurada)
    level: str                  # ok | vigilar | riesgo | critico
    limit_c: float              # techo aplicado a este producto
    partial: bool = False       # el producto entró antes de que haya datos
    covered_from: datetime | None = None

    @property
    def remaining_min(self) -> float | None:
        return None if self.max_minutes is None else self.max_minutes - self.minutes_above


def assess_loads(loads, temp_readings, limit_c: float, now: datetime, rules: dict | None = None):
    from analysis.forecast import despike
    rs = despike(temp_readings)          # los picos aislados del sensor no son exposición del producto
    out: list[LoadRisk] = []
    for l in loads:
        rule = rule_for(l.product, rules)
        lim = rule.get("max_temp_c")
        lim = float(limit_c) if lim is None else float(lim)
        end = min(now, l.t_out) if l.t_out else now
        above, mx = 0.0, None
        for a, b in zip(rs, rs[1:]):
            if b.event_time <= l.t_in or a.event_time >= end:
                continue
            lo, hi = max(a.event_time, l.t_in), min(b.event_time, end)
            dt = min((hi - lo).total_seconds() / 60.0, 15.0)
            v = float(a.value)
            if dt > 0 and v > lim:
                above += dt
            mx = v if mx is None else max(mx, v)
        mm = rule.get("max_minutes_above")
        mm = None if mm is None else float(mm)
        if mm is not None:
            if mm <= 0:
                level = "critico" if above > 0 else "ok"
            else:
                ratio = above / mm
                level = "critico" if ratio >= 1.0 else "riesgo" if ratio >= 0.5 else "vigilar" if above > 0 else "ok"
        else:
            level = "vigilar" if above > 0 else "ok"
        first = rs[0].event_time if rs else None
        partial = bool(first is not None and l.t_in < first - timedelta(minutes=5))
        out.append(LoadRisk(l, (end - l.t_in).total_seconds() / 60.0, above, mx, mm, level, lim, partial, first))
    return out


def _dur(m: float) -> str:
    m = int(round(m))
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h" if r == 0 else f"{h} h {r:02d} min"


def _t(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def render_load_line(risk: LoadRisk, limit_c: float | None = None, tz_name: str = "America/Bogota",
                     detail: bool = False) -> str:
    """Una línea: producto, tiempo sobre SU techo y margen. Con detail=True, con hora de entrada."""
    l = risk.load
    lim = _t(risk.limit_c)
    if risk.minutes_above <= 0:
        s = f"{l.label()}: sin superar {lim} °C"
        if risk.max_c is not None and detail:
            s += f" (máx. {_t(round(risk.max_c, 1))} °C)"
    else:
        s = f"{l.label()}: {_dur(risk.minutes_above)} sobre {lim} °C"
        if risk.max_minutes is None:
            s += " (sin plazo configurado)"
        elif risk.max_minutes <= 0:
            s += " — no admite tolerancia"
        elif risk.remaining_min is not None and risk.remaining_min > 0:
            s += f" (máx. {_dur(risk.max_minutes)}; margen {_dur(risk.remaining_min)})"
        else:
            s += f" — superó el máximo de {_dur(risk.max_minutes)}"
    if detail:
        hm = l.t_in.astimezone(local_tz(tz_name)).strftime("%d/%m %H:%M")
        s += f". Entró {hm}"
        if l.entry_temp_c is not None:
            s += f" a {_t(l.entry_temp_c)} °C"
    if risk.partial and risk.covered_from is not None:
        s += f" [solo hay datos desde {risk.covered_from.astimezone(local_tz(tz_name)).strftime('%d/%m %H:%M')}]"
    return s


ICON = {"ok": "🟢", "vigilar": "🟡", "riesgo": "🟠", "critico": "🔴"}


def catalog_summary(log: ProductLog) -> str:
    """Productos con regulación conocidos."""
    if not log.rules:
        return ("No tengo productos con regulación cargados. Dime, por ejemplo: "
                "\"el suero no debe subir de 12 grados\".")
    lines = ["Productos con regulación:"]
    for name, r in sorted(log.rules.items()):
        t = f"{_t(r['max_temp_c'])} °C" if r.get("max_temp_c") is not None else "sin techo"
        m = r.get("max_minutes_above")
        tol = "sin plazo configurado" if m is None else "sin tolerancia" if m <= 0 else f"tolerancia {_dur(m)}"
        lines.append(f"• {name}: no debe pasar de {t} ({tol})")
    return "\n".join(lines)
