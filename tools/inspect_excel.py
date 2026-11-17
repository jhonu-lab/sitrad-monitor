"""
tools/inspect_excel.py — analiza un Excel de SITRAD y reporta qué instrumento es
y qué canales contiene. No arranca el bot.

Uso directo:
    python tools/inspect_excel.py ruta.xlsx [--config config.json] [--output report.json]

Uso desde el ejecutable:
    sitrad-monitor.exe --inspect-excel ruta.xlsx

Qué detecta:
    - tipo de instrumento (controlador, Multipower M1/M2, mixto, desconocido)
    - canales mapeados (con el channel_map del config y con el catálogo canónico)
    - columnas no reconocidas
    - sugerencia de mapeo para las no reconocidas (si son parecidas a algo conocido)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.channel_catalog import (
    CONTROLLER_SPECS,
    MULTIPOWER_SPECS,
    SPEC_BY_NAME,
    norm_col,
)
from core.sitrad_mapping import key, match_columns


# ─────────────────────────────────────────────────────────────────────
# Lectura robusta del Excel
# ─────────────────────────────────────────────────────────────────────

def _read_excel_any(path: str) -> tuple[pd.DataFrame, int]:
    """Devuelve (df, header_row). Intenta varias filas de cabecera y elige
    la que más columnas no vacías tenga. Tolera .xlsx y .xls."""
    book = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
    best_df, best_row, best_score = None, 0, -1
    for sheet_name, df in book.items():
        for row_idx in range(min(10, len(df))):
            row = df.iloc[row_idx]
            non_empty = sum(1 for v in row if v is not None and str(v).strip() and str(v) != "nan")
            # Preferir la fila con más columnas + contenga algo parecido a "fecha" o "date"
            score = non_empty
            if any(str(v).lower().strip() in ("fecha", "date", "data") for v in row if v is not None):
                score += 50
            if score > best_score:
                best_score, best_df, best_row = score, df, row_idx
    if best_df is None:
        raise ValueError(f"No pude encontrar una fila de cabecera en {path}")
    df = pd.read_excel(path, sheet_name=0, header=best_row, dtype=object)
    df.columns = [norm_col(c) for c in df.columns]
    return df, best_row


# ─────────────────────────────────────────────────────────────────────
# Detección de instrumento
# ─────────────────────────────────────────────────────────────────────

_CTRL_SIGNATURES = {
    "MT-512E": {"camera 01", "setpoint", "refr", "defr"},
    "TC-900E": {"ambiente", "salida refr", "salida defr", "salida fans"},
    "Genérico controlador": {"setpoint", "temp"},
}

_MP_SIGNATURES = {
    "Multipower M1": {"m1: tensión", "m1: corriente", "m1: potencia activa"},
    "Multipower M2": {"m2: tensión", "m2: corriente", "m2: potencia activa"},
}
_SEMANTIC_SIGNATURES = {
    "refrigeración (controlador completo)": {
        "any": ["refr", "defr", "setpoint", "deshielo", "ambiente", "camera"],
        "min": 2,
    },
    "refrigeración (solo temperatura)": {
        "any": ["temperatura", "temp "],
        "must_not": ["refr", "setpoint", "presion", "presión", "potencia"],
        "min": 1,
    },
    "eléctrico (medidor)": {
        "any": ["potencia", "tension", "tensión", "corriente", "energia", "energía", "kwh"],
        "min": 2,
    },
    "vapor / caldera": {
        "any": ["presion", "presión", "psi", "bar", "vapor", "caldera"],
        "min": 1,
    },
}


def _semantic_category(headers: list[str]) -> str:
    text = " ".join(key(h) for h in headers)
    for cat, sig in _SEMANTIC_SIGNATURES.items():
        hits = sum(1 for w in sig["any"] if w in text)
        if hits < sig["min"]:
            continue
        if sig.get("must_not") and any(w in text for w in sig["must_not"]):
            continue
        return cat
    return "otro / no identificado"

def _match_against_specs(headers: list[str], specs) -> dict[str, str]:
    """canal -> columna del Excel, para un conjunto de specs.
    Usa match_columns (que ya conoce el catálogo del controlador) y,
    si no, comparación directa por source_col."""
    out: dict[str, str] = {}
    keys = {key(h): h for h in headers}
    # 1. match_columns (solo CONTROLLER_SPECS, pero lo intentamos igual)
    try:
        idx_map = match_columns(headers)
        for ch, idx in idx_map.items():
            if idx < len(headers):
                out[ch] = headers[idx]
    except Exception:
        pass
    # 2. comparación directa por source_col
    for spec in specs:
        want = key(spec.source_col)
        if want in keys and spec.name not in out:
            out[spec.name] = keys[want]
    return out


def _detect_instrument_type(headers: list[str]) -> dict:
    """Devuelve {type, subtype, score_ctrl, score_mp, signatures_hit}."""
    lower_headers = [key(h) for h in headers]

    def _hit(sigs: set[str]) -> bool:
        return all(any(s in h for h in lower_headers) for s in sigs)

    ctrl_hits = []
    for name, sig in _CTRL_SIGNATURES.items():
        if _hit(sig):
            ctrl_hits.append(name)

    mp_hits = []
    for name, sig in _MP_SIGNATURES.items():
        if _hit(sig):
            mp_hits.append(name)

    # Match contra specs reales
    ctrl_match = _match_against_specs(headers, CONTROLLER_SPECS)
    mp_match = _match_against_specs(headers, MULTIPOWER_SPECS)

    score_ctrl = len(ctrl_match)
    score_mp = len(mp_match)

    if score_ctrl >= 3 and score_mp < 3:
        t = "controlador"
        subtype = ctrl_hits[0] if ctrl_hits else "Genérico controlador"
    elif score_mp >= 3 and score_ctrl < 3:
        t = "multipower"
        subtype = ", ".join(mp_hits) if mp_hits else "Multipower (M1/M2)"
    elif score_ctrl >= 3 and score_mp >= 3:
        t = "mixto"
        subtype = "Controlador + Multipower en el mismo archivo"
    else:
        t = "desconocido"
        subtype = "No coincide con ningún catálogo conocido"

    return {
        "type": t,
        "subtype": subtype,
        "score_ctrl": score_ctrl,
        "score_mp": score_mp,
        "ctrl_hits": ctrl_hits,
        "mp_hits": mp_hits,
        "ctrl_match": ctrl_match,
        "mp_match": mp_match,
    }


# ─────────────────────────────────────────────────────────────────────
# Sugerencias para columnas no reconocidas
# ─────────────────────────────────────────────────────────────────────

_ALL_SPECS_BY_SOURCE = {
    key(spec.source_col): spec.name
    for spec in list(CONTROLLER_SPECS) + list(MULTIPOWER_SPECS)
}


def _suggest(ch: str) -> str | None:
    """Sugiere un canal del catálogo si la columna se parece a alguno."""
    k = key(ch)
    best, best_score = None, 0
    for src, name in _ALL_SPECS_BY_SOURCE.items():
        # Similitud simple: cuántas palabras comparten
        words_a = set(k.split())
        words_b = set(src.split())
        common = words_a & words_b
        if not common:
            continue
        score = len(common) / max(len(words_a), len(words_b))
        if score > best_score and score > 0.3:
            best, best_score = name, score
    return best


# ─────────────────────────────────────────────────────────────────────
# API principal
# ─────────────────────────────────────────────────────────────────────

def inspect_excel(path: str, config_path: str = "config.json") -> dict:
    """Analiza un Excel y devuelve un dict con el informe completo."""
    path_obj = Path(path)
    if not path_obj.exists():
        return {"ok": False, "error": f"No existe el archivo: {path}"}

    # channel_map del config (opcional)
    
    channel_map: dict[str, str] = {}
    try:
        cfg = json.load(open(config_path, encoding="utf-8"))
        channel_map = cfg.get("sitrad", {}).get("channel_map", {}) or {}
    except Exception:
        pass

    try:
        df, header_row = _read_excel_any(path)
    except Exception as exc:
        return {"ok": False, "error": f"No pude leer el Excel: {exc}"}

    headers = list(df.columns)
    headers = [h for h in headers if h and str(h) != "nan"]
    n_rows = len(df)

    # ── 1. Aplicar channel_map primero (nombres exactos de SITRAD) ──
    mapped_by_config: dict[str, str] = {}
    for bot_ch, sitrad_name in channel_map.items():
        for h in headers:
            if key(h) == key(sitrad_name):
                mapped_by_config[bot_ch] = h
                break

    # ── 2. Detectar tipo de instrumento ──
    det = _detect_instrument_type(headers)

    # ── 3. Canales mapeados (config + catálogo) ──
    all_mapped: dict[str, str] = dict(mapped_by_config)
    for ch, h in det["ctrl_match"].items():
        all_mapped.setdefault(ch, h)
    for ch, h in det["mp_match"].items():
        all_mapped.setdefault(ch, h)

    # ── 4. Columnas no reconocidas ──
    used_headers = set(all_mapped.values())
    unmatched = [h for h in headers if h not in used_headers]

    # ── 5. Sugerencias para las no reconocidas ──
    suggestions = []
    for h in unmatched:
        s = _suggest(h)
        if s:
            suggestions.append({"columna": h, "sugerencia": s})

    # ── 6. Resumen ──
    total_channels = len(CONTROLLER_SPECS) if det["type"] == "controlador" else len(MULTIPOWER_SPECS)
    if det["type"] == "mixto":
        total_channels = len(CONTROLLER_SPECS) + len(MULTIPOWER_SPECS)

    return {
        "ok": True,
        "archivo": str(path_obj),
        "filas": n_rows,
        "fila_cabecera": header_row,
        "columnas_totales": len(headers),
        "instrumento": {
            "tipo": det["type"],
            "subtipo": det["subtype"],
            "score_controlador": det["score_ctrl"],
            "score_multipower": det["score_mp"],
        },
        "mapeados_por_config": mapped_by_config,
        "mapeados_por_catalogo": {
            **det["ctrl_match"],
            **det["mp_match"],
        },
        "canales_mapeados": all_mapped,
        "total_canales_mapeados": len(all_mapped),
        "total_canales_esperados": total_channels,
        "columnas_no_reconocidas": unmatched,
        "sugerencias": suggestions,
        "categoria_semantica": _semantic_category(headers),
    }


# ─────────────────────────────────────────────────────────────────────
# Salida legible + JSON
# ─────────────────────────────────────────────────────────────────────

def render_report(rep: dict) -> str:
    if not rep.get("ok"):
        return f"ERROR: {rep.get('error')}"

    inst = rep["instrumento"]
    lines = [
        f"=== Archivo: {rep['archivo']} ===",
        f"Filas de datos: {rep['filas']} · Cabecera en fila {rep['fila_cabecera'] + 1} · "
        f"{rep['columnas_totales']} columnas",
        "",
        f"Instrumento detectado: {inst['tipo'].upper()}",
        f"  Subtipo: {inst['subtipo']}",
        f"  Score controlador: {inst['score_controlador']} canales",
        f"  Score multipower:  {inst['score_multipower']} canales",
        "",
        f"Categoría semántica: {rep.get('categoria_semantica', '?')}",
        f"Canales mapeados: {rep['total_canales_mapeados']}/{rep['total_canales_esperados']}",
    ]
    for ch in sorted(rep["canales_mapeados"]):
        src = rep["canales_mapeados"][ch]
        how = "config" if ch in rep["mapeados_por_config"] else "catálogo"
        lines.append(f"  ✓ {ch:30s} ← {src!r}   [{how}]")

    if rep["columnas_no_reconocidas"]:
        lines.append("")
        lines.append(f"Columnas NO reconocidas ({len(rep['columnas_no_reconocidas'])}):")
        for h in rep["columnas_no_reconocidas"]:
            lines.append(f"  · {h!r}")

    if rep["sugerencias"]:
        lines.append("")
        lines.append("Sugerencias para las no reconocidas:")
        for s in rep["sugerencias"]:
            lines.append(f"  · {s['columna']!r} → ¿{s['sugerencia']}?")

    return "\n".join(lines)


def run_inspect_excel(path: str, config_path: str = "config.json",
                      output_json: str | None = None) -> int:
    rep = inspect_excel(path, config_path)
    print(render_report(rep))
    if output_json:
        out = Path(output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        print(f"\nInforme JSON guardado en {out}")
    return 0 if rep.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Analiza un Excel de SITRAD.")
    ap.add_argument("path")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--output", default=None, help="Ruta del JSON de salida.")
    a = ap.parse_args()
    return run_inspect_excel(a.path, a.config, a.output)


if __name__ == "__main__":
    sys.exit(main())