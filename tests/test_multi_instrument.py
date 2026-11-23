"""Tests de regresión para el refactor multi-instrumento."""

from __future__ import annotations



from core.channel_catalog import (
    detect_instrument_type,
    channels_for_type,
    channel_map_for_type,
)
from sources.sitrad_api import (
    InstrumentRegistry, describe_alarm,
)


def test_detect_tc900e():
    labels = ["Ambiente (°C)", "Salida REFR", "Salida DEFR", "Setpoint actual (°C)"]
    assert detect_instrument_type(labels) == "tc900e"


def test_detect_multipower():
    labels = ["M1: Potencia Activa Total (kW)", "M2: Potencia Activa Total (kW)",
              "M1: Energia Activa Consumida Total (kWh)"]
    assert detect_instrument_type(labels) == "multipower"


def test_detect_generic():
    assert detect_instrument_type(["Sensor X (°C)", "Otra cosa"]) == "generic"


def test_channels_for_type_tc900e():
    chans = channels_for_type("tc900e")
    names = {c["name"] for c in chans}
    assert "Temp Cámara" in names
    assert "Estado Refrigeración" in names


def test_channels_for_type_multipower():
    chans = channels_for_type("multipower")
    names = {c["name"] for c in chans}
    assert any("M1" in n and "Potencia Activa" in n for n in names)
    assert any("M2" in n and "Energía Activa" in n for n in names)


def test_channel_map_for_type_tc900e():
    m = channel_map_for_type("tc900e")
    assert m["Temp Cámara"] == "Ambiente"
    assert m["Estado Refrigeración"] == "Salida REFR"


# ---------------------------------------------------------------------------
# describe_alarm — resolución de instrument_id → nombre
# ---------------------------------------------------------------------------

def test_describe_alarm_resolves_instrument_id():
    alarm = {
        "id": 42,
        "instrumentId": 5,
        "description": "Puerta abierta",
        "startDate": "2026-11-22T10:00:00Z",
    }
    desc = describe_alarm(alarm, {5: "Medidor 1"})
    assert desc["instrument"] == "Medidor 1"
    assert desc["instrument_id"] == 5
    assert desc["text"] == "Puerta abierta"


def test_describe_alarm_without_names_keeps_id():
    alarm = {"id": 1, "instrumentId": 99, "description": "X"}
    desc = describe_alarm(alarm, None)
    assert desc["instrument"] == ""
    assert desc["instrument_id"] == 99


def test_describe_alarm_with_explicit_instrument_name():
    alarm = {
        "id": 1,
        "instrumentId": 5,
        "instrumentName": "Cava Directa",
        "description": "Alarma",
    }
    desc = describe_alarm(alarm, {5: "Otro"})
    assert desc["instrument"] == "Cava Directa"
    assert desc["instrument_id"] == 5


# ---------------------------------------------------------------------------
# InstrumentRegistry — clasifica un TC-900E y un Multipower
# ---------------------------------------------------------------------------

class _FakeApi:
    def __init__(self, instruments, values_by_id, converters=None):
        self._instruments = instruments
        self._values_by_id = values_by_id
        self._converters = converters or []

    def instruments(self):
        return list(self._instruments)

    def values(self, iid):
        return self._values_by_id[iid]

    def converters(self):
        return list(self._converters)


def _tc900e_vars():
    return [
        {"code": "Ambiente", "value": 2.5, "unit": "°C"},
        {"code": "Salida REFR", "value": 1},
        {"code": "Salida DEFR", "value": 0},
        {"code": "Setpoint actual", "value": 2.0, "unit": "°C"},
    ]


def _multipower_vars():
    return [
        {"code": "M1: Potencia Activa Total (kW)", "value": 1.5, "unit": "kW"},
        {"code": "M2: Potencia Activa Total (kW)", "value": 1.2, "unit": "kW"},
        {"code": "M1: Energia Activa Consumida Total (kWh)", "value": 100.0, "unit": "kWh"},
    ]


def test_registry_classifies_both_types():
    api = _FakeApi(
        instruments=[
            {"id": 1, "name": "Cava 1", "converterId": "c1"},
            {"id": 5, "name": "Medidor 1", "converterId": "c1"},
        ],
        values_by_id={1: _tc900e_vars(), 5: _multipower_vars()},
        converters=[{"id": "c1", "name": "Convertidor A"}],
    )
    reg = InstrumentRegistry(api)
    tree = reg.discover_all()

    assert len(reg.instruments) == 2
    tc = reg.by_device_id(1001)
    mp = reg.by_device_id(1005)
    assert tc is not None and tc.type == "tc900e"
    assert mp is not None and mp.type == "multipower"
    assert reg.by_type("tc900e") == [tc]
    assert reg.by_type("multipower") == [mp]
    assert reg.device_ids() == [1001, 1005]
    assert tree["n_instruments"] == 2
    assert tree["n_converters"] == 1


