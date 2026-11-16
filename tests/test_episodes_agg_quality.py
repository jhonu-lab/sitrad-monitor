import json
from datetime import datetime, timedelta, timezone

from analysis.aggregations import aggregate, export_csv
from analysis.episode_history import EpisodeHistory
from analysis.quality import channel_quality
from analysis.time_ranges import parse_time_range
from core.contracts import ChannelReading as R, DataQuality as Q

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _series(n=60, step=60, bad=()):
    return [R(1, "x", float(i), "°C", Q.MISSING if i in bad else Q.VALID, T0 + timedelta(seconds=i * step),
             ) if i not in bad else R(1, "x", None, "°C", Q.MISSING, T0 + timedelta(seconds=i * step))
            for i in range(n)]


def test_aggregate_by_hour_and_count():
    rows = aggregate(_series(120), T0, T0 + timedelta(hours=3), "mean", "hour", tz_name="UTC")
    assert len(rows) == 2 and rows[0]["n"] == 60
    assert aggregate(_series(10), T0, T0 + timedelta(hours=1), "count", "none")[0]["valor"] == 10


def test_export_csv(tmp_path):
    n = export_csv(_series(5), T0, T0 + timedelta(hours=1), str(tmp_path / "a.csv"))
    assert n == 5 and (tmp_path / "a.csv").read_text(encoding="utf-8-sig").startswith("timestamp_local")


def test_quality_full_and_gap():
    q = channel_quality(_series(60), T0, T0 + timedelta(minutes=59), now=T0 + timedelta(minutes=59))
    assert q["cobertura_pct"] >= 95 and q["n_gaps_mayores_10min"] == 0
    gapped = [r for i, r in enumerate(_series(60)) if not 20 <= i < 40]
    q = channel_quality(gapped, T0, T0 + timedelta(minutes=59), now=T0 + timedelta(minutes=59))
    assert q["cobertura_pct"] < 80 and q["n_gaps_mayores_10min"] >= 1


def test_quality_all_missing_is_disabled():
    q = channel_quality(_series(10, bad=set(range(10))), T0, T0 + timedelta(minutes=10))
    assert q["n_valid"] == 0 and q["estado"].startswith("sin datos")


def test_time_ranges():
    now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)           # 13:00 en Bogotá
    d0, d1, label = parse_time_range("como estuvo la cava ayer", now)
    assert label == "ayer" and (d1 - d0) == timedelta(days=1)
    assert parse_time_range("hola", now) is None
    assert parse_time_range("la semana pasada", now)[2] == "la semana pasada"


def test_episode_history_record_query_summary(tmp_path):
    from types import SimpleNamespace
    h = EpisodeHistory(str(tmp_path / "e.jsonl"))
    rec = {"id": "a", "canal": "Temp Cámara", "start": T0.isoformat(), "end": (T0 + timedelta(minutes=30)).isoformat(),
           "peak_c": 6.0, "prev_c": 2.0, "above_min": 30.0, "kind": "puerta", "kind_label": "puerta abierta",
           "cause": "puerta", "limit_c": 4.0}
    h._eps["a"] = rec
    assert len(h.query(T0 - timedelta(hours=1), T0 + timedelta(hours=1))) == 1
    assert h.query(T0 + timedelta(hours=2), T0 + timedelta(hours=3)) == []
    s = h.summary(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert s["n"] == 1 and s["por_causa"] == {"puerta abierta": 1} and s["tiempo_total_sobre_limite"] == 30.0
    # persistencia: un archivo con una línea corrupta se lee igual
    p = tmp_path / "e2.jsonl"
    p.write_text(json.dumps(rec) + "\nbasura\n", encoding="utf-8")
    assert len(EpisodeHistory(str(p)).query(T0 - timedelta(hours=1), T0 + timedelta(hours=1))) == 1
