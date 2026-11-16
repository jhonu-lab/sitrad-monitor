from dataclasses import dataclass

@dataclass(frozen=True)
class Capability:
    name: str                # "compresor", "puerta", "evaporador", "multipower_kw", "presostato_alta", ...
    available: bool          # ¿el canal existe en el instrumento?
    enabled: bool            # ¿el canal llega con datos válidos?
    channel: str | None      # canal del bot al que se mapea (o None)
    reason: str = ""         # "sensor S2 deshabilitado", "no existe en el modelo", "enabled=False"

@dataclass
class InstrumentCapabilities:
    instrument_id: str
    instrument_name: str
    model: str                       # "TC-900E" (deducido del nombre)
    channels: dict[str, Capability]  # canal del bot → capability
    alarms: dict[str, Capability]    # alarma interna → capability
    uncovered_hypotheses: list[str]  # ["baja_tension", ...]

    def can(self, name: str) -> bool:
        c = self.channels.get(name) or self.alarms.get(name)
        return bool(c and c.available and c.enabled)