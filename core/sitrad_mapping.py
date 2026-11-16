"""
core/sitrad_mapping.py — de "variables de SITRAD" a canales del bot.

Compartido por la lectura de Excel, la repetición de lecturas.jsonl y la API en vivo, para que los tres
entiendan los nombres igual: "camera 01 (°C)", "Setpoint", "Desvío relativo al Setpoint", "REFR", "DEFR",
"Puerta abierta", "SALIDA HOT" (los espacios dobles, mayúsculas y acentos no importan).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from core.channel_catalog import CONTROLLER_SPECS, norm_col

_ON = {"conectado", "ligado", "on", "si", "sim", "yes", "abierta", "abierto", "activo", "activa", "1", "true",
       "encendido", "acionado", "accionado"}
_OFF = {"desconectado", "desligado", "off", "no", "nao", "cerrada", "cerrado", "inactivo", "inactiva", "0",
        "false", "apagado", "desativado", "desactivado"}


def key(s) -> str:
    s = unicodedata.normalize("NFD", norm_col(str(s)))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def parse_state(v):
    """Estado -> 1.0 / 0.0 / None (acepta 0/1, booleanos y texto Conectado/Desconectado, Sí/No, ON/OFF...)."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return 1.0 if float(v) >= 0.5 else 0.0
    k = key(v)
    if k in _ON:
        return 1.0
    if k in _OFF:
        return 0.0
    try:
        return 1.0 if float(k.replace(",", ".")) >= 0.5 else 0.0
    except ValueError:
        return None


def parse_number(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


def match_columns(headers: list) -> dict[str, int]:
    """canal del catálogo -> índice del encabezado (por nombre exacto y, si no, por prefijo/parecido)."""
    keys = [key(h) if h is not None and str(h) != "nan" else "" for h in headers]
    out: dict[str, int] = {}
    for spec in CONTROLLER_SPECS:
        want = key(spec.source_col)
        if want in keys:
            out[spec.name] = keys.index(want)
    fallbacks = {
        "Temp Cámara": lambda k: k.startswith(("camera", "camara")),
        "Setpoint": lambda k: k.startswith("setpoint") and "desvio" not in k,
        "Desvío vs Setpoint": lambda k: "desvio" in k,
        "Estado Refrigeración": lambda k: k == "refr",
        "Estado Deshielo": lambda k: k == "defr",
        "Puerta Abierta": lambda k: k.startswith("puerta"),
        "Salida HOT": lambda k: k.replace(" ", "") == "salidahot",
    }
    for name, pred in fallbacks.items():
        if name not in out:
            for i, k in enumerate(keys):
                if k and pred(k):
                    out[name] = i
                    break
    return out


def parse_datetime_any(v) -> datetime | None:
    """Fecha de la API: ISO con o sin Z, o época en segundos/milisegundos. Devuelve UTC o None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e11:
            x /= 1000.0
        if x < 1e8:
            return None
        return datetime.fromtimestamp(x, tz=timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d{9,13}", s):
            return parse_datetime_any(int(s))
        try:
            t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None
