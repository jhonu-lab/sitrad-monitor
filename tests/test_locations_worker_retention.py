"""Regresión de la revisión de noviembre 2026: lugares multi-instrumento, hilo del agente,
respuestas por chat, rotación/limpieza de datos y recuperación de Telegram/Ollama."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from core.contracts import ChannelReading, DataQuality
from core.series_store import SeriesStore

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# API SITRAD falsa: 2 controladores, 1 Multipower, 1 horno genérico
# ---------------------------------------------------------------------------

def _tc(temp):
    return [
        {"code": "Ambiente", "value": temp, "unit": "°C", "date": "2026-11-20T12:00:00Z"},
        {"code": "Salida REFR", "value": 1, "date": "2026-11-20T12:00:00Z"},
        {"code": "Salida DEFR", "value": 0, "date": "2026-11-20T12:00:00Z"},
    ]


def _mp():
    return [
        {"code": "M1: Potencia Activa Total (kW)", "value": 1.5, "unit": "kW", "date": "2026-11-20T12:00:00Z"},
        {"code": "M2: Potencia Activa Total (kW)", "value": 1.2, "unit": "kW", "date": "2026-11-20T12:00:00Z"},
    ]


class _Api:
    def __init__(self):
        self.fail = set()
        self.vals = {1: _tc(2.0), 2: _tc(3.0), 5: _mp(),
                     7: [{"code": "Temperatura Horno", "value": 180.0, "unit": "°C",
                          "date": "2026-11-20T12:00:00Z"}]}

    def instruments(self):
        return [{"id": 1, "name": "Cava Norte"}, {"id": 2, "name": "Cava Norte B"},
                {"id": 5, "name": "Medidor"}, {"id": 7, "name": "Horno 1"}]

    def values(self, iid):
        if iid in self.fail:
            raise RuntimeError("caído")
        return self.vals[iid]

    def converters(self):
        return []

    def alarms(self, start, end):
        return []


LOCATIONS = [
    {"name": "Cava Norte", "kind": "cava", "instruments": [1, "Cava Norte B", 5]},
    {"name": "Horno", "kind": "horno", "instruments": ["horno 1"], "device_id": 3001},
]


def _source(**kw):
    from sources.sitrad_api import SitradApiSource
    src = SitradApiSource(_Api(), poll_seconds=0, alarms_every_s=0, locations=LOCATIONS, **kw)
    src.discover()
    return src


def test_locations_group_instruments_and_flags():
    src = _source()
    reg = src.registry
    assert reg.location_ids() == [1001, 3001]
    cava, horno = reg.location(1001), reg.location(3001)
    assert cava.instrument_ids == [1, 2, 5] and cava.cooling and cava.kind == "cava"
    assert "multipower" in cava.types
    assert horno.kind == "horno" and not horno.cooling
    assert reg.location_of_instrument(5).device_id == 1001
    assert reg.location_of_instrument(7).device_id == 3001


def test_auto_location_for_unassigned_instrument():
    from sources.sitrad_api import SitradApiSource
    src = SitradApiSource(_Api(), poll_seconds=0, locations=[{"name": "Cava", "instruments": [1]}])
    src.discover()
    reg = src.registry
    # 2, 5 y 7 no estaban en el config: un lugar automático cada uno.
    assert reg.location_ids() == [1001, 1002, 1005, 1007]
    assert reg.location(1005).kind == "energia" and not reg.location(1005).cooling


def test_multi_instrument_off_reads_only_primary():
    src = _source(instrument_id=2, multi_instrument=False)
    assert list(src.registry.instruments) == [1002]


def test_poll_publishes_under_location_and_suffixes_collisions():
    src = _source()
    snap = src.poll(T0)
    by_dev: dict[int, set] = {}
    for r in snap.readings:
        by_dev.setdefault(r.device_id, set()).add(r.channel)
    assert "Temp Cámara" in by_dev[1001]
    assert "Temp Cámara [Cava Norte B]" in by_dev[1001]          # segundo controlador del mismo lugar
    assert "M1 Potencia Activa" in by_dev[1001] or any(c.startswith("M1") for c in by_dev[1001])
    assert by_dev[3001]                                            # el horno publica en su lugar


def test_poll_failure_streak_only_when_everything_fails(caplog):
    src = _source()
    src.api.fail = {1, 2, 5, 7}
    src.poll(T0)
    assert src._fail_streak == 1
    src.api.fail = {7}
    src.poll(T0 + timedelta(seconds=30))
    assert src._fail_streak == 0                                   # hubo lecturas: no es caída total


def test_rediscover_when_sitrad_was_down_at_start():
    from sources.sitrad_api import SitradApiSource

    class _Down(_Api):
        up = False

        def instruments(self):
            if not self.up:
                raise RuntimeError("sin conexión")
            return super().instruments()

    api = _Down()
    src = SitradApiSource(api, poll_seconds=0, locations=LOCATIONS)
    src.discover()
    reg = src.registry
    assert not reg.instruments
    api.up = True
    src.poll(T0)
    assert src.registry is reg and reg.location_ids() == [1001, 3001]


# ---------------------------------------------------------------------------
# Política por lugar
# ---------------------------------------------------------------------------

def test_policy_keeps_incidents_of_each_location_apart():
    from analysis.anomaly_detector import AnomalyState
    from core.report_policy import ReportPolicy
    pol = ReportPolicy({"max_messages_per_hour": 100})
    st = AnomalyState("Temp Cámara", "out_of_profile", 0.6, T0, {})
    m1 = pol.decide_anomalies({"Temp Cámara": st}, T0, device_id=1001, label="Cava Norte")
    m2 = pol.decide_anomalies({"Temp Cámara": st}, T0, device_id=1002, label="Cava Sur")
    assert len(pol.open_incidents) == 2
    assert m1[0].text.startswith("[Cava Norte]") and m2[0].payload["device_id"] == 1002


# ---------------------------------------------------------------------------
# SeriesStore
# ---------------------------------------------------------------------------

def _r(dev, ch, t, v=1.0):
    return ChannelReading(dev, ch, v, "°C", DataQuality.VALID, t)


def test_store_short_retention_for_passthrough_and_device_series():
    st = SeriesStore(max_hours=48, short_max_hours=1, is_long=lambda c: c == "Temp Cámara")
    for i in range(0, 6 * 60, 5):                                  # 6 h cada 5 min
        t = T0 + timedelta(minutes=i)
        st.ingest(_r(1, "Temp Cámara", t))
        st.ingest(_r(1, "FncOffset", t))
        st.ingest(_r(2, "Temp Cámara", t))
    st._prune(T0 + timedelta(hours=6))
    assert len(st.get_series(1, "FncOffset")) <= 13                 # ~1 h
    assert len(st.get_series(1, "Temp Cámara")) == 72               # 6 h completas
    ds = st.device_series(1, minutes=30)
    assert set(ds) == {"Temp Cámara", "FncOffset"} and len(ds["Temp Cámara"]) == 7
    assert st.device_series(1, minutes=30, channels=lambda c: c == "Temp Cámara").keys() == {"Temp Cámara"}
    assert st.latest_times(2)["Temp Cámara"] == T0 + timedelta(minutes=355)


def test_store_concurrent_reads_do_not_break():
    st = SeriesStore(max_hours=1)
    errors = []

    def reader():
        for _ in range(300):
            try:
                st.all_series(minutes=10)
                st.channels(1)
            except Exception as exc:                               # pragma: no cover
                errors.append(exc)

    th = threading.Thread(target=reader)
    th.start()
    for i in range(3000):
        st.ingest(_r(1, f"c{i % 50}", T0 + timedelta(seconds=i)))
    th.join()
    assert not errors


# ---------------------------------------------------------------------------
# FileLog: rotación diaria y lectura en streaming
# ---------------------------------------------------------------------------

def test_daily_rotation_creates_one_file_per_day(tmp_path):
    from core.file_log import FileLog
    obs = tmp_path / "observations.jsonl"
    fl = FileLog(path=str(tmp_path / "r.jsonl"), events_path=str(tmp_path / "e.jsonl"),
                 observations_path=str(obs), rotation_mode="daily", rotation_keep_days=0)
    for d in range(3):
        fl.write_observations([_r(1, "x", T0 + timedelta(days=d))])
    names = sorted(os.listdir(tmp_path))
    assert names == ["observations.20261120.jsonl", "observations.20261121.jsonl", "observations.jsonl"]
    # Un FileLog nuevo (reinicio otro día) también rota lo que quedó del día anterior.
    fl2 = FileLog(path=str(tmp_path / "r.jsonl"), events_path=str(tmp_path / "e.jsonl"),
                  observations_path=str(obs), rotation_mode="daily", rotation_keep_days=0)
    fl2.write_observations([_r(1, "x", T0 + timedelta(days=5))])
    assert "observations.20261122.jsonl" in os.listdir(tmp_path)
    got = list(fl2.iter_observations_since(T0 - timedelta(days=1), device_id=1))
    assert len(got) == 4


def test_filelog_drops_non_finite_values(tmp_path):
    from core.file_log import FileLog
    fl = FileLog(observations_path=str(tmp_path / "o.jsonl"), path=str(tmp_path / "r"),
                 events_path=str(tmp_path / "e"))
    fl.write_observations([_r(1, "x", T0, float("nan")), _r(1, "x", T0 + timedelta(seconds=1), 2.0)])
    assert len((tmp_path / "o.jsonl").read_text(encoding="utf-8").splitlines()) == 1


# ---------------------------------------------------------------------------
# Limpieza programada
# ---------------------------------------------------------------------------

def test_retention_trims_jsonl_prunes_daily_and_runs_once_a_day(tmp_path):
    from core.retention import RetentionManager
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps({"inicio": (T0 - timedelta(days=d)).isoformat()})
                                for d in (100, 50, 1)) + "\n", encoding="utf-8")
    for day in ("20261101", "20261119"):
        (tmp_path / f"lecturas.{day}.jsonl").write_text("{}\n", encoding="utf-8")
    charts = tmp_path / "charts"
    charts.mkdir()
    old = charts / "viejo.png"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 30 * 86400,) * 2)

    rm = RetentionManager.from_config(
        {"days": {"events": 90, "raw_readings": 3, "charts": 7}},
        paths={"events": str(events), "raw_readings": str(tmp_path / "lecturas.jsonl"),
               "charts": str(charts)},
        tz_name="America/Bogota")
    now = T0.replace(hour=9)                                       # 04:00 en Bogotá
    assert rm.maybe_run(now)
    assert len(events.read_text(encoding="utf-8").splitlines()) == 2
    assert sorted(p.name for p in tmp_path.glob("lecturas.*")) == ["lecturas.20261119.jsonl"]
    assert not old.exists()
    assert not rm.maybe_run(now + timedelta(hours=2))              # ya corrió hoy


# ---------------------------------------------------------------------------
# Telegram, reintentos por chat y LLM
# ---------------------------------------------------------------------------

def test_telegram_poll_thread_survives_backlog_drain():
    from notifiers.telegram_report import TelegramReporter
    t = TelegramReporter(bot_token="123456789:" + "A" * 35, chat_id="1")
    t._identify_bot = lambda: None
    calls = []

    class _Sess:
        def get(self, *a, **k):
            raise __import__("requests").exceptions.ConnectionError("sin red")

    t._get_session = lambda kind: _Sess()
    t._poll_once = lambda: (calls.append(1), t._stop_event.set())
    th = threading.Thread(target=t._poll_loop)
    th.start()
    th.join(3)
    assert calls == [1]


def test_parse_incoming_accepts_any_configured_chat():
    from notifiers.telegram_report import parse_incoming
    msg = {"text": "hola", "chat": {"id": 22, "type": "private"}, "from": {"id": 9}}
    assert parse_incoming(msg, allowed_chats=["11", "22"]).chat_id == "22"
    assert parse_incoming(msg, allowed_chats=["11"]) is None


def test_retry_notifier_broadcasts_and_retries_per_chat():
    from notifiers.retry_notifier import RetryNotifier

    class _Inner:
        chat_ids = ["A", "B"]

        def __init__(self):
            self.ok = {"A": True, "B": False}
            self.sent = []

        def send(self, text, chat_id=None):
            if self.ok.get(chat_id):
                self.sent.append((chat_id, text))
                return True
            return False

    inner = _Inner()
    rn = RetryNotifier(inner, retry_interval_seconds=0)
    assert rn.send("alerta") is False
    assert inner.sent == [("A", "alerta")] and rn.pending == 1
    inner.ok["B"] = True
    rn.tick()
    assert inner.sent == [("A", "alerta"), ("B", "alerta")] and rn.pending == 0
    rn.send("respuesta", chat_id="B")
    assert inner.sent[-1] == ("B", "respuesta")


def test_llm_recovers_when_ollama_starts_late():
    import requests
    import agent.llm_client as lc

    class _R:
        def __init__(self, d):
            self.d = d

        def raise_for_status(self):
            pass

        def json(self):
            return self.d

    state = {"up": False}

    def fake_get(*a, **k):
        if not state["up"]:
            raise requests.exceptions.ConnectionError("down")
        return _R({"models": [{"name": "m"}]})

    old_get, old_post = lc.requests.get, lc.requests.post
    lc.requests.get = fake_get
    lc.requests.post = lambda *a, **k: _R({"message": {"content": "hola"}})
    try:
        llm = lc.OllamaLLM(model="m", circuit_failures=5)
        assert llm.generate_text("s", "u") is None
        state["up"] = True
        assert llm.generate_text("s", "u") == "hola"
    finally:
        lc.requests.get, lc.requests.post = old_get, old_post


# ---------------------------------------------------------------------------
# Reporter: hilo del agente y respuesta en el chat de origen
# ---------------------------------------------------------------------------

class _Notifier:
    def __init__(self):
        self.sent = []
        self.photos = []
        self.q = []

    def send(self, text, chat_id=None):
        self.sent.append((chat_id, text))
        return True

    def send_photo(self, path, caption="", chat_id=None):
        self.photos.append((chat_id, path))
        return True

    def has_command(self):
        return bool(self.q)

    def pop_command(self):
        return self.q.pop(0)


def _reporter(async_agent, tmp_path, agent):
    from analysis.engine import AnalysisEngine
    from analysis.event_engine import EventEngine
    from agent.context_serializer import ContextSerializer
    from core.clock import FrozenClock
    from core.reporter import Reporter
    from core.contracts import Snapshot

    class _Src:
        def poll(self, now):
            return Snapshot("x", now, (_r(201, "Temp Cámara", now, 2.0),))

    eng = AnalysisEngine({})
    rep = Reporter(source=_Src(), store=SeriesStore(24), event_engine=EventEngine(analysis=eng, config={}),
                   agent=agent, context_serializer=ContextSerializer(), telegram=_Notifier(), filelog=None,
                   clock=FrozenClock(T0), config={
                       "device_id": 201, "async_agent": async_agent,
                       "cooling": {"enabled": False, "products_path": str(tmp_path / "p.json"),
                                   "cases_path": str(tmp_path / "c.jsonl"),
                                   "feedback_path": str(tmp_path / "f.jsonl")},
                       "storage": {"episodes_log": str(tmp_path / "ep.jsonl")}})
    return rep


class _SlowAgent:
    engine = None

    def __init__(self, delay=0.0):
        self.delay = delay
        self.thread = None

    def answer_question(self, q, **kw):
        self.thread = threading.current_thread().name
        time.sleep(self.delay)
        if "foto" in q:
            return {"__photo__": "x.png", "caption": "gráfico"}
        return f"respuesta a {q}"


def test_questions_run_in_worker_and_answer_in_their_chat(tmp_path):
    from notifiers.telegram_report import UserQuestion
    agent = _SlowAgent(delay=0.3)
    rep = _reporter(True, tmp_path, agent)
    rep.telegram.q = [UserQuestion("temperatura", user_key="7:1", user_name="Ana", chat_id="7"),
                      UserQuestion("una foto", user_key="8:2", user_name="Luis", chat_id="8")]
    started = time.monotonic()
    rep.run_one_cycle(T0)
    assert time.monotonic() - started < 0.25                       # el ciclo no espera al agente
    assert rep.drain_worker(5)
    assert rep.telegram.sent == [("7", "Ana: respuesta a temperatura")]
    assert rep.telegram.photos == [("8", "x.png")]
    assert agent.thread == "agent-worker"
    rep._worker.stop()


def test_inline_worker_keeps_synchronous_behaviour(tmp_path):
    from notifiers.telegram_report import UserQuestion
    rep = _reporter(False, tmp_path, _SlowAgent())
    rep.telegram.q = [UserQuestion("hola", user_key="5:1", chat_id="5")]
    rep.run_one_cycle(T0)
    assert rep.telegram.sent == [("5", "respuesta a hola")]


# ---------------------------------------------------------------------------
# Agente: dispositivo por defecto y diagnóstico de un lugar concreto
# ---------------------------------------------------------------------------

def test_agent_defaults_to_cooling_location_and_diagnose_uses_given_device():
    from agent.cava_agent import CavaAgent
    from agent.demo_llm import DemoLLM
    from analysis.engine import AnalysisEngine
    src = _source()
    store = SeriesStore(24)
    for i in range(30):
        t = T0 + timedelta(minutes=i)
        store.ingest(_r(1001, "Temp Cámara", t, 2.0 + i * 0.01))
        store.ingest(_r(3001, "Temperatura Horno", t, 180.0))
    agent = CavaAgent(llm=DemoLLM(), engine=AnalysisEngine({}), store=store, device_id=201,
                      registry_provider=lambda: src.registry)
    assert agent._dev() == 1001                                    # 201 no existe: primer lugar con frío
    assert agent._resolve_device_for_question("como esta el horno") == 3001
    r = agent._tool_diagnose_now({"minutos": 60, "device_id": 3001})
    assert "Temperatura Horno" in str(r)                           # diagnosticó el horno, no la cava
