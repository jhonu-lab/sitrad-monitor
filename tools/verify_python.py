# verify_python.py v3
"""Verificaciones en caliente: import e instanciacion de clases clave."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, ".")
fails = 0

def check(name, fn):
    global fails
    try:
        result = fn()
        print(f"  [OK]    {name}: {result}")
    except Exception as exc:
        print(f"  [FALLA] {name}: {exc!r}")
        fails += 1

print("\n>>> Instanciacion de clases clave")
print("=" * 60)

def _t_contracts():
    from core.contracts import ChannelReading, DataQuality
    from datetime import datetime, timezone
    r = ChannelReading(1, "x", 1.0, "°C", DataQuality.VALID, datetime.now(timezone.utc))
    assert r.value == 1.0
    return "ChannelReading OK"

def _t_engine():
    from analysis.engine import AnalysisEngine
    AnalysisEngine(config={})
    return "AnalysisEngine OK"

def _t_diagnosis():
    from analysis.diagnosis import diagnose, DataQuality, DEFAULT_CFG
    b = diagnose({})
    assert b is not None
    return f"diagnose({{}}) = {b.principal}"

def _t_cava_agent_funcs():
    from agent.cava_agent import strip_fake_media, CHART_UNAVAILABLE, CavaAgent
    assert strip_fake_media("ver [aquí](https://x.com/a)") == "ver aquí"
    assert "http" not in CHART_UNAVAILABLE
    return "strip_fake_media OK"

def _t_cava_agent_methods():
    from agent.cava_agent import CavaAgent
    for name in ("_render_tree", "_tool_list_converters", "_tool_alarm_history",
                 "_tool_correlate_with_alarms", "_render_instrument_status",
                 "_tool_render_chart", "_route_question", "_render_instruments"):
        assert hasattr(CavaAgent, name), f"falta {name}"
    return "4 metodos nuevos + render_chart + router OK"

def _t_sitrad_api():
    from sources.sitrad_api import (SitradApi, SitradApiSource, iter_variables,
                                    Variable, sensor_enabled_flags, describe_alarm)
    import inspect
    sig = inspect.signature(SitradApi.__init__)
    assert "data_dir" not in sig.parameters, "SitradApi.__init__ tiene data_dir"
    assert hasattr(SitradApiSource, "discover_all")
    return "SitradApi y SitradApiSource OK"

def _t_cava_random():
    from simulation.cava_random import CavaRandomSource, EPISODE_CATALOG, SCENARIO_PRESETS
    src = CavaRandomSource(config={"device_id": 1}, seed=42)
    assert src is not None
    return f"CavaRandomSource OK ({len(EPISODE_CATALOG)} episodios, {len(SCENARIO_PRESETS)} presets)"

def _t_cooling_report():
    from analysis.cooling_report import CoolingConfig, CoolingMonitor, _risk_lines
    import inspect
    sig = inspect.signature(_risk_lines)
    assert "only_level" not in sig.parameters, "_risk_lines aun tiene only_level"
    return "_risk_lines sin only_level OK"

def _t_llm_client():
    from agent.llm_client import build_llm
    build_llm({"provider": "template"})
    return "build_llm(template) OK"

def _t_episode_history():
    from analysis.episode_history import EpisodeHistory
    EpisodeHistory(path=None)
    return "EpisodeHistory OK"

def _t_smoke_reporter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("smoke_reporter", "tools/smoke_reporter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return "tools/smoke_reporter.py importa OK"

def _t_audit_real_data():
    import importlib.util
    spec = importlib.util.spec_from_file_location("audit_real_data", "tools/audit_real_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return "tools/audit_real_data.py importa OK"

check("core.contracts", _t_contracts)
check("analysis.engine", _t_engine)
check("analysis.diagnosis", _t_diagnosis)
check("agent.cava_agent (funciones)", _t_cava_agent_funcs)
check("agent.cava_agent (metodos)", _t_cava_agent_methods)
check("sources.sitrad_api", _t_sitrad_api)
check("simulation.cava_random", _t_cava_random)
check("analysis.cooling_report", _t_cooling_report)
check("agent.llm_client", _t_llm_client)
check("analysis.episode_history", _t_episode_history)
check("tools.smoke_reporter", _t_smoke_reporter)
check("tools.audit_real_data", _t_audit_real_data)

print("\n============================================================")
if fails == 0:
    print(" RESULTADO: OK")
else:
    print(f" RESULTADO: {fails} fallo(s)")
print("============================================================")
sys.exit(0 if fails == 0 else 1)