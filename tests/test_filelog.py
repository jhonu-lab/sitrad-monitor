import json
from datetime import datetime, timedelta, timezone

from core.file_log import FileLog

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


def _rec(ch, t, v=1.0, q="VALID"):
    return {"type": "observation", "device_id": 201, "channel": ch, "value": v, "unit": "°C", "quality": q,
            "event_time": t.isoformat(), "raw": ""}


def test_read_observations_since(tmp_path):
    p = tmp_path / "observations.jsonl"
    rot = tmp_path / "observations.20261103.jsonl"
    rot.write_text(json.dumps(_rec("Temp Cámara", T0 - timedelta(hours=30))) + "\n" +
                   json.dumps(_rec("Temp Cámara", T0 - timedelta(hours=2))) + "\n", encoding="utf-8")
    lines = [json.dumps(_rec("Temp Cámara", T0)),
             "{línea corrupta",
             json.dumps(_rec("Temp Cámara", T0 + timedelta(minutes=1), q="FROZEN")),
             json.dumps(_rec("Estado Refrigeración", T0 + timedelta(minutes=2), v=None)),
             json.dumps(_rec("Estado Refrigeración", T0 + timedelta(minutes=3), v=1.0))]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fl = FileLog(observations_path=str(p), path=str(tmp_path / "r.jsonl"), events_path=str(tmp_path / "e.jsonl"))
    got = fl.read_observations_since(T0 - timedelta(hours=24))
    assert [r.event_time for r in got] == sorted(r.event_time for r in got)       # cronológico
    assert len(got) == 3                                  # sin la vieja, la corrupta, FROZEN ni value=None
    assert all(r.quality.value == "VALID" for r in got)
    assert got[0].device_id == 201 and got[0].channel == "Temp Cámara" and got[0].unit == "°C"