def test_registry_type_override_wins():
    api = _FakeApi(
        instruments=[{"id": 7, "name": "Raro"}],
        values_by_id={7: [{"code": "Foo", "value": 1.0, "unit": "u"}]},
    )
    reg = InstrumentRegistry(api, type_overrides={"7": "tc900e"})
    reg.discover_all()
    assert reg.by_device_id(1007).type == "tc900e"


def test_registry_names_map():
    api = _FakeApi(
        instruments=[
            {"id": 1, "name": "Cava 1"},
            {"id": 5, "name": "Medidor 1"},
        ],
        values_by_id={1: _tc900e_vars(), 5: _multipower_vars()},
    )
    reg = InstrumentRegistry(api)
    reg.discover_all()
    assert reg.names_map() == {1: "Cava 1", 5: "Medidor 1"}


# ---------------------------------------------------------------------------
# CavaAgent — resolución device y detección de Multipower
# ---------------------------------------------------------------------------

from types import SimpleNamespace


class _FakeRegistry:
    def __init__(self, entries):
        # entries: list of dicts {device_id, instrument_id, type, name}
        self.instruments = {
            e["device_id"]: SimpleNamespace(**e) for e in entries
        }

    def device_ids(self):
        return sorted(self.instruments.keys())

    def by_device_id(self, d):
        return self.instruments.get(int(d))

    def by_type(self, t):
        return [i for i in self.instruments.values() if i.type == t]


def _make_agent(registry):
    from agent.cava_agent import CavaAgent
    agent = object.__new__(CavaAgent)
    agent.registry_provider = (lambda: registry) if registry is not None else None
    agent.multipower_present = False
    return agent


def test_resolve_device_by_cava_number():
    reg = _FakeRegistry([
        {"device_id": 1001, "instrument_id": 1, "type": "tc900e", "name": "C1"},
        {"device_id": 1002, "instrument_id": 2, "type": "tc900e", "name": "C2"},
        {"device_id": 1005, "instrument_id": 5, "type": "multipower", "name": "MP"},
    ])
    agent = _make_agent(reg)
    assert agent._resolve_device_for_question("temperatura de la cava 1") == 1001
    assert agent._resolve_device_for_question("cava 2") == 1002


def test_resolve_device_ambiguous_returns_none():
    reg = _FakeRegistry([
        {"device_id": 1001, "instrument_id": 1, "type": "tc900e", "name": "C1"},
        {"device_id": 1002, "instrument_id": 2, "type": "tc900e", "name": "C2"},
    ])
    agent = _make_agent(reg)
    assert agent._resolve_device_for_question("como esta la cava") is None


def test_resolve_device_single_thermal_auto():
    reg = _FakeRegistry([
        {"device_id": 1001, "instrument_id": 1, "type": "tc900e", "name": "C1"},
        {"device_id": 1005, "instrument_id": 5, "type": "multipower", "name": "MP"},
    ])
    agent = _make_agent(reg)
    assert agent._resolve_device_for_question("como esta la cava") == 1001


def test_resolve_device_multipower_explicit():
    reg = _FakeRegistry([
        {"device_id": 1001, "instrument_id": 1, "type": "tc900e", "name": "C1"},
        {"device_id": 1002, "instrument_id": 2, "type": "tc900e", "name": "C2"},
        {"device_id": 1005, "instrument_id": 5, "type": "multipower", "name": "MP"},
    ])
    agent = _make_agent(reg)
    assert agent._resolve_device_for_question("el multipower") == 1005


def test_has_multipower_from_registry_true():
    reg = _FakeRegistry([
        {"device_id": 1001, "instrument_id": 1, "type": "tc900e", "name": "C1"},
        {"device_id": 1005, "instrument_id": 5, "type": "multipower", "name": "MP"},
    ])
    assert _make_agent(reg)._has_multipower() is True


def test_has_multipower_from_registry_false():
    reg = _FakeRegistry([
        {"device_id": 1001, "instrument_id": 1, "type": "tc900e", "name": "C1"},
    ])
    assert _make_agent(reg)._has_multipower() is False


def test_has_multipower_fallback_without_registry():
    agent = _make_agent(None)
    agent.multipower_present = True
    assert agent._has_multipower() is True


# ---------------------------------------------------------------------------
# Reporter — fan-out por device_id
# ---------------------------------------------------------------------------

def _reporter_stub(device_ids, fallback=201):
    from core.reporter import Reporter
    rep = object.__new__(Reporter)
    rep._monitored_device_ids = list(device_ids)
    rep.config = {"device_id": fallback}
    return rep


def test_reporter_device_ids_default_single():
    rep = _reporter_stub([], fallback=201)
    assert rep._device_ids() == [201]


def test_reporter_device_ids_multi():
    rep = _reporter_stub([1001, 1002, 1005], fallback=201)
    assert rep._device_ids() == [1001, 1002, 1005]
    assert rep._device_ids_len() == 3