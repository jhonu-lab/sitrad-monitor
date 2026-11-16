# core.contracts
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class DataQuality(str, Enum):
    VALID = "VALID"
    ERR = "ERR"
    OFFLINE = "OFFLINE"
    MISSING = "MISSING"
    FROZEN = "FROZEN"


@dataclass(frozen=True)
class ChannelReading:
    """Una lectura individual de un canal."""

    device_id: int
    channel: str
    value: float | None
    unit: str
    quality: DataQuality
    event_time: datetime
    raw: str = ""

    def __post_init__(self) -> None:
        if self.value is None and self.quality not in {
            DataQuality.ERR,
            DataQuality.OFFLINE,
            DataQuality.MISSING,
        }:
            raise ValueError(
                "ChannelReading con value=None debe tener "
                "quality=ERR, OFFLINE o MISSING"
            )


@dataclass(frozen=True)
class Snapshot:
    """
    Conjunto inmutable de lecturas correspondientes a un instante.
    """

    source_id: str
    processing_time: datetime
    readings: tuple[ChannelReading, ...]
    schema_version: str = "1"


@dataclass(frozen=True)
class Change:
    """
    Cambio matemáticamente detectado en una serie.

    No contiene ninguna interpretación causal.
    """

    inicio: datetime
    fin: datetime
    tipo: str
    delta: float
    valor_inicial: float
    valor_final: float


@dataclass(frozen=True)
class EventCandidate:
    """
    Evento detectado por el motor de análisis.

    No representa todavía una causa.
    El agente será responsable de interpretar el patrón.
    """

    inicio: datetime
    fin: datetime
    duracion_min: float

    variables_afectadas: tuple[str, ...]

    magnitud: dict[str, float]

    patron: str

    evidencia: tuple[ChannelReading, ...]

    volvio_a_base: bool


@dataclass(frozen=True)
class Baseline:
    """
    Línea base histórica de una variable.
    """

    variable: str
    media: float
    desviacion_estandar: float
    periodo_horas: float

