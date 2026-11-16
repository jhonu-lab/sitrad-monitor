"""Tests unitarios sin hardware ni LLM. Ejecutar:  python -m pytest tests -q   (escritos, aún no ejecutados en la cava)."""
from datetime import datetime, timezone

from agent.cava_agent import CHART_UNAVAILABLE, strip_fake_media
from core.sitrad_mapping import parse_state
from sources.sitrad_api import SitradApiSource, Variable, sensor_enabled_flags


def test_strip_markdown_image_and_adds_note():
    out = strip_fake_media("Mira:\n![Gráfico](https://example.com/t.png)\nMáx 3,2 °C")
    assert "example.com" not in out and "![" not in out
    assert "Máx 3,2 °C" in out and "No puedo mostrar gráficos" in out


def test_strip_link_keeps_text_and_bare_url_removed():
    assert strip_fake_media("ver [aquí](https://x.com/a)") == "ver aquí"
    assert "http" not in strip_fake_media("entra a https://foo.bar/x ahora")


def test_strip_none_and_plain_text_untouched():
    assert strip_fake_media(None) is None
    assert strip_fake_media("Temp Cámara: 2,1 °C") == "Temp Cámara: 2,1 °C"


def test_chart_unavailable_is_plain_text():
    assert "http" not in CHART_UNAVAILABLE


def _var(name, value):
    return Variable(name=name, value=value, unit="", ts=None, error=False, enabled=True, raw={})


def test_sensor_enabled_flags():
    flags = sensor_enabled_flags([_var("IsS1Enabled", True), _var("IsS2Enabled", False), _var("Otra", 1)])
    assert flags == {1: True, 2: False}


def test_disabled_sensor_and_implausible_value_are_missing():
    src = SitradApiSource.__new__(SitradApiSource)
    from core.channel_catalog import CONTROLLER_SPECS
    src.device_id = 1
    src._spec = {s.name: s for s in CONTROLLER_SPECS}
    t = datetime.now(timezone.utc)
    r = src._reading("Temp Evaporador", _var("Evaporador", -12.0), t, {2: False})
    assert r.quality.value == "MISSING" and r.value is None
    r = src._reading("Temp Evaporador", _var("Evaporador", -112.2), t, {2: True})
    assert r.quality.value == "MISSING"
    r = src._reading("Temp Cámara", _var("Ambiente", 2.5), t, {1: True})
    assert r.quality.value == "VALID" and r.value == 2.5


def test_parse_state():
    assert parse_state("Conectado") == 1.0 and parse_state("Desconectado") == 0.0
    assert parse_state(True) == 1.0 and parse_state(0) == 0.0
    assert parse_state("???") is None and parse_state(None) is None
