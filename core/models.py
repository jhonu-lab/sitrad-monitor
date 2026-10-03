"""Dataclasses del dominio. Independientes del origen de datos."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Severity(Enum):
    CRITICAL = "CRÍTICO"
    WARNING = "ADVERTENCIA"
    RECOVERY = "RECUPERACIÓN"


@dataclass
class SensorReading:
    name: str
    value: Optional[float]
    unit: str
    setpoint: Optional[float] = None
    hysteresis: Optional[float] = None
    raw: str = ""

    @property
    def is_error(self) -> bool:
        return isinstance(self.raw, str) and self.raw.strip().upper() == "ERR"

    @property
    def is_out_of_range(self) -> bool:
        if self.value is None or self.setpoint is None or self.hysteresis is None:
            return False
        return abs(self.value - self.setpoint) > self.hysteresis

    @property
    def deviation(self) -> Optional[float]:
        if self.value is None or self.setpoint is None:
            return None
        return abs(self.value - self.setpoint)


@dataclass
class Alert:
    severity: Severity
    device_id: int
    device_name: str
    sensor_name: str
    alert_type: str       # "SENSOR_ERROR" | "OFFLINE" | "OUT_OF_RANGE"
    value_detected: str
    suggested_action: str
    timestamp: datetime = field(default_factory=datetime.now)

    def format_message(self) -> str:
        icon = {"CRÍTICO": "🔴", "ADVERTENCIA": "🟡",
                "RECUPERACIÓN": "🟢"}[self.severity.value]
        return (
            f"{icon} [{self.severity.value}] | {self.device_name}\n"
            f"   Sensor: {self.sensor_name}\n"
            f"   Valor:  {self.value_detected}\n"
            f"   Hora:   {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"   Acción: {self.suggested_action}"
        )

    def format_oneline(self) -> str:
        """Formato compacto para tablas/listados."""
        icon = {"CRÍTICO": "🔴", "ADVERTENCIA": "🟡",
                "RECUPERACIÓN": "🟢"}[self.severity.value]
        return (f"{icon} {self.severity.value} · {self.device_name} · "
                f"{self.sensor_name} · {self.value_detected}")

    @property
    def key(self) -> str:
        """Clave única para deduplicación."""
        return f"{self.device_id}:{self.sensor_name}:{self.alert_type}"