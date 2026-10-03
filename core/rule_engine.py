"""Motor de reglas. Convierte lecturas en alertas."""
from typing import List
from core.models import Alert, SensorReading, Severity


class RuleEngine:
    """4 reglas deterministas, sin IA, sin ambigüedad."""

    CRITICAL_MULT = 2.0

    def evaluate(self, instrument: dict) -> List[Alert]:
        alerts: List[Alert] = []
        dev_id = instrument["id"]
        dev_name = instrument["name"]

        # REGLA 1: Dispositivo Offline
        if instrument.get("status") == "Offline":
            alerts.append(Alert(
                severity=Severity.CRITICAL,
                device_id=dev_id, device_name=dev_name,
                sensor_name="DISPOSITIVO", alert_type="OFFLINE",
                value_detected="OFFLINE",
                suggested_action=(
                    "Verificar alimentación del controlador, cables RS-485 "
                    "y configuración de red en Sitrad PRO."
                ),
            ))
            return alerts  # No evaluar sensores si el device está offline

        # REGLA 2-4: Evaluar cada sensor
        for s in instrument.get("sensors", []):
            reading = SensorReading(
                name=s["name"], value=s.get("value"),
                unit=s.get("unit", ""), setpoint=s.get("setpoint"),
                hysteresis=s.get("hysteresis"), raw=str(s.get("value", "")),
            )

            # R2: sensor con "Err"
            if reading.is_error:
                alerts.append(Alert(
                    severity=Severity.CRITICAL,
                    device_id=dev_id, device_name=dev_name,
                    sensor_name=reading.name, alert_type="SENSOR_ERROR",
                    value_detected="Err",
                    suggested_action=(
                        "Verificar conexión física del sensor (NTC/Pt-100), "
                        "cables y terminales. Revisar si está dañado."
                    ),
                ))
                continue

            # R3/R4: fuera de rango
            if reading.is_out_of_range:
                dev = reading.deviation
                is_critical = (
                    dev is not None
                    and reading.hysteresis is not None
                    and dev > reading.hysteresis * self.CRITICAL_MULT
                )
                alerts.append(Alert(
                    severity=(Severity.CRITICAL if is_critical
                              else Severity.WARNING),
                    device_id=dev_id, device_name=dev_name,
                    sensor_name=reading.name, alert_type="OUT_OF_RANGE",
                    value_detected=(
                        f"{reading.value}{reading.unit} "
                        f"(SP {reading.setpoint}{reading.unit} "
                        f"±{reading.hysteresis}{reading.unit})"
                    ),
                    suggested_action=(
                        f"Desviación crítica de {dev:.1f}{reading.unit}. "
                        "Revisar sistema de refrigeración/compresión."
                        if is_critical else
                        "Monitorear evolución. Verificar setpoint."
                    ),
                ))
        return alerts