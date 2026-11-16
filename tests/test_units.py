"""Tests unitarios sin hardware ni LLM."""
from datetime import datetime, timedelta, timezone

from agent.cava_agent import CHART_UNAVAILABLE, CavaAgent, strip_fake_media
from agent.report_renderer import numeric_anchor_ok
from core.contracts import ChannelReading as R, DataQuality as Q
from core.sitrad_mapping import parse_state
from sources.sitrad_api import SitradApiSource, Variable, sensor_enabled_flags

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


def test_strip_markdown_image_and_adds_note():
    out = strip_fake_media("Mira:\n![Gráfico](https://example.com/t.png)\nMáx 3,2 °C")
    assert "example.com" not in out and "![" not in out
    assert "Máx 3,2 °C" in out and "No puedo mostrar gráficos" in out


def test_strip_link_and_bare_url():
    assert strip_fake_media("ver [aquí](https://x.com/a)") == "ver aquí"
    assert "http" not in strip_fake_media("entra a https://foo.bar/x ahora")


def test_strip_none_and_plain_text():
    assert strip_fake_media(None) is None
    assert strip_fake_media("Temp Cámara: 2,1 °C") == "Temp Cámara: 2,1 °C"


def test_chart_unavailable_is_plain_text():
    assert "http" not in CHART_UNAVAILABLE


def _var(name, value, enabled=True):
    return Variable(name=name, value=value, unit="", ts=None, error=False, enabled=enabled, raw={})


def test_sensor_enabled_flags():
    assert sensor_enabled_flags([_var("IsS1Enabled", True), _var("IsS2Enabled", False), _var("Otra", 1)]) == {1: True, 2: False}


def _src():
    from core.channel_catalog import CONTROLLER_SPECS
    s = SitradApiSource.__new__(SitradApiSource)
    s.device_id = 1
    s._spec = {c.name: c for c in CONTROLLER_SPECS}
    return s


def test_disabled_sensor_is_missing():
    r = _src()._reading("Temp Evaporador", _var("Evaporador", -12.0), T0, {2: False})
    assert r.quality == Q.MISSING and r.value is None


def test_implausible_temperature_is_missing():
    r = _src()._reading("Temp Evaporador", _var("Evaporador", -112.2), T0, {2: True})
    assert r.quality == Q.MISSING


def test_valid_temperature():
    r = _src()._reading("Temp Cámara", _var("Ambiente", 2.5), T0, {1: True})
    assert r.quality == Q.VALID and r.value == 2.5


def test_enabled_false_is_missing():
    r = _src()._reading("Puerta Abierta", _var("Puerta abierta", 0, enabled=False), T0, {})
    assert r.quality == Q.MISSING


def test_parse_state():
    assert parse_state("Conectado") == 1.0 and parse_state("Desconectado") == 0.0
    assert parse_state(True) == 1.0 and parse_state(0) == 0.0
    assert parse_state("???") is None and parse_state(None) is None


def _rs(times_s, ch="x"):
    return [R(1, ch, float(i), "u", Q.VALID, T0 + timedelta(seconds=t)) for i, t in enumerate(times_s)]


def test_align_series_one_to_one_no_reuse():
    fast = _rs(range(0, 1200, 120))              # cada 2 min
    slow = _rs([0, 1200])                        # cada 20 min
    a, b = CavaAgent._align_series(fast, slow)
    assert len(a) == len(b) <= 2                 # la lenta no se cuenta ~10 veces
    assert len({id(x) for x in b}) == len(b)


def test_numeric_anchor_ok_accepts_only_context_numbers():
    ok, miss = numeric_anchor_ok("Máximo 3,5 °C", "temperatura max 3.5")
    assert ok and not miss
    ok, miss = numeric_anchor_ok("Máximo 9 °C", "temperatura max 3.5")
    assert not ok and "9" in miss
