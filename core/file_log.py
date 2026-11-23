from __future__ import annotations
# core.file_log
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from core.clock import local_tz
from core.contracts import ChannelReading, DataQuality, EventCandidate

logger = logging.getLogger(__name__)

# observations.20261122.jsonl  /  observations.20261122_153000.jsonl
_ROTATED_RE = re.compile(r"\.(\d{8})(?:_\d{6})?$")


class FileLog:
    """
    Persistencia histórica mediante JSONL.

    Rotación del archivo de observaciones (el de alto volumen):

        - "daily": al cambiar el día local, el archivo vigente pasa a
                   observations.YYYYMMDD.jsonl (fecha del día que contiene).
        - "size":  rota cuando el archivo supera max_file_mb
                   (observations.YYYYMMDD_HHMMSS.jsonl).
        - "none":  sin rotación.

    Tras cada rotación se conservan los `rotation_keep_days` días más
    recientes de archivos rotados; el resto se borra. La limpieza del resto
    de archivos la hace core.retention.RetentionManager.
    """

    def __init__(
        self,
        path: str = "logs/reports.jsonl",
        events_path: str = "logs/events.jsonl",
        observations_path: str = "logs/observations.jsonl",
        rotation_mode: str = "none",           # "none" | "daily" | "size"
        rotation_keep_days: int = 7,
        rotation_max_mb: float = 50.0,
        tz_name: str = "America/Bogota",
    ) -> None:
        self.path = path
        self.events_path = events_path
        self.observations_path = observations_path

        self.rotation_mode = rotation_mode
        self.rotation_keep_days = int(rotation_keep_days)
        self.rotation_max_mb = float(rotation_max_mb)
        self.local_tz = local_tz(tz_name)

        # Día local (YYYYMMDD) al que pertenece el archivo vigente.
        self._current_day: str | None = None
        self._day_checked = False

    # ------------------------------------------------------------
    # ROTACIÓN
    # ------------------------------------------------------------

    def _day_key(self, t: datetime) -> str:
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(self.local_tz).strftime("%Y%m%d")

    def _maybe_rotate(self, now: datetime | None = None) -> None:
        if self.rotation_mode == "none":
            return
        if now is None:
            now = datetime.now(timezone.utc)

        if self.rotation_mode == "daily":
            day = self._day_key(now)
            if not self._day_checked:
                # Al arrancar: ¿de qué día es lo que ya hay en el archivo?
                self._day_checked = True
                self._current_day = self._last_observation_day_key()
            if self._current_day is not None and day > self._current_day:
                self._rotate_observations(suffix=self._current_day)
                self._prune_old_observations()
            if self._current_day is None or day > self._current_day:
                self._current_day = day

        elif self.rotation_mode == "size":
            try:
                size_mb = os.path.getsize(self.observations_path) / (1024 * 1024)
            except OSError:
                return
            if size_mb >= self.rotation_max_mb:
                stamp = now.astimezone(self.local_tz).strftime("%Y%m%d_%H%M%S")
                self._rotate_observations(suffix=stamp)
                self._prune_old_observations()

    def _rotate_observations(self, suffix: str) -> None:
        if not os.path.exists(self.observations_path):
            return
        base, ext = os.path.splitext(self.observations_path)
        rotated = f"{base}.{suffix}{ext}"
        if os.path.exists(rotated):
            # Ya hay un archivo de ese día (p. ej. rotación interrumpida): no se pisa.
            n = 1
            while os.path.exists(f"{base}.{suffix}_{n:06d}{ext}"):
                n += 1
            rotated = f"{base}.{suffix}_{n:06d}{ext}"
        try:
            os.replace(self.observations_path, rotated)
            logger.info("Observaciones rotadas a %s", rotated)
        except OSError:
            # Si falla, no rompemos el pipeline. La próxima rotación lo intenta.
            logger.warning("No pude rotar %s", self.observations_path, exc_info=True)

    def rotated_files(self) -> list[tuple[str, str]]:
        """[(día YYYYMMDD, ruta)] de los archivos rotados, del más viejo al más nuevo."""
        directory = os.path.dirname(self.observations_path) or "."
        base, ext = os.path.splitext(os.path.basename(self.observations_path))
        out: list[tuple[str, str]] = []
        try:
            names = os.listdir(directory)
        except OSError:
            return out
        for name in names:
            if not (name.startswith(base + ".") and name.endswith(ext)):
                continue
            stem = name[: -len(ext)] if ext else name
            m = _ROTATED_RE.search(stem)
            if m:
                out.append((m.group(1), os.path.join(directory, name)))
        out.sort()
        return out

    def _prune_old_observations(self) -> None:
        """Borra los archivos rotados de días anteriores a `rotation_keep_days`."""
        if self.rotation_keep_days <= 0:
            return
        cutoff = (datetime.now(self.local_tz) - timedelta(days=self.rotation_keep_days)).strftime("%Y%m%d")
        for day, path in self.rotated_files():
            if day < cutoff:
                try:
                    os.remove(path)
                    logger.info("Observaciones antiguas borradas: %s", path)
                except OSError:
                    pass

    # ------------------------------------------------------------
    # ESCRITURA
    # ------------------------------------------------------------

    def write_observation(self, reading: ChannelReading) -> None:
        self.write_observations([reading])

    def write_observations(self, readings) -> None:
        """Escribe un lote abriendo el archivo una sola vez."""
        readings = list(readings)
        if not readings:
            return
        self._maybe_rotate(max(r.event_time for r in readings))
        lines = []
        for r in readings:
            rec = {"type": "observation", **self._serialize_reading(r)}
            try:
                lines.append(json.dumps(rec, ensure_ascii=False, allow_nan=False))
            except ValueError:
                logger.debug("Lectura no finita descartada del log: %s=%r", r.channel, r.value)
        if lines:
            self._append_lines(self.observations_path, lines)

    def write_event(self, event: EventCandidate, device: str = "") -> None:
        record = {
            "type": "event",
            "device": device,
            "inicio": event.inicio.isoformat(),
            "fin": event.fin.isoformat(),
            "duracion_min": event.duracion_min,
            "variables_afectadas": list(event.variables_afectadas),
            "magnitud": {key: float(value) for key, value in event.magnitud.items()},
            "patron": event.patron,
            "volvio_a_base": event.volvio_a_base,
            "evidencia": [self._serialize_reading(r) for r in event.evidencia],
        }
        self._append(self.events_path, record)

    def write(self, informe: str, timestamp: datetime, device: str, window_min: int) -> None:
        record = {
            "type": "report",
            "ts": timestamp.isoformat(),
            "device": device,
            "window_min": window_min,
            "informe": informe,
        }
        self._append(self.path, record)

    # ------------------------------------------------------------
    # LECTURA
    # ------------------------------------------------------------

    def _last_observation_day_key(self) -> str | None:
        """Día local (YYYYMMDD) de la última observación del archivo vigente.

        Usa el contenido (event_time), no el mtime, que antivirus, backups o
        montajes pueden tocar.
        """
        try:
            size = os.path.getsize(self.observations_path)
            if size == 0:
                return None
            with open(self.observations_path, "rb") as f:
                chunk = min(8192, size)
                f.seek(-chunk, os.SEEK_END)
                tail = f.read().decode("utf-8", errors="ignore")
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                et = json.loads(line).get("event_time")
                if et:
                    return self._day_key(datetime.fromisoformat(et))
            except (ValueError, AttributeError):
                continue
        return None

    def read_events(self) -> list[dict[str, Any]]:
        """Registros JSON de eventos (la conversión pertenece a Memory)."""
        return self._read_jsonl(self.events_path)

    def read_observations(self) -> list[dict[str, Any]]:
        """Registros JSON del archivo de observaciones vigente."""
        return self._read_jsonl(self.observations_path)

    def _observation_files_since(self, cutoff: datetime) -> list[str]:
        """Rotados cuyo día puede contener datos >= cutoff, y el vigente al final."""
        cutoff_day = self._day_key(cutoff)
        files = [p for day, p in self.rotated_files() if day >= cutoff_day]
        if os.path.exists(self.observations_path):
            files.append(self.observations_path)
        return files

    def iter_observations_since(
        self,
        cutoff: datetime,
        device_id: int | None = None,
    ) -> Iterator[ChannelReading]:
        """Lecturas VALID con event_time >= cutoff (vigente + rotados), en streaming.

        Tolera líneas corruptas. FROZEN/ERR/MISSING se descartan. Los
        archivos se recorren del más viejo al más nuevo, de modo que el
        resultado sale casi ordenado (cada archivo está en orden).
        """
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        valid = '"VALID"'
        dev_tag = None if device_id is None else f'"device_id": {int(device_id)},'
        for path in self._observation_files_since(cutoff):
            try:
                if os.path.getmtime(path) < cutoff.timestamp():
                    continue                      # se dejó de escribir antes del corte
                fh = open(path, "r", encoding="utf-8")
            except OSError:
                continue
            with fh:
                for line in fh:
                    # Filtros baratos antes de parsear JSON.
                    if valid not in line or (dev_tag is not None and dev_tag not in line):
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("quality") != "VALID" or rec.get("value") is None:
                            continue
                        t = datetime.fromisoformat(rec["event_time"])
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        if t < cutoff:
                            continue
                        value = float(rec["value"])
                        if not math.isfinite(value):
                            continue
                        yield ChannelReading(
                            device_id=int(rec["device_id"]), channel=str(rec["channel"]),
                            value=value, unit=str(rec.get("unit", "")),
                            quality=DataQuality.VALID, event_time=t, raw=str(rec.get("raw", "")))
                    except (KeyError, ValueError, TypeError):
                        continue

    def read_observations_since(self, cutoff: datetime, device_id: int | None = None) -> list[ChannelReading]:
        """Como iter_observations_since, pero en lista ordenada por tiempo."""
        out = list(self.iter_observations_since(cutoff, device_id))
        out.sort(key=lambda r: r.event_time)
        return out

    @staticmethod
    def _read_jsonl(path: str) -> list[dict[str, Any]]:
        if not os.path.exists(path):
            return []
        records: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Una línea corrupta no debe impedir recuperar el histórico.
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    # ------------------------------------------------------------
    # UTILIDADES
    # ------------------------------------------------------------

    @staticmethod
    def _serialize_reading(reading: ChannelReading) -> dict[str, Any]:
        return {
            "device_id": reading.device_id,
            "channel": reading.channel,
            "value": reading.value,
            "unit": reading.unit,
            "quality": reading.quality.value,
            "event_time": reading.event_time.isoformat(),
            "raw": reading.raw,
        }

    @classmethod
    def _append(cls, path: str, record: dict[str, Any]) -> None:
        cls._append_lines(path, [json.dumps(record, ensure_ascii=False, default=str)])

    @staticmethod
    def _append_lines(path: str, lines: list[str]) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as file:
            file.write("\n".join(lines))
            file.write("\n")
