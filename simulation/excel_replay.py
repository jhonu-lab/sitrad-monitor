"""
simulation/excel_replay.py — repite un historial real de SITRAD (Excel/CSV) como si estuviera ocurriendo.

El bot no sabe que es un archivo: recibe las lecturas fila por fila, al ritmo del reloj de repetición
(`ReplayClock`), y reacciona igual que en vivo (alertas, informes, preguntas). Sirve para demostrarle al
ingeniero el bot sobre datos REALES de su cava, y para probarlo sin conexión a SITRAD.

Formato esperado (exportación del controlador): una columna de fecha-hora y estas columnas (los espacios
dobles y mayúsculas da igual):
    camera 01 (°C) · Setpoint (°C) · Desvío relativo al Setpoint (°C) · Proceso actual · Puerta abierta ·
    DEFR · REFR · SALIDA HOT
Los estados pueden venir como número (0/1) o texto (Conectado/Desconectado, Ligado, ON/OFF, Sí/No, ...).
"Proceso actual" (texto) se ignora. Si la exportación trae filas de título antes de la tabla, se detectan.

Fechas: "10/3/2026 10:49:02" se interpreta mes/día/año o día/mes/año según cuál produzca filas
consecutivas (2 minutos) en vez de saltos de un mes (date_order="auto"). Las horas se toman como hora local.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from core.channel_catalog import CONTROLLER_SPECS
from core.clock import Clock, local_tz
from core.contracts import ChannelReading, DataQuality, Snapshot

logger = logging.getLogger(__name__)

from core.sitrad_mapping import key as _key, match_columns as _match_columns, parse_number as _num, parse_state as _state  # noqa: E402


# ----------------------------------------------------------------------
# Lectura del archivo
# ----------------------------------------------------------------------

def _read_raw(path: str) -> list[pd.DataFrame]:
    if str(path).lower().endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "latin-1"):
            try:
                return [pd.read_csv(path, header=None, sep=None, engine="python", encoding=enc, dtype=object)]
            except Exception:
                continue
        raise ValueError(f"No pude leer {path} como CSV.")
    book = pd.read_excel(path, header=None, sheet_name=None, dtype=object)
    return list(book.values())


def _find_table(frames: list[pd.DataFrame]):
    for df in frames:
        for r in range(min(len(df), 80)):
            cols = _match_columns(list(df.iloc[r].values))
            if len(cols) >= 3:
                return df, r, cols
    raise ValueError("No encontré la tabla: busco una fila con columnas como 'Setpoint', 'REFR', 'DEFR', "
                     "'camera 01'. Revisa que sea la exportación del controlador.")


_FMT = {
    "mdy": ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p"),
    "dmy": ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M:%S", "%d/%m/%Y %I:%M:%S %p"),
}
_ISO = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")


def _to_dt(v, order: str):
    if isinstance(v, (datetime, pd.Timestamp)):
        return None if pd.isna(v) else pd.Timestamp(v).to_pydatetime()
    if isinstance(v, (int, float)) and not pd.isna(v) and 20000 < float(v) < 80000:      # serial de Excel
        return (pd.Timestamp("1899-12-30") + pd.to_timedelta(float(v), unit="D")).to_pydatetime()
    if isinstance(v, str):
        s = re.sub(r"\s+", " ", v.strip())
        for f in _FMT[order] + _ISO:
            try:
                return datetime.strptime(s, f)
            except ValueError:
                continue
    return None


def _parse_times(values, order: str = "auto"):
    """Devuelve (lista de datetime|None, orden usado). 'auto' elige el que da pasos regulares."""
    if order in ("mdy", "dmy"):
        return [_to_dt(v, order) for v in values], order
    best = None
    for o in ("mdy", "dmy"):
        ts = [_to_dt(v, o) for v in values]
        valid = [t for t in ts if t is not None]
        if len(valid) < max(2, 0.9 * len(values)):
            continue
        steps = sorted(abs((b - a).total_seconds()) for a, b in zip(valid, valid[1:]))
        big = sum(1 for s in steps if s > 6 * 3600)                    # saltos de horas = orden equivocado
        score = (big, steps[len(steps) // 2] if steps else 0)
        if best is None or score < best[0]:
            best = (score, ts, o)
    if best is None:
        raise ValueError("No pude interpretar la columna de fecha y hora.")
    return best[1], best[2]


def load_sitrad_table(path: str, tz_name: str = "America/Bogota", date_order: str = "auto"):
    """
    Lee el historial. Devuelve (filas, info): filas = [(instante UTC, {canal: valor|None})] en orden,
    info = resumen (columnas usadas e ignoradas, orden de fecha, rango, paso mediano, estados vistos).
    """
    df, hrow, cols = _find_table(_read_raw(path))
    headers = list(df.iloc[hrow].values)
    body = df.iloc[hrow + 1:]
    tcol = next((i for i, h in enumerate(headers) if h is not None and re.search(
        r"fecha|data|hora|date|time", _key(h))), None)
    if tcol is None or tcol in cols.values():
        tcol = 0
    times, used_order = _parse_times(list(body.iloc[:, tcol].values), date_order)
    tz = local_tz(tz_name)
    spec_by = {s.name: s for s in CONTROLLER_SPECS}
    rows, bad, states_seen = [], 0, {}
    for t, (_, rec) in zip(times, body.iterrows()):
        if t is None:
            bad += 1
            continue
        t_utc = (t.replace(tzinfo=tz) if t.tzinfo is None else t).astimezone(timezone.utc)
        vals = {}
        for ch, ci in cols.items():
            raw = rec.iloc[ci]
            if spec_by[ch].kind == "state":
                vals[ch] = _state(raw)
                states_seen.setdefault(ch, set()).add(str(raw))
            else:
                vals[ch] = _num(raw)
        rows.append((t_utc, vals))
    rows.sort(key=lambda r: r[0])
    dedup, last = [], None
    for t, v in rows:
        if t != last:
            dedup.append((t, v))
        last = t
    rows = dedup
    if not rows:
        raise ValueError("La tabla no tiene filas con fecha válida.")
    steps = sorted((b[0] - a[0]).total_seconds() for a, b in zip(rows, rows[1:]))
    info = {
        "filas": len(rows), "filas_sin_fecha": bad, "orden_fecha": used_order,
        "primera": rows[0][0].astimezone(tz), "ultima": rows[-1][0].astimezone(tz),
        "paso_mediano_s": steps[len(steps) // 2] if steps else None,
        "huecos_mayores_10min": sum(1 for s in steps if s > 600),
        "columnas_usadas": {ch: str(headers[ci]).strip() for ch, ci in cols.items()},
        "columnas_ignoradas": [str(h).strip() for i, h in enumerate(headers)
                               if i not in cols.values() and i != tcol and h is not None and str(h) != "nan"],
        "estados_vistos": {k: sorted(v)[:6] for k, v in states_seen.items()},
        "sin_valor": {ch: sum(1 for _, v in rows if v.get(ch) is None) for ch in cols},
    }
    return rows, info


# ----------------------------------------------------------------------
# Reloj y fuente
# ----------------------------------------------------------------------

class ReplayClock(Clock):
    """Reloj que parte del primer instante del historial y avanza `speed` veces más rápido que el real."""

    def __init__(self, start: datetime, speed: float = 24.0) -> None:
        self._start = start
        self._speed = float(speed)
        self._t0 = time.monotonic()

    def now(self) -> datetime:
        return self._start + timedelta(seconds=(time.monotonic() - self._t0) * self._speed)


class RowsReplaySource:
    """Entrega filas ya leídas [(instante UTC, {canal: valor|None})] según el reloj. Misma interfaz que CavaRandomSource."""

    def __init__(self, rows, info: dict, device_id: int, stop_at_end: bool = True, end_grace_polls: int = 4) -> None:
        self.device_id = int(device_id)
        self.rows, self.info = rows, info
        self.start_time: datetime = self.rows[0][0]
        self.end_time: datetime = self.rows[-1][0]
        self._i = 0
        self._spec = {s.name: s for s in CONTROLLER_SPECS}
        self.stop_at_end = stop_at_end
        self._grace = int(end_grace_polls)
        self._finished_polls = 0
        logger.info("Historial cargado: %d filas, %s → %s (paso mediano %ss, orden de fecha %s).",
                    info["filas"], info["primera"].strftime("%d/%m/%Y %H:%M:%S"),
                    info["ultima"].strftime("%d/%m/%Y %H:%M:%S"), info.get("paso_mediano_s"),
                    info.get("orden_fecha", "-"))

    @property
    def finished(self) -> bool:
        return self._i >= len(self.rows)

    def _reading(self, ch: str, value, t: datetime) -> ChannelReading:
        spec = self._spec[ch]
        if value is None:
            return ChannelReading(self.device_id, ch, None, spec.unit, DataQuality.MISSING, t)
        return ChannelReading(self.device_id, ch, float(value), spec.unit, DataQuality.VALID, t)

    def poll(self, now: datetime) -> Snapshot:
        out: list[ChannelReading] = []
        while self._i < len(self.rows) and self.rows[self._i][0] <= now:
            t, vals = self.rows[self._i]
            out.extend(self._reading(ch, v, t) for ch, v in vals.items())
            self._i += 1
        if self.finished:
            self._finished_polls += 1
            if self._finished_polls == 1:
                logger.info("Historial terminado: se repitieron %d filas.", len(self.rows))
            if self.stop_at_end and self._finished_polls > self._grace:
                raise SystemExit("Historial terminado: se repitió todo el archivo. Fin de la demostración.")
        return Snapshot(source_id="sitrad_replay", processing_time=now, readings=tuple(out))


class ExcelReplaySource(RowsReplaySource):
    """Historial exportado de SITRAD en Excel/CSV."""

    def __init__(self, path: str, device_id: int, tz_name: str = "America/Bogota",
                 date_order: str = "auto", stop_at_end: bool = True, end_grace_polls: int = 4) -> None:
        rows, info = load_sitrad_table(path, tz_name, date_order)
        super().__init__(rows, info, device_id, stop_at_end, end_grace_polls)
