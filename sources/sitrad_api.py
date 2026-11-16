"""
sources/sitrad_api.py — el bot leyendo la API local de SITRAD PRO en vivo.

Mismas llamadas que el extractor (sitrad_source.py original), pero como módulo reutilizable:
  GET /instruments                      -> instrumentos del servidor
  GET /instruments/{id}/values          -> variables y valores de un instrumento
  GET /alarms?startDate=..&endDate=..   -> alarmas configuradas en SITRAD que se han disparado

Qué hace de más:
  * Credenciales desde config / variables de entorno SITRAD_USER y SITRAD_PASSWORD / pregunta al iniciar.
  * Encuentra solo cuál instrumento es el controlador de la cámara (el que tiene variables como camera 01,
    Setpoint, REFR, DEFR) o usa el que indiques; avisa si hay varios.
  * Convierte las variables a los canales del bot. El formato exacto del JSON de tu servidor no está
    fijado: se buscan los campos de nombre/valor/unidad/fecha por varios nombres habituales. Para ver lo que
    devuelve de verdad:  python tools/inspect_sitrad_api.py
  * Una lectura solo se entrega si su fecha es posterior a la última entregada (consultar cada 30 s no
    duplica datos de un instrumento que se actualiza cada 2 min). Si el instrumento deja de actualizar,
    las lecturas se detienen y el bot avisa "sin lecturas".
  * Las alarmas nuevas de SITRAD se entregan una sola vez (new_alarms) para mostrarlas en el chat.
  * Opcional: guarda cada lectura cruda en sitrad_data/lecturas.jsonl (mismo formato que el extractor).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

from core.channel_catalog import CONTROLLER_SPECS
from core.contracts import ChannelReading, DataQuality, Snapshot
from core.sitrad_mapping import key, match_columns, parse_datetime_any, parse_number, parse_state

logger = logging.getLogger(__name__)

_NAME_KEYS  = ("name", "description", "label", "variable", "title", "alias", "sensorName", "text", "code")
_VALUE_KEYS = ("value", "currentValue", "reading", "val", "measure", "lastValue")
_UNIT_KEYS  = ("unit", "unitOfMeasure", "unity", "measureUnit", "units", "measurementUnity")
_DATE_KEYS  = ("date", "dateTime", "datetime", "timestamp", "readAt", "lastUpdate", "updatedAt",
               "lastReading", "time", "readingDate")
_ERR_KEYS   = ("error", "hasError", "isError", "isInError", "fault", "failure")
_EN_KEYS    = ("enabled", "isEnabled", "active")


@dataclass(frozen=True)
class Variable:
    name: str
    value: object
    unit: str
    ts: datetime | None
    error: bool
    enabled: bool
    raw: dict

    @property
    def label(self) -> str:
        return f"{self.name} ({self.unit})" if self.unit and "(" not in self.name else self.name


def _first(d: dict, keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None

def iter_variables(obj):
    """Recorre el JSON de /values y entrega cada variable (diccionario con nombre y valor).

    SITRAD PRO devuelve cada variable así:
        {"code": "FncOffsetSensor1", "name": null,
         "values": [{"date": "...", "value": 0.3, "measurementUnity": "°C", ...}]}
    Se aplana: nombre = code (o name si viene), y unidad/fecha/valor salen de cada elemento de 'values'.
    """
    if isinstance(obj, list):
        for x in obj:
            yield from iter_variables(x)
    elif isinstance(obj, dict):
        inner = obj.get("values")
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            base_name = _first(obj, _NAME_KEYS) or obj.get("code")
            outer = {k: v for k, v in obj.items() if k != "values"}
            for item in inner:
                merged = {**outer, **item}
                if base_name and not merged.get("name"):
                    merged["name"] = base_name
                yield from iter_variables(merged)
            return
        name = _first(obj, _NAME_KEYS)
        has_value = any(k in obj for k in _VALUE_KEYS)
        if isinstance(name, str) and has_value:
            err = any(bool(obj.get(k)) for k in _ERR_KEYS if k in obj and not isinstance(obj.get(k), (dict, list)))
            en = True
            for k in _EN_KEYS:
                if k in obj and isinstance(obj[k], bool):
                    en = obj[k]
            unit = _first(obj, _UNIT_KEYS)
            yield Variable(name=name.strip(), value=_first(obj, _VALUE_KEYS),
                           unit=str(unit).strip() if unit is not None else "",
                           ts=parse_datetime_any(_first(obj, _DATE_KEYS)), error=err, enabled=en, raw=obj)
        else:
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    yield from iter_variables(v)

# Sensor físico -> flag IsS<n>Enabled del controlador. Un sensor deshabilitado devuelve basura
# (p. ej. -112 °C) que NO debe entrar como lectura válida.
SENSOR_INDEX = {"Temp Cámara": 1, "Temp Evaporador": 2}
_IS_EN = re.compile(r"^iss(\d)enabled$")
# Rango físicamente plausible para sondas de cámara (°C); fuera de él la lectura es inválida.
TEMP_PLAUSIBLE = (-60.0, 150.0)


def sensor_enabled_flags(variables) -> dict[int, bool]:
    """{1: True, 2: False, ...} a partir de las variables IsS1Enabled, IsS2Enabled..."""
    out: dict[int, bool] = {}
    for v in variables:
        m = _IS_EN.match(key(v.name).replace(" ", "").replace("_", ""))
        if m:
            b = parse_state(v.value)
            if b is not None:
                out[int(m.group(1))] = bool(b)
    return out


def map_variables(variables: list[Variable], channel_map: dict | None = None) -> dict[str, Variable]:
    """canal del bot -> variable de SITRAD. channel_map {canal: nombre} manda sobre el reconocimiento automático."""
    out: dict[str, Variable] = {}
    for ch, idx in match_columns([v.label for v in variables]).items():
        out[ch] = variables[idx]
    for ch, want in (channel_map or {}).items():
        for v in variables:
            if key(v.name) == key(want) or key(v.label) == key(want):
                out[ch] = v
                break
    return out


# ----------------------------------------------------------------------
# Cliente HTTP
# ----------------------------------------------------------------------

def resolve_credentials(cfg: dict) -> tuple[str, str]:
    user = cfg.get("user") or os.environ.get("SITRAD_USER") or ""
    pwd = cfg.get("password") or os.environ.get("SITRAD_PASSWORD") or ""
    if (not user or not pwd) and sys.stdin is not None and sys.stdin.isatty():
        from getpass import getpass
        user = user or input("Usuario Sitrad: ").strip()
        pwd = pwd or getpass("Contraseña Sitrad: ")
    if not user or not pwd:
        raise SystemExit("ERROR: faltan las credenciales de SITRAD. Defínelas en la sección 'sitrad' de config.json "
                         "(user/password), en las variables de entorno SITRAD_USER y SITRAD_PASSWORD, o ejecuta "
                         "en una terminal para que las pregunte.")
    return user, pwd

class SitradApi:
    def __init__(self, base_url: str, user: str, password: str, verify_ssl: bool = False,
                 timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, password)
        self.session.verify = bool(verify_ssl)

        if not verify_ssl:
            # Certificado local autofirmado de SITRAD: no se valida (en producción, configura el certificado).
            from urllib3.exceptions import InsecureRequestWarning
            warnings.simplefilter("ignore", InsecureRequestWarning)

    def get_json(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout,
                            verify=self.session.verify)
        r.raise_for_status()
        # SITRAD declara un charset que no coincide; los bytes reales son UTF-8.
        r.encoding = "utf-8"
        data = r.json()
        if isinstance(data, dict) and data.get("status") not in (None, 200):
            raise RuntimeError(f"Respuesta inesperada de SITRAD: {str(data)[:200]}")
        return data
    def converters(self) -> list[dict]:
        """Convertidores RS-485/RS-232 configurados en SITRAD.
        Prueba varios endpoints y devuelve [] si ninguno responde (SITRAD PRO
        de esta instalación no siempre expone /converters)."""
        for path in ("/converters", "/converter", "/instruments/converters"):
            try:
                return list(self.get_json(path).get("results", []))
            except Exception:
                continue
        return []

    def alarms_of(self, instrument_id) -> list[dict]:
        """Alarmas configuradas de un instrumento (si el endpoint existe)."""
        for path in (f"/instruments/{instrument_id}/alarms",
                    f"/instruments/{instrument_id}/alarms/config"):
            try:
                return list(self.get_json(path).get("results", []))
            except Exception:
                continue
        return []
    def instruments(self) -> list[dict]:
        return list(self.get_json("/instruments").get("results", []))

    def values(self, instrument_id) -> object:
        return self.get_json(f"/instruments/{instrument_id}/values").get("results", [])

    def alarms(self, start: datetime, end: datetime) -> list[dict]:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        data = self.get_json("/alarms", params={"startDate": start.strftime(fmt), "endDate": end.strftime(fmt)})
        return list(data.get("results", []))

    def close(self) -> None:
        self.session.close()


# ----------------------------------------------------------------------
# Alarmas
# ----------------------------------------------------------------------

_ALARM_TEXT = ("description", "message", "alarmDescription", "text", "name", "alarmName", "type", "alarmType", "title")
_ALARM_INST = ("instrumentName", "instrument", "deviceName", "equipmentName")
_ALARM_DATE = ("startDate", "date", "dateTime", "timestamp", "start", "occurredAt", "createdAt", "startTime")


def describe_alarm(alarm: dict) -> dict:
    """Texto, instrumento y hora de una alarma de SITRAD (los nombres de campo se buscan, no se asumen)."""
    text = _first(alarm, _ALARM_TEXT)
    inst = _first(alarm, _ALARM_INST)
    if isinstance(inst, dict):
        inst = _first(inst, _NAME_KEYS)
    when = parse_datetime_any(_first(alarm, _ALARM_DATE))
    if not text:
        text = json.dumps({k: v for k, v in alarm.items() if k != "id"}, ensure_ascii=False)[:140]
    return {"id": str(alarm.get("id", "")), "text": str(text).strip(), "instrument": str(inst).strip() if inst else "",
            "when": when, "raw": alarm}


# ----------------------------------------------------------------------
# Fuente del bot
# ----------------------------------------------------------------------

class SitradApiSource:
    
    """Misma interfaz que CavaRandomSource: poll(now) -> Snapshot."""

    def __init__(self, api: SitradApi, device_id: int, instrument_id=None, instrument_name: str | None = None,
                 channel_map: dict | None = None, poll_seconds: float = 30.0, alarms_every_s: float = 60.0,
                 save_raw: bool = False, data_dir: str = "sitrad_data", announce_existing_alarms: bool = False) -> None:
        self.api = api
        self.device_id = int(device_id)
        self.instrument_id = instrument_id
        self.instrument_name = instrument_name
        self.channel_map = channel_map or {}
        self.poll_seconds = float(poll_seconds)
        self.alarms_every_s = float(alarms_every_s)
        self.save_raw = save_raw
        self.data_dir = Path(data_dir)
        self.announce_existing = announce_existing_alarms
        self._spec = {s.name: s for s in CONTROLLER_SPECS}
        self._last_fetch: datetime | None = None
        self._last_ts: dict[str, datetime] = {}
        self._fail_streak = 0
        self._alarm_seen: set[str] = set()
        self._alarm_primed = False
        self._last_alarm_fetch: datetime | None = None
        self.instrument: dict | None = None
        self.alarms_path = Path(data_dir) / "sitrad_alarms.jsonl"
        if save_raw:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        
    def _save_alarm(self, alarm: dict, raw: dict) -> None:
        """Persiste la alarma en logs/sitrad_alarms.jsonl (append-only, para consultas históricas)."""
        import json
        from datetime import datetime, timezone
        rec = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "id": alarm.get("id"),
            "instrument": alarm.get("instrument"),
            "text": alarm.get("text"),
            "when": alarm["when"].isoformat() if alarm.get("when") else None,
            "raw": raw,
        }
        try:
            self.alarms_path.parent.mkdir(parents=True, exist_ok=True)
            with self.alarms_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.exception("No pude persistir la alarma.")
    # ---- descubrimiento ----
    def discover(self) -> dict:
        """Elige el instrumento del controlador. Lanza RuntimeError con una explicación si no puede."""
        insts = self.api.instruments()
        if not insts:
            raise RuntimeError("SITRAD no devolvió ningún instrumento (¿credenciales o servidor correcto?).")
        scored = []
        for ins in insts:
            try:
                vs = list(iter_variables(self.api.values(ins["id"])))
                scored.append((ins, len(map_variables(vs, self.channel_map)), len(vs)))
            except Exception as exc:                                     # un instrumento caído no debe impedir el resto
                logger.warning("No pude leer el instrumento %s: %s", ins.get("id"), exc)
                scored.append((ins, 0, 0))
        table = "; ".join(f"{i.get('id')}='{i.get('name', '?')}' ({m} canales reconocidos de {n})" for i, m, n in scored)
        logger.info("Instrumentos en SITRAD: %s", table)
        if self.instrument_id is not None:
            sel = [i for i, _, _ in scored if str(i.get("id")) == str(self.instrument_id)]
        elif self.instrument_name:
            sel = [i for i, _, _ in scored if key(self.instrument_name) in key(i.get("name", ""))]
        else:
            cands = [i for i, m, _ in scored if m >= 3]
            if len(cands) > 1:
                raise RuntimeError("Hay varios instrumentos que parecen controladores de cámara: " + table +
                                   ". Indica cuál con sitrad.instrument_id o sitrad.instrument_name en config.json.")
            sel = cands
        if not sel:
            raise RuntimeError("No encontré el controlador de la cámara. Instrumentos: " + table +
                               ". Corre tools/inspect_sitrad_api.py para ver cómo se llaman sus variables, o indica "
                               "sitrad.instrument_id / sitrad.channel_map en config.json.")
        self.instrument = sel[0]
        self.instrument_id = self.instrument["id"]
        logger.info("Controlador elegido: id=%s nombre=%s", self.instrument_id, self.instrument.get("name"))
        return self.instrument

    # ---- lecturas ----
    def _reading(self, ch: str, v: Variable, t: datetime, sensor_flags: dict | None = None) -> ChannelReading:
        spec = self._spec[ch]
        idx = SENSOR_INDEX.get(ch)
        if idx is not None and sensor_flags and sensor_flags.get(idx) is False:
            return ChannelReading(self.device_id, ch, None, spec.unit, DataQuality.MISSING, t, raw=str(v.value))
        if spec.kind == "analog" and spec.unit == "°C":
            x = parse_number(v.value)
            if x is not None and not (TEMP_PLAUSIBLE[0] <= x <= TEMP_PLAUSIBLE[1]):
                return ChannelReading(self.device_id, ch, None, spec.unit, DataQuality.MISSING, t, raw=str(v.value))
        if not v.enabled:
            return ChannelReading(self.device_id, ch, None, spec.unit, DataQuality.MISSING, t, raw=str(v.value))
        val = parse_state(v.value) if spec.kind == "state" else parse_number(v.value)
        if val is None:
            return ChannelReading(self.device_id, ch, None, spec.unit, DataQuality.ERR if v.error else DataQuality.MISSING,
                                  t, raw=str(v.value))
        if v.error:
            return ChannelReading(self.device_id, ch, val, spec.unit, DataQuality.ERR, t, raw=str(v.value))
        return ChannelReading(self.device_id, ch, float(val), spec.unit, DataQuality.VALID, t, raw=str(v.value))

    def _save_raw(self, now: datetime, results) -> None:
        if not self.save_raw:
            return
        try:
            with (self.data_dir / "lecturas.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"collectedAt": now.isoformat(),
                                    "instruments": [{"instrument": self.instrument, "values": results}]},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def poll(self, now: datetime) -> Snapshot:
        empty = Snapshot(source_id="sitrad_api", processing_time=now, readings=())
        if self._last_fetch is not None and (now - self._last_fetch).total_seconds() < self.poll_seconds:
            return empty
        self._last_fetch = now
        try:
            if self.instrument_id is None:
                self.discover()
            results = self.api.values(self.instrument_id)
        except Exception as exc:
            self._fail_streak += 1
            if self._fail_streak in (1, 5) or self._fail_streak % 30 == 0:
                logger.warning("No pude leer SITRAD (intento %d): %s", self._fail_streak, exc)
            return empty
        if self._fail_streak:
            logger.info("SITRAD respondió de nuevo tras %d fallo(s).", self._fail_streak)
            self._fail_streak = 0
        self._save_raw(now, results)
        variables = list(iter_variables(results))
        mapped = map_variables(variables, self.channel_map)
        flags = sensor_enabled_flags(variables)
        out: list[ChannelReading] = []
        for ch, v in mapped.items():
            t = v.ts or now
            last = self._last_ts.get(ch)
            if v.ts is not None and last is not None and v.ts <= last:
                continue                                    # el instrumento aún no actualizó este dato
            self._last_ts[ch] = t
            out.append(self._reading(ch, v, t, flags))
        return Snapshot(source_id="sitrad_api", processing_time=now, readings=tuple(out))
    def _would_accept(self, ch: str, v: Variable, sensor_flags: dict | None) -> tuple[bool, str]:
        """Misma lógica que _reading pero devolviendo (aceptada, razón_si_no). Sin construir ChannelReading."""
        spec = self._spec.get(ch)
        if spec is None:
            return False, "canal no catalogado"
        if not v.enabled:
            return False, "deshabilitado en SITRAD"
        idx = SENSOR_INDEX.get(ch)
        if idx is not None and sensor_flags and sensor_flags.get(idx) is False:
            return False, f"sensor S{idx} deshabilitado"
        if spec.kind == "analog" and spec.unit == "°C":
            x = parse_number(v.value)
            if x is not None and not (TEMP_PLAUSIBLE[0] <= x <= TEMP_PLAUSIBLE[1]):
                return False, "fuera de rango físico"
        return True, ""

    def discover_all(self) -> dict:
        """Árbol completo SITRAD: convertidores → instrumentos → canales.

        No selecciona ni cambia el instrumento activo: es informativo. Pensado para
        la tool list_converters y para el log de arranque. Tolerante a fallos:
        cada instrumento que no responda se anota con error y el resto sigue.
        """
        tree: dict = {"converters": [], "unassigned_instruments": []}

        # 1. Convertidores (si el endpoint existe)
        try:
            raw_convs = self.api.converters()
        except Exception:
            raw_convs = []
        by_conv: dict[str, dict] = {}
        for c in raw_convs:
            cid = str(c.get("id", ""))
            if not cid:
                continue
            by_conv[cid] = {
                "id": cid,
                "name": c.get("name") or c.get("description") or f"Convertidor {cid}",
                "address": c.get("address"),
                "status": c.get("status"),
                "instruments": [],
            }

        # 2. Instrumentos
        try:
            instruments = self.api.instruments()
        except Exception as exc:
            logger.warning("discover_all: no pude listar instrumentos: %s", exc)
            instruments = []

        for ins in instruments:
            ins_id = ins.get("id")
            ins_name = ins.get("name") or f"Instrumento {ins_id}"
            conv_id = ins.get("converterId")
            conv_id = None if conv_id in (None, 0, "0", "") else str(conv_id)

            channels: list[dict] = []
            unmapped: list[str] = []
            n_vars = 0
            try:
                raw = self.api.values(ins_id)
                vs = list(iter_variables(raw))
                n_vars = len(vs)
                mapped = map_variables(vs, self.channel_map)
                flags = sensor_enabled_flags(vs)
                for ch, v in mapped.items():
                    ok, why = self._would_accept(ch, v, flags)
                    channels.append({
                        "canal": ch,
                        "variable": v.name,
                        "unit": v.unit,
                        "value": v.value,
                        "enabled": v.enabled,
                        "accepted": ok,
                        "reason": why,
                    })
                mapped_names = {c["variable"] for c in channels}
                unmapped = sorted(v.name for v in vs if v.name not in mapped_names)
            except Exception as exc:
                logger.warning("discover_all: no pude leer %s (id=%s): %s", ins_name, ins_id, exc)

            entry = {
                "id": ins_id,
                "name": ins_name,
                "model": ins.get("modelId") or ins.get("modelVersion") or "",
                "status": ins.get("status"),
                "address": ins.get("address"),
                "converterId": conv_id,
                "channels": channels,
                "n_channels_total": len(channels),
                "n_channels_effective": sum(1 for c in channels if c["accepted"]),
                "n_variables_total": n_vars,
                "unmapped_sample": unmapped[:15],
            }

            # ─── BLOQUE 3.2: catálogo de alarmas del instrumento ───
            alarm_channels = [
                {"canal": ch, "variable": v.name,
                "active": bool(parse_number(v.value)) if v.value is not None else None}
                for ch, v in mapped.items()
                if ch.startswith(("Alarma ", "Error Sensor"))
            ]
            entry["alarm_channels"] = alarm_channels
            entry["n_alarm_channels"] = len(alarm_channels)

            try:
                raw_alarms = self.api.get_json(f"/instruments/{ins_id}/alarms").get("results", [])
                entry["alarm_catalog"] = [
                    {"code": a.get("code"), "description": a.get("description"),
                    "inhibited": a.get("isInhibited"), "recognized": a.get("isRecognized")}
                    for a in raw_alarms
                ]
            except Exception:
                entry["alarm_catalog"] = []          # endpoint no disponible en esta versión de SITRAD
            # ─── FIN BLOQUE 3.2 ───

            if conv_id and conv_id in by_conv:
                by_conv[conv_id]["instruments"].append(entry)
            else:
                tree["unassigned_instruments"].append(entry)
            

        tree["converters"] = list(by_conv.values())
        tree["n_converters"] = len(tree["converters"])
        tree["n_instruments"] = len(instruments)
        tree["n_channels_total"] = (
            sum(len(i["channels"]) for c in tree["converters"] for i in c["instruments"])
            + sum(len(i["channels"]) for i in tree["unassigned_instruments"])
        )
        tree["n_channels_effective"] = (
            sum(i["n_channels_effective"] for c in tree["converters"] for i in c["instruments"])
            + sum(i["n_channels_effective"] for i in tree["unassigned_instruments"])
        )
        return tree
    
    # ---- alarmas ----
    def new_alarms(self, now: datetime) -> list[dict]:
        """Alarmas de SITRAD que no se habían entregado (descripción en lenguaje simple)."""
        if self._last_alarm_fetch is not None and (now - self._last_alarm_fetch).total_seconds() < self.alarms_every_s:
            return []
        self._last_alarm_fetch = now
        try:
            raw = self.api.alarms(now - timedelta(minutes=15), now)
        except Exception as exc:
            logger.warning("No pude consultar las alarmas de SITRAD: %s", exc)
            return []
        fresh = []
        for a in raw:
            aid = str(a.get("id", json.dumps(a, sort_keys=True, default=str)))
            if aid in self._alarm_seen:
                continue
            self._alarm_seen.add(aid)
            self._save_alarm(describe_alarm(a), a)
            fresh.append(describe_alarm(a))
        if not self._alarm_primed:
            self._alarm_primed = True
            if not self.announce_existing:
                return []                                   # al arrancar no se re-anuncia lo que ya estaba
        return fresh
