"""Enrutador determinista: no debe invocar al LLM para estas preguntas."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from agent.cava_agent import CavaAgent
from analysis.engine import AnalysisEngine
from core.clock import FrozenClock
from core.contracts import ChannelReading as R, DataQuality as Q
from core.series_store import SeriesStore

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
DEV = 201


class _NoLLM:
    def generate_with_tools(self, *a, **k):
        raise AssertionError("el LLM no debía llamarse")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                      # los PNG/CSV van a tmp_path/logs
    store = SeriesStore(max_hours=168)
    t = NOW - timedelta(hours=30)
    i = 0
    while t <= NOW:
        for ch, v, u in (("Temp Cámara", 2.0 + (i % 20) * 0.1, "°C"), ("Estado Refrigeración", float(i % 2), "estado"),
                         ("Puerta Abierta", 0.0, "estado"), ("Setpoint", 1.0, "°C")):
            store.ingest(R(DEV, ch, v, u, Q.VALID, t))
        t += timedelta(seconds=120)
        i += 1
    a = CavaAgent(llm=_NoLLM(), engine=AnalysisEngine(config={}), store=store, device_id=DEV,
                  clock=FrozenClock(NOW))
    a.multipower_present = False
    return a


def test_controladores_is_not_a_product_question(agent):
    calls = []
    orig = agent._execute_tool
    agent._execute_tool = lambda n, a: (calls.append(n), orig(n, a))[1]
    agent._route_question("cuántos controladores tienes")
    assert "load_report" not in calls


def test_chart_of_3_minutes_is_clamped_and_honest(agent):
    r = agent._route_question("hazme un gráfico de 3 minutos")
    assert isinstance(r, dict) and os.path.exists(r["__photo__"])
    assert "pediste 3, mínimo 5" in r["caption"]


def test_chart_temp_vs_compresor_goes_to_router(agent):
    r = agent._route_question("gráfico de temperatura vs compresor")
    assert isinstance(r, dict) and r["__photo__"].endswith(".png")
    assert "Temp Cámara" in r["caption"] and "Estado Refrigeración" in r["caption"]


def test_chart_three_series(agent):
    r = agent._route_question("hazme un gráfico de temperatura, compresor y puerta de las últimas 6 horas")
    assert isinstance(r, dict)
    for ch in ("Temp Cámara", "Estado Refrigeración", "Puerta Abierta"):
        assert ch in r["caption"]


def test_history_coverage(agent):
    r = agent._route_question("qué historia tienes")
    assert "Tengo datos desde" in r and "30.0 h" in r


def test_last_week_is_honest_about_history(agent):
    r = agent._route_question("cómo estuvo la cava la semana pasada")
    assert r.startswith("Solo tengo datos desde")


def test_quality_and_aggregate_and_export(agent):
    q = agent._route_question("calidad de los datos")
    assert "Calidad de datos" in q and "Puerta Abierta" in q
    a = agent._route_question("promedio de temperatura por hora de hoy")
    assert "mean por hour" in a
    old = agent._route_question("promedio de temperatura por día de la última semana")
    assert old.startswith("Solo tengo datos desde")          # pidió 7 días y solo hay 30 h
    e = agent._route_question("exporta la temperatura de hoy a CSV")
    assert "Exporté" in e and os.path.exists(e.split(" a ")[1].split(" (")[0])


def test_episodes_without_history_object(agent):
    r = agent._route_question("dame los episodios de ayer")
    assert isinstance(r, str)
