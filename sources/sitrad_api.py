"""
sources/sitrad_api.py — el bot leyendo la API local de SITRAD PRO en vivo.

Endpoints usados (solo lectura):
  GET /instruments                      -> instrumentos del servidor
  GET /converters                       -> convertidores (si existen)
  GET /instruments/{id}/values          -> variables y valores de un instrumento
  GET /alarms?startDate=..&endDate=..   -> alarmas configuradas en SITRAD que se han disparado

Comportamiento:
  * Credenciales desde config / entorno / prompt.
  * Descubre el controlador y opcionalmente construye el árbol completo
    convertidores → instrumentos → canales.
  * Passthrough: además de los canales del catálogo, ingesta al store
    cualquier variable con valor válido, usando su nombre SITRAD como
    nombre de canal. Los canales del catálogo se mapean primero; los demás
    se añaden sin filtros de plausibilidad (el bot no sabe qué son).
  * Deduplicación por timestamp: si el instrumento no actualizó, no reingesta.
  * Alarmas nuevas se entregan una sola vez (new_alarms) y se persisten en
    sitrad_data/sitrad_alarms.jsonl.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

from core.channel_catalog import CONTROLLER_SPECS, norm_col
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


def map_variables(
    variables: list[Variable],
    channel_map: dict | None = None,
    passthrough: bool = False,
) -> dict[str, Variable]:
    """canal del bot -> variable de SITRAD.

    Orden de prioridad:
      1. match_columns (catálogo canónico: CONTROLLER_SPECS / MULTIPOWER_SPECS).
      2. channel_map del config (mapea nombres SITRAD concretos a canales del bot).
      3. Si passthrough=True: cualquier variable no mapeada se entrega con su
         nombre SITRAD normalizado como nombre de canal. Estas variables NO
         se filtran por plausibilidad térmica (el bot no sabe qué son).
    """
    out: dict[str, Variable] = {}

    # 1. Catálogo
    try:
        idx_map = match_columns([v.label for v in variables])
    except Exception:
        idx_map = {}
    for ch, idx in idx_map.items():
        if 0 <= idx < len(variables):
            out[ch] = variables[idx]

    # 2. channel_map
    for ch, want in (channel_map or {}).items():
        for v in variables:
            if key(v.name) == key(want) or key(v.label) == key(want):
                out[ch] = v
                break

    # 3. Passthrough
    if passthrough:
        mapped_ids = {id(v) for v in out.values()}
        existing_channels = set(out.keys())
        for v in variables:
            if id(v) in mapped_ids:
                continue
            if v.value is None or v.error or not v.enabled:
                continue
            ch = norm_col(v.name).strip()
            if not ch or ch in existing_channels:
                continue
            out[ch] = v

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
    """Cliente HTTP contra la API local de SITRAD PRO. Solo lectura."""

    def __init__(self, base_url: str, user: str, password: str,
                 verify_ssl: bool = False, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, password)
        self.session.verify = bool(verify_ssl)
        if not verify_ssl:
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
        Prueba varios endpoints y devuelve [] si ninguno responde."""
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
        data = self.get_json("/alarms", params={"startDate": start.strftime(fmt),
                                                "endDate": end.strftime(fmt)})
        return list(data.get("results", []))

    def close(self) -> None:
        self.session.close()


# ----------------------------------------------------------------------
# Alarmas
# ----------------------------------------------------------------------

_ALARM_TEXT = ("description", "message", "alarmDescription", "text", "name", "alarmName", "type", "alarmType", "title")
_ALARM_INST = ("instrumentName", "instrument", "deviceName", "equipmentName")
_ALARM_DATE = ("startDate", "date", "dateTime", "timestamp", "start", "occurredAt", "createdAt", "startTime")

def describe_alarm(alarm: dict, names: dict | None = None) -> dict:
    text = _first(alarm, _ALARM_TEXT)
    inst = _first(alarm, _ALARM_INST)
    if not inst and names:
        iid = alarm.get("instrumentId")
        if iid is not None:
            inst = names.get(int(iid))
    if isinstance(inst, dict):
        inst = _first(inst, _NAME_KEYS)
    when = parse_datetime_any(_first(alarm, _ALARM_DATE))
    if not text:
        text = json.dumps({k: v for k, v in alarm.items() if k != "id"}, ensure_ascii=False)[:140]
    return {
        "id": str(alarm.get("id", "")),
        "text": str(text).strip(),
        "instrument": str(inst).strip() if inst else "",
        "when": when,
        "raw": alarm,
    }


