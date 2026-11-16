# verify_python.py
"""Verificaciones que PowerShell no puede hacer: import en caliente,
instanciación de clases clave, y claves huérfanas en config.json."""
from __future__ import annotations
import json
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

print("\n>>> 8. Instanciación de clases clave (no solo import)")
print("=" * 60)

def _test_contracts():
    from core.contracts import ChannelReading, DataQuality, Baseline, EventCandidate, Change, Snapshot
    from datetime import datetime, timezone
    r = ChannelReading(1, "x", 1.0, "°C", DataQuality.VALID, datetime.now(timezone.utc))
    assert r.value == 1.0
    return "ChannelReading OK"

def _test_engine():
    from analysis.engine import AnalysisEngine
    e = AnalysisEngine(config={})
    return "AnalysisEngine OK"

def _test_diagnosis():
    from analysis.diagnosis import diagnose
    from datetime import datetime, timezone
    b = diagnose({})
    assert b is not None
    return f"diagnose({{}}) = {b.principal}"

def _test_cava_agent():
    from agent.cava_agent import CavaAgent, strip_fake_media, CHART_UNAVAILABLE
    out = strip_fake_media("ver [aquí](https://x.com/a)")
    assert out == "ver aquí"
    assert "http" not in CHART_UNAVAILABLE
    return "strip_fake_media OK"

def _test_cava_agent_methods():
    from agent.cava_agent import CavaAgent
    for name in ("_render_tree", "_tool_list_converters", "_tool_alarm_history",
                 "_tool_correlate_with_alarms", "_render_instrument_status",
                 "_tool_render_chart", "_route_question", "_render_instruments"):
        assert hasattr(CavaAgent, name), f"falta {name}"
    return "4 metodos nuevos + render_chart + router OK"

def _test_sitrad_api():
    from sources.sitrad_api import SitradApi, SitradApiSource, iter_variables, Variable
    from sources.sitrad_api import sensor_enabled_flags, describe_alarm
    import inspect
    # Bug 5: data_dir NO debe estar en SitradApi.__init__
    sig = inspect.signature(SitradApi.__init__)
    assert "data_dir" not in sig.parameters, "SitradApi.__init__ tiene data_dir"
    # discover_all existe
    assert hasattr(SitradApiSource, "discover_all")
    return "SitradApi y SitradApiSource OK"

def _test_cava_random():
    from simulation.cava_random import CavaRandomSource, EPISODE_CATALOG, SCENARIO_PRESETS
    src = CavaRandomSource(config={"device_id": 1}, seed=42)
    assert src is not None
    return f"CavaRandomSource OK ({len(EPISODE_CATALOG)} episodios, {len(SCENARIO_PRESETS)} presets)"

def _test_cooling_report():
    from analysis.cooling_report import CoolingConfig, CoolingMonitor, _risk_lines
    import inspect
    sig = inspect.signature(_risk_lines)
    assert "only_level" not in sig.parameters, "_risk_lines aun tiene only_level"
    return "_risk_lines sin only_level OK"

def _test_llm_client():
    from agent.llm_client import build_llm, OllamaLLM, TemplateLLM
    t = build_llm({"provider": "template"})
    assert t is not None
    return "build_llm(template) OK"

def _test_episode_history():
    from analysis.episode_history import EpisodeHistory
    h = EpisodeHistory(path=None)
    return f"EpisodeHistory OK (path=None)"

def _test_audit_real_data():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "audit_real_data", "tools/audit_real_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return "tools/audit_real_data.py importa OK"

def _test_smoke_reporter():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "smoke_reporter", "tools/smoke_reporter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return "tools/smoke_reporter.py importa OK"

check("core.contracts", _test_contracts)
check("analysis.engine", _test_engine)
check("analysis.diagnosis", _test_diagnosis)
check("agent.cava_agent (funciones)", _test_cava_agent)
check("agent.cava_agent (metodos)", _test_cava_agent_methods)
check("sources.sitrad_api", _test_sitrad_api)
check("simulation.cava_random", _test_cava_random)
check("analysis.cooling_report", _test_cooling_report)
check("agent.llm_client", _test_llm_client)
check("analysis.episode_history", _test_episode_history)
check("tools.audit_real_data", _test_audit_real_data)
check("tools.smoke_reporter", _test_smoke_reporter)

# ------------------------------------------------------------------
print("\n>>> 9. Claves de config.json sin uso")
print("=" * 60)

def _walk(d, prefix=""):
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        yield path, k
        if isinstance(v, dict):
            yield from _walk(v, path)

def _all_py_text():
    out = []
    for p in Path(".").rglob("*.py"):
        if any(x in p.parts for x in (".venv", "__pycache__", "logs", "sitrad_data")):
            continue
        try:
            out.append(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return "\n".join(out)

try:
    cfg = json.load(open("config.json", encoding="utf-8"))
    all_src = _all_py_text()
    # Extraer claves que SÍ se leen: cfg.get("X") o ["X"] o .get("X")
    import re
    used_leafs = set()
    for m in re.finditer(r'\.get\(\s*["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']', all_src):
        used_leafs.add(m.group(1))
    for m in re.finditer(r'\[\s*["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']\s*\]', all_src):
        used_leafs.add(m.group(1))
    # También claves con "schema" (DEFAULT_CFG, CoolingConfig.from_dict)
    for m in re.finditer(r'["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']\s*:', all_src):
        used_leafs.add(m.group(1))

    orphans = []
    for path, leaf in _walk(cfg):
        if leaf in used_leafs:
            continue
        # Los valores de "product_rules" son dinámicos (claves = nombre de producto)
        if path.startswith("cooling.product_rules."):
            continue
        # Los alias de producto también son dinámicos
        if path.startswith("channel_aliases."):
            continue
        # Los canales de "channels" son dinámicos
        if ".channels." in path:
            continue
        orphans.append(path)

    if orphans:
        print("  [INFO] Claves no encontradas en el código (revisar si son inertes):")
        for o in orphans:
            print(f"    · {o}")
    else:
        print("  [OK]    Todas las claves de config.json se leen en el código")
except Exception as exc:
    print(f"  [FALLA] No se pudo verificar config.json: {exc!r}")
    fails += 1

# ------------------------------------------------------------------
print("\n============================================================")
if fails == 0:
    print(" RESULTADO: OK")
    print("============================================================")
    sys.exit(0)
else:
    print(f" RESULTADO: {fails} fallo(s)")
    print("============================================================")
    sys.exit(1)