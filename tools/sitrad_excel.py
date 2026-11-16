"""
tools/sitrad_excel.py — carga informes Excel de SITRAD como ChannelReading.

Sirve para (a) auditar el pipeline con la estructura real de los
instrumentos y (b) reproducir datos en pruebas. Requiere pandas y openpyxl.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import timezone
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, ".")
from core.channel_catalog import (  # noqa: E402
    CONTROLLER_SPECS, MULTIPOWER_SPECS, ChannelSpec, norm_col,
)
from core.contracts import ChannelReading, DataQuality  # noqa: E402

_STATE = {"conectado": 1.0, "desconectado": 0.0}


@dataclass
class LoadReport:
    instrument: str
    total_columns: int = 0
    included: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)   # columna -> motivo
    rows: int = 0


def _read(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, header=3)
    df.columns = [norm_col(c) for c in df.columns]
    df = df[df["Fecha"].notna()].copy()
    df["Fecha"] = pd.to_datetime(df["Fecha"])
    return df.sort_values("Fecha").reset_index(drop=True)


def _reason(df: pd.DataFrame, col: str) -> str:
    s = pd.to_numeric(df[col], errors="coerce")
    if s.notna().sum() == 0:
        txt = df[col].dropna()
        return "vacía" if txt.empty else "texto no numérico (redundante)"
    if s.nunique() <= 1:
        return f"constante ({s.dropna().iloc[0]})"
    return "redundante (por fase / línea / demanda por fase)"


def load_instrument(
    path: str,
    specs: tuple[ChannelSpec, ...],
    device_id: int,
    tz_name: str = "America/Bogota",
) -> tuple[list[ChannelReading], LoadReport]:
    df = _read(path)
    inst = specs[0].instrument
    rep = LoadReport(instrument=inst, total_columns=len(df.columns) - 1, rows=len(df))
    tz = ZoneInfo(tz_name)
    by_col = {s.source_col: s for s in specs}

    for col in df.columns:
        if col != "Fecha" and col not in by_col:
            rep.excluded[col] = _reason(df, col)

    readings: list[ChannelReading] = []
    for col, spec in by_col.items():
        if col not in df.columns:
            rep.excluded[f"(ausente) {col}"] = "no existe en el archivo"
            continue
        rep.included.append(spec.name)
        for t, raw in zip(df["Fecha"], df[col]):
            ts = t.to_pydatetime().replace(tzinfo=tz).astimezone(timezone.utc)
            if spec.kind == "state":
                v = _STATE.get(str(raw).strip().lower())
            else:
                v = pd.to_numeric(raw, errors="coerce")
                v = None if pd.isna(v) else float(v)
            if v is None:
                readings.append(ChannelReading(device_id, spec.name, None, spec.unit,
                                               DataQuality.MISSING, ts, str(raw)))
            else:
                readings.append(ChannelReading(device_id, spec.name, float(v), spec.unit,
                                               DataQuality.VALID, ts, str(raw)))
    return readings, rep


def load_all(controller_xlsx: str, multipower_xlsx: str, device_id: int = 201):
    r1, rep1 = load_instrument(controller_xlsx, CONTROLLER_SPECS, device_id)
    r2, rep2 = load_instrument(multipower_xlsx, MULTIPOWER_SPECS, device_id)
    allr = sorted(r1 + r2, key=lambda r: r.event_time)
    return allr, (rep1, rep2)