# ----------------------------------------------------------------------
# Fuente del bot
# ----------------------------------------------------------------------

class SitradApiSource:
    """Misma interfaz que CavaRandomSource: poll(now) -> Snapshot."""

    def __init__(self, api: SitradApi, device_id: int, instrument_id=None,
                 instrument_name: str | None = None, channel_map: dict | None = None,
                 poll_seconds: float = 30.0, alarms_every_s: float = 60.0,
                 save_raw: bool = False, data_dir: str = "sitrad_data",
                 announce_existing_alarms: bool = False,
                 passthrough: bool = True) -> None:
        self.api = api
        self.device_id = int(device_id)
        self.instrument_id = instrument_id
        self._instrument_names: dict[int, str] = {}
        self.instrument_name = instrument_name
        self.channel_map = channel_map or {}
        self.poll_seconds = float(poll_seconds)
        self.alarms_every_s = float(alarms_every_s)
        self.save_raw = save_raw
        self.data_dir = Path(data_dir)
        self.announce_existing = announce_existing_alarms
        self.passthrough = bool(passthrough)
        self._spec = {s.name: s for s in CONTROLLER_SPECS}
        self._last_fetch: datetime | None = None
        self._last_ts: dict[str, datetime] = {}
        self._fail_streak = 0
        self._alarm_seen: set[str] = set()
        self._alarm_primed = False
        self._last_alarm_fetch: datetime | None = None
        self.instrument: dict | None = None
        self.alarms_path = self.data_dir / "sitrad_alarms.jsonl"
        if save_raw:
            self.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Persistencia de alarmas
    # ------------------------------------------------------------------

    def _save_alarm(self, alarm: dict, raw: dict) -> None:
        """Persiste la alarma en sitrad_data/sitrad_alarms.jsonl (append-only)."""
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

    # ------------------------------------------------------------------
    # Descubrimiento del controlador (instrumento activo)
    # ------------------------------------------------------------------

    def discover(self) -> dict:
        """Elige el instrumento del controlador. Lanza RuntimeError con una explicación si no puede."""
        insts = self.api.instruments()
        if not insts:
            raise RuntimeError("SITRAD no devolvió ningún instrumento (¿credenciales o servidor correcto?).")
        scored = []
        for ins in insts:
            self._instrument_names[int(ins.get("id"))] = ins.get("name") or f"Instrumento {ins.get('id')}"
            try:
                vs = list(iter_variables(self.api.values(ins["id"])))
                scored.append((ins, len(map_variables(vs, self.channel_map)), len(vs)))
            except Exception as exc:
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

    # ------------------------------------------------------------------
    # Lectura de un canal
    # ------------------------------------------------------------------

    def _infer_spec(self, ch: str, v: Variable):
        """Spec sintética para canales passthrough (no catalogados)."""
        from core.channel_catalog import ChannelSpec
        unit = (v.unit or "").strip()
        raw = v.value
        is_state = False
        if isinstance(raw, bool):
            is_state = True
        elif isinstance(raw, (int, float)) and float(raw) in (0.0, 1.0) and unit in ("", "estado"):
            is_state = True
        kind = "state" if is_state else "analog"
        return ChannelSpec(ch, v.name, unit, kind, "otro", None)

    def _reading(self, ch: str, v: Variable, t: datetime,
                 sensor_flags: dict | None = None) -> ChannelReading:
        spec = self._spec.get(ch)
        if spec is None:
            # Canal passthrough: sin filtros de plausibilidad ni de sensor,
            # solo sanidad básica (valor parseable).
            spec = self._infer_spec(ch, v)
            if not v.enabled:
                return ChannelReading(self.device_id, ch, None, spec.unit,
                                      DataQuality.MISSING, t, raw=str(v.value))
            val = parse_state(v.value) if spec.kind == "state" else parse_number(v.value)
            if val is None:
                return ChannelReading(self.device_id, ch, None, spec.unit,
                                      DataQuality.ERR if v.error else DataQuality.MISSING,
                                      t, raw=str(v.value))
            if v.error:
                return ChannelReading(self.device_id, ch, val, spec.unit,
                                      DataQuality.ERR, t, raw=str(v.value))
            return ChannelReading(self.device_id, ch, float(val), spec.unit,
                                  DataQuality.VALID, t, raw=str(v.value))

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
            return ChannelReading(self.device_id, ch, None, spec.unit,
                                  DataQuality.ERR if v.error else DataQuality.MISSING,
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

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

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
        mapped = map_variables(variables, self.channel_map, passthrough=self.passthrough)
        flags = sensor_enabled_flags(variables)
        out: list[ChannelReading] = []
        for ch, v in mapped.items():
            t = v.ts or now
            last = self._last_ts.get(ch)
            if v.ts is not None and last is not None and v.ts <= last:
                continue  # el instrumento aún no actualizó este dato
            self._last_ts[ch] = t
            out.append(self._reading(ch, v, t, flags))
        return Snapshot(source_id="sitrad_api", processing_time=now, readings=tuple(out))

    # ------------------------------------------------------------------
    # Filtro de aceptación (para discover_all)
    # ------------------------------------------------------------------

    def _would_accept(self, ch: str, v: Variable,
                      sensor_flags: dict | None) -> tuple[bool, str]:
        """Misma lógica que _reading pero devolviendo (aceptada, razón_si_no)."""
        spec = self._spec.get(ch)
        if spec is None:
            # Passthrough
            if not v.enabled:
                return False, "deshabilitado en SITRAD"
            if v.value is None or v.error:
                return False, "sin valor"
            return True, ""
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

    # ------------------------------------------------------------------
    # Árbol completo (para la tool list_converters y el log de arranque)
    # ------------------------------------------------------------------

    def discover_all(self) -> dict:
        """Árbol completo SITRAD: convertidores → instrumentos → canales.

        Incluye tanto los canales catalogados como los passthrough.
        No selecciona ni cambia el instrumento activo: es informativo.
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
            unmapped_names: list[str] = []
            n_vars = 0
            n_catalogados = 0
            n_passthrough = 0

            try:
                raw = self.api.values(ins_id)
                vs = list(iter_variables(raw))
                n_vars = len(vs)
                flags = sensor_enabled_flags(vs)

                # 2a. Canales del catálogo
                mapped = map_variables(vs, self.channel_map, passthrough=False)
                mapped_var_ids = {id(v) for v in mapped.values()}
                catalog_var_names = {v.name for v in mapped.values()}

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
                        "catalogado": True,
                    })
                    n_catalogados += 1

                # 2b. Canales passthrough
                existing_channels = {c["canal"] for c in channels}
                for v in vs:
                    if id(v) in mapped_var_ids:
                        continue
                    if v.value is None or v.error or not v.enabled:
                        continue
                    ch = norm_col(v.name).strip()
                    if not ch or ch in existing_channels:
                        continue
                    ok, why = self._would_accept(ch, v, flags)
                    channels.append({
                        "canal": ch,
                        "variable": v.name,
                        "unit": v.unit,
                        "value": v.value,
                        "enabled": v.enabled,
                        "accepted": ok,
                        "reason": why,
                        "catalogado": False,
                    })
                    existing_channels.add(ch)
                    n_passthrough += 1

                # Sample de variables que no están ni en el catálogo ni en channel_map.
                # Útil para ver qué se ha catalogado y qué es ruido.
                unmapped_names = sorted(
                    v.name for v in vs if v.name not in catalog_var_names
                )[:15]

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
                "n_channels_catalogados": n_catalogados,
                "n_channels_passthrough": n_passthrough,
                "n_variables_total": n_vars,
                "unmapped_sample": unmapped_names,
            }

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
        tree["n_channels_catalogados"] = (
            sum(i.get("n_channels_catalogados", 0) for c in tree["converters"] for i in c["instruments"])
            + sum(i.get("n_channels_catalogados", 0) for i in tree["unassigned_instruments"])
        )
        tree["n_channels_passthrough"] = (
            sum(i.get("n_channels_passthrough", 0) for c in tree["converters"] for i in c["instruments"])
            + sum(i.get("n_channels_passthrough", 0) for i in tree["unassigned_instruments"])
        )
        return tree

    # ------------------------------------------------------------------
    # Alarmas nuevas
    # ------------------------------------------------------------------

    def new_alarms(self, now: datetime) -> list[dict]:
        """Alarmas de SITRAD que no se habían entregado."""
        if self._last_alarm_fetch is not None and (now - self._last_alarm_fetch).total_seconds() < self.alarms_every_s:
            return []
        self._last_alarm_fetch = now
        desc = describe_alarm(a, self._instrument_names)
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
            desc = describe_alarm(a)
            self._save_alarm(desc, a)
            fresh.append(desc)
        if not self._alarm_primed:
            self._alarm_primed = True
            if not self.announce_existing:
                return []  # al arrancar no se re-anuncia lo que ya estaba
        return fresh