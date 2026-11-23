"""
core/channel_catalog.py — qué se extrae de cada instrumento SITRAD.

Derivado de los dos informes reales:
    - Controlador MT-512E Log  (1 temperatura, setpoint, salidas digitales)
    - Multipower, 2 medidores trifásicos M1 y M2 (153 columnas)

Principios
    * Un nombre canónico ÚNICO por canal ("M1 Potencia Activa"), porque las
      herramientas del agente buscan por nombre exacto en UN device_id.
    * kind:  analog  -> serie continua
             state   -> 0/1 (Conectado/Desconectado)
             counter -> acumulado monótono (kWh); NO se correlaciona ni se
                        promedia, se resta (fin - inicio).
    * role: nombre lógico independiente del instrumento, para que las reglas
      de diagnóstico no dependan del nombre físico.
    * Se excluyen: constantes (neutro, PF por fase, "suministrada"), columnas
      por fase redundantes con el total, y texto vacío.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    source_col: str          # columna SITRAD normalizada (espacios colapsados)
    unit: str
    kind: str                # analog | state | counter
    instrument: str          # controlador | multipower
    role: str | None = None


def norm_col(col: str) -> str:
    """'Potencia Activa Total  (kW)' -> 'Potencia Activa Total (kW)'."""
    return re.sub(r"\s+", " ", str(col)).strip()


# ----------------------------------------------------------------------
# Controlador
# ----------------------------------------------------------------------
CONTROLLER_SPECS: tuple[ChannelSpec, ...] = (
    # --- temperatura y consigna ---
    ChannelSpec("Temp Cámara", "Ambiente (°C)", "°C", "analog", "controlador", "temp_principal"),
    ChannelSpec("Setpoint", "Setpoint actual (°C)", "°C", "analog", "controlador", "setpoint"),
    ChannelSpec("Desvío vs Setpoint", "Desvío relativo al Setpoint (°C)", "°C", "analog", "controlador", "desvio"),
    ChannelSpec("Diferencial", "CurrentDifferential (°C)", "°C", "analog", "controlador", "diferencial"),
    # --- salidas ---
    ChannelSpec("Estado Refrigeración", "Salida REFR", "estado", "state", "controlador", "compresor"),
    ChannelSpec("Estado Deshielo", "Salida DEFR", "estado", "state", "controlador", "deshielo"),
    ChannelSpec("Estado Ventilador", "Salida FANS", "estado", "state", "controlador", "ventilador"),
    ChannelSpec("Puerta Abierta", "Puerta abierta", "estado", "state", "controlador", "puerta"),
    # --- sondas opcionales (deshabilitables) ---
    ChannelSpec("Temp Evaporador", "Evaporador (°C)", "°C", "analog", "controlador", "evaporador"),
    # --- datalogger interno del controlador (el buffer circular puede estar lleno) ---
    ChannelSpec("Datalogger Activo", "DataloggerOn", "estado", "state", "controlador", "datalogger"),
    ChannelSpec("Datalogger Lleno", "IsDataloggerFull", "estado", "state", "controlador", "datalogger_lleno"),
    ChannelSpec("Datalogger Uso", "DataloggerPercentUsage (%)", "%", "analog", "controlador", "datalogger_uso"),
    ChannelSpec("Datalogger Ya Sobrescribió", "DataloggerAlreadyOverwrote", "estado", "state", "controlador",
                "datalogger_overwrote"),
    # --- proceso y mantenimiento ---
    ChannelSpec("Horas Compresor", "Hourmeter", "h", "counter", "controlador", "horas"),
    # --- alarmas del propio controlador ---
    ChannelSpec("Alarma Temp Alta", "IsAlrHighTempS1", "estado", "state", "controlador", "alr_temp_alta"),
    ChannelSpec("Alarma Temp Baja", "IsAlrLowTempS1", "estado", "state", "controlador", "alr_temp_baja"),
    ChannelSpec("Alarma Puerta", "IsAlrmDoorOpn", "estado", "state", "controlador", "alr_puerta"),
    ChannelSpec("Alarma Deshielo Timeout", "IsDefrTimeOut", "estado", "state", "controlador", "alr_deshielo"),
    ChannelSpec("Error Sensor 1", "IsErrorS1", "estado", "state", "controlador", "err_s1"),
    ChannelSpec("Error Sensor 2", "IsErrorS2", "estado", "state", "controlador", "err_s2"),
    ChannelSpec("Error Sensor 3", "IsErrorS3", "estado", "state", "controlador", "err_s3"),
    # --- configuración útil como referencia ---
    ChannelSpec("Límite Alarma Temp", "FncSensor1HighAlarmLimit (°C)", "°C", "analog", "controlador", "limite_alarma"),
    ChannelSpec("Duración Deshielo Config", "FncMaxDefrostDuration (min)", "min", "analog", "controlador", "deshielo_max"),
)

# ----------------------------------------------------------------------
# Multipower (M1, M2)
# ----------------------------------------------------------------------

# (columna SITRAD con {m} = M1/M2 e {i} = fase, nombre canónico, unidad, kind, rol)
_MP_TEMPLATES = (
    ("Tensión de la Fase {i} (Vac)", "{m} Tensión F{i}", "V", "analog", None),
    ("Corriente de la fase {i} (A)", "{m} Corriente F{i}", "A", "analog", None),
    ("Potencia Activa Total (kW)", "{m} Potencia Activa", "kW", "analog", "kw"),
    ("Potencia Reactiva Total (kVAr)", "{m} Potencia Reactiva", "kVAr", "analog", None),
    ("Potencia Aparente Total (kVA)", "{m} Potencia Aparente", "kVA", "analog", "kva"),
    ("Factor de Potencia Total (PF)", "{m} Factor de Potencia", "PF", "analog", "pf"),
    ("Frecuencia (Hz)", "{m} Frecuencia", "Hz", "analog", None),
    ("Simetría modular de tensión (%)", "{m} Simetría Tensión", "%", "analog", None),
    ("Simetría modular de corriente (%)", "{m} Simetría Corriente", "%", "analog", None),
    ("Demanda Activa Total (kW)", "{m} Demanda Activa", "kW", "analog", None),
    ("Energia Activa Consumida Total (kWh)", "{m} Energía Activa", "kWh", "counter", "energia"),
)


def multipower_specs(meters: tuple[str, ...] = ("M1", "M2")) -> tuple[ChannelSpec, ...]:
    out: list[ChannelSpec] = []
    for m in meters:
        for src, name, unit, kind, role in _MP_TEMPLATES:
            phases = (1, 2, 3) if "{i}" in src else (None,)
            for i in phases:
                s = f"{m}: " + (src.format(i=i) if i else src)
                n = name.format(m=m, i=i) if i else name.format(m=m)
                r = f"{role}_{m.lower()}" if role else None
                out.append(ChannelSpec(n, norm_col(s), unit, kind, "multipower", r))
    return tuple(out)


MULTIPOWER_SPECS = multipower_specs()
ALL_SPECS = CONTROLLER_SPECS + MULTIPOWER_SPECS

SPEC_BY_NAME = {s.name: s for s in ALL_SPECS}
ROLE_TO_CHANNEL = {s.role: s.name for s in ALL_SPECS if s.role}


def channels_config(specs=ALL_SPECS) -> list[dict]:
    """Lista lista para `cava_colanta.channels` de config.json."""
    return [{"name": s.name, "unit": s.unit, "kind": s.kind, "instrument": s.instrument}
            for s in specs]
def format_channels(names, max_names: int = 4) -> str:
    """Pocos canales: sus nombres. Muchos: resumen por instrumento."""
    chs = sorted(names)
    if len(chs) <= max_names:
        return ", ".join(chs)
    groups: dict[str, int] = {}
    for c in chs:
        spec = SPEC_BY_NAME.get(c)
        if spec is None:
            label = "otros"
        elif spec.instrument == "multipower":
            label = f"Multipower {c.split(' ')[0]}"
        else:
            label = spec.instrument.capitalize()
        groups[label] = groups.get(label, 0) + 1
    return ", ".join(f"{g}: {n}" for g, n in groups.items())



def detect_instrument_type(labels: list[str]) -> str:
    """Clasifica un instrumento a partir de los NOMBRES SITRAD de sus variables.

    - multipower: tiene 'potencia activa' y referencias a M1/M2.
    - tc900e:     tiene 'ambiente' y alguna de las salidas (refr, defr, fans).
    - generic:    cualquier otro (sensor suelto, etc.).
    """
    text = " ".join(norm_col(lbl).lower() for lbl in labels)
    if "potencia activa" in text and ("m1" in text or "m2" in text):
        return "multipower"
    if "ambiente" in text and ("salida refr" in text or "salida defr" in text or "salida fans" in text):
        return "tc900e"
    return "generic"


def channels_for_type(instrument_type: str) -> list[dict]:
    """Canales esperados para cada tipo (lista lista para config)."""
    if instrument_type == "tc900e":
        return channels_config(CONTROLLER_SPECS)
    if instrument_type == "multipower":
        return channels_config(MULTIPOWER_SPECS)
    return []  # generic: solo passthrough


def channel_map_for_type(instrument_type: str) -> dict:
    """channel_map por defecto para cada tipo."""
    if instrument_type == "tc900e":
        return {
            "Temp Cámara": "Ambiente",
            "Temp Cámara (alt)": "Temperatura",
            "Estado Refrigeración": "Salida REFR",
            "Estado Deshielo": "Salida DEFR",
            "Estado Ventilador": "Salida FANS",
            "Temp Evaporador": "Evaporador",
            "Puerta Abierta": "Puerta abierta",
            "Horas Compresor": "Hourmeter",
            "Alarma Temp Alta": "IsAlrHighTempS1",
            "Alarma Temp Baja": "IsAlrLowTempS1",
            "Alarma Puerta": "IsAlrmDoorOpn",
            "Alarma Deshielo Timeout": "IsDefrTimeOut",
            "Error Sensor 1": "IsErrorS1",
            "Error Sensor 2": "IsErrorS2",
            "Error Sensor 3": "IsErrorS3",
            "Límite Alarma Temp": "FncSensor1HighAlarmLimit",
            "Duración Deshielo Config": "FncMaxDefrostDuration",
            "Datalogger Activo": "DataloggerOn",
            "Datalogger Lleno": "IsDataloggerFull",
            "Datalogger Uso": "DataloggerPercentUsage",
            "Datalogger Ya Sobrescribió": "DataloggerAlreadyOverwrote",
        }
    return {}

# ----------------------------------------------------------------------
# Canales "catalogados" (conocidos) frente a passthrough
# ----------------------------------------------------------------------
# Un lugar puede agrupar varios instrumentos; si dos traen el mismo canal,
# el segundo se publica como "Canal [Nombre del instrumento]".
_SUFFIX_RE = re.compile(r"\s+\[[^\]]+\]$")


def base_channel(name: str) -> str:
    """'Temp Cámara [Cava 2]' -> 'Temp Cámara'."""
    return _SUFFIX_RE.sub("", str(name))


def catalog_names(extra=()) -> frozenset:
    """Nombres canónicos conocidos: catálogo + channel_map por defecto + los que añada el config."""
    names = set(SPEC_BY_NAME)
    names.update(channel_map_for_type("tc900e"))
    names.update(str(x) for x in (extra or ()))
    return frozenset(names)


def make_catalog_predicate(extra=()):
    """Devuelve f(canal) -> True si el canal es catalogado (no passthrough)."""
    names = catalog_names(extra)

    def is_catalogued(channel: str) -> bool:
        return channel in names or base_channel(channel) in names

    return is_catalogued
