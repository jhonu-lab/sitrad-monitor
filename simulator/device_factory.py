"""
Fábrica de payloads JSON con el formato exacto de Sitrad PRO.

Centraliza la construcción de instrumentos y sensores para:
  1. Evitar duplicación en scenarios.py.
  2. Servir como documentación ejecutable del contrato JSON.
  3. Facilitar añadir nuevos modelos Full Gauge (PCT, MT, RCK, etc.)
     sin tocar el resto del simulador.
"""
from typing import Optional, Union


class SensorType:
    """Categorías de sensor según la instrumentación Full Gauge."""
    TEMPERATURE = "temperature"
    PRESSURE = "pressure"
    PERCENTAGE = "percentage"


DEFAULT_UNITS = {
    SensorType.TEMPERATURE: "°C",
    SensorType.PRESSURE: "psi",
    SensorType.PERCENTAGE: "%",
}


def sensor(
    name: str,
    value: Optional[Union[float, str]],
    sensor_type: str = SensorType.TEMPERATURE,
    setpoint: Optional[float] = None,
    hysteresis: Optional[float] = None,
    unit: Optional[str] = None,
) -> dict:
    """Construye el dict JSON de un sensor tal como lo devuelve Sitrad."""
    return {
        "name": name,
        "value": value,
        "unit": unit or DEFAULT_UNITS.get(sensor_type, ""),
        "setpoint": setpoint,
        "hysteresis": hysteresis,
    }


def instrument(
    instrument_id: int,
    name: str,
    model: str,
    sensors: list,
    status: str = "Online",
) -> dict:
    """Construye el dict JSON completo de un instrumento."""
    return {
        "id": instrument_id,
        "name": name,
        "model": model,
        "status": status,
        "sensors": sensors,
    }


class DeviceFactory:
    """Helpers específicos por modelo de controlador Full Gauge."""

    @staticmethod
    def pct_3001_plus(instrument_id: int, name: str,
                      suction_psi: float, discharge_psi: float,
                      demand_pct: float, offline: bool = False) -> dict:
        """Controlador de presión PCT-3001 plus (3 canales)."""
        v = None if offline else suction_psi
        v2 = None if offline else discharge_psi
        v3 = None if offline else demand_pct

        return instrument(
            instrument_id=instrument_id, name=name, model="PCT-3001 plus",
            status="Offline" if offline else "Online",
            sensors=[
                sensor("Presión Succión", v, SensorType.PRESSURE, 50.0, 5.0),
                sensor("Presión Descarga", v2, SensorType.PRESSURE, 185.0, 10.0),
                sensor("Demanda", v3, SensorType.PERCENTAGE, 80, 10),
            ],
        )

    @staticmethod
    def mt_512e_log(instrument_id: int, name: str,
                    ambient_c: float, evaporator: Union[float, str],
                    product_c: float) -> dict:
        """Termostato MT-512E Log (3 canales NTC)."""
        return instrument(
            instrument_id=instrument_id, name=name, model="MT-512E Log",
            sensors=[
                sensor("Temp Ambiente", ambient_c,
                       SensorType.TEMPERATURE, -18.0, 2.0),
                sensor("Temp Evaporador", evaporator,
                       SensorType.TEMPERATURE, -22.0, 3.0),
                sensor("Temp Producto", product_c,
                       SensorType.TEMPERATURE, -18.0, 2.0),
            ],
        )

    @staticmethod
    def rck_862_plus(instrument_id: int, name: str,
                     line_psi: float, return_c: float) -> dict:
        """Rack RCK-862 plus (2 canales expuestos en la demo)."""
        return instrument(
            instrument_id=instrument_id, name=name, model="RCK-862 plus",
            sensors=[
                sensor("Presión Línea", line_psi,
                       SensorType.PRESSURE, 70.0, 3.0),
                sensor("Temp Retorno", return_c,
                       SensorType.TEMPERATURE, 5.0, 2.0),
            ],
        )