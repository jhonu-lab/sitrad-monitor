from datetime import datetime, timedelta, timezone

from analysis.diagnosis import diagnose
from core.contracts import ChannelReading as R, DataQuality as Q

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


def _s(ch, f, unit="°C", n=40):
    return [R(201, ch, f(i), unit, Q.VALID, T0 + timedelta(minutes=2 * i)) for i in range(n)]


def _base(door=0.0):
    return {"Temp Cámara": _s("Temp Cámara", lambda i: -1 + 0.05 * i),
            "Setpoint": _s("Setpoint", lambda i: -2.0),
            "Estado Refrigeración": _s("Estado Refrigeración", lambda i: 1.0, "estado"),
            "Estado Deshielo": _s("Estado Deshielo", lambda i: 0.0, "estado"),
            "Puerta Abierta": _s("Puerta Abierta", lambda i: door, "estado"),
            "Temp Evaporador": _s("Temp Evaporador", lambda i: -12 + 0.1 * i),
            "Estado Ventilador": _s("Estado Ventilador", lambda i: 1.0, "estado")}


def test_capacidad_reducida_confident():
    b = diagnose(_base())
    assert b.principal == "capacidad_reducida" and b.confianza >= 0.6
    assert b.hipotesis[0].faltan


def test_fan_off_is_not_capacidad_reducida():
    s = _base()
    s["Estado Ventilador"] = _s("Estado Ventilador", lambda i: 0.0, "estado")
    assert all(h.nombre != "capacidad_reducida" for h in diagnose(s).hipotesis)
