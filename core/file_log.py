from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from typing import Any

from core.contracts import ChannelReading, DataQuality, EventCandidate
from core.clock import local_tz


class FileLog:
    """
    Persistencia histórica mediante JSONL.

    Añade rotación opcional del archivo de observaciones:

        - "daily": un archivo por día local.
        - "size":  rota cuando el archivo supera max_file_mb.
        - "none":  sin rotación (comportamiento histórico).

    El archivo de eventos NO rota: son pocos al día y se
    consultan por rango temporal. La rotación aplica solo a
    observations.jsonl, que es el archivo de alto volumen.
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
        self.rotation_keep_days = rotation_keep_days
        self.rotation_max_mb = rotation_max_mb
        self.local_tz = local_tz(tz_name)

        # Último día visto (para rotación diaria).
        self._last_day_key: str | None = None

    # ------------------------------------------------------------
    # ROTACIÓN
    # ------------------------------------------------------------

    def _maybe_rotate(self, now: datetime | None = None) -> None:
        if self.rotation_mode == "none":
            return

        if now is None:
            now = datetime.now(timezone.utc)

        if self.rotation_mode == "daily":
            day_key = now.astimezone(self.local_tz).strftime("%Y%m%d")
            if self._last_day_key is None:
                self._last_day_key = day_key
                return
            if day_key != self._last_day_key:
                self._rotate_observations(suffix=day_key)
                self._last_day_key = day_key
                self._prune_old_observations()

        elif self.rotation_mode == "size":
            if not os.path.exists(self.observations_path):
                return
            size_mb = os.path.getsize(self.observations_path) / (1024 * 1024)
            if size_mb >= self.rotation_max_mb:
                stamp = now.astimezone(self.local_tz).strftime("%Y%m%d_%H%M%S")
                self._rotate_observations(suffix=stamp)
                self._prune_old_observations()

    def _rotate_observations(self, suffix: str) -> None:
        if not os.path.exists(self.observations_path):
            return
        base, ext = os.path.splitext(self.observations_path)
        rotated = f"{base}.{suffix}{ext}"
        try:
            os.replace(self.observations_path, rotated)
        except OSError:
            # Si falla, no rompemos el pipeline. La próxima rotación lo intenta.
            pass

    def _prune_old_observations(self) -> None:
        if self.rotation_keep_days <= 0:
            return
        directory = os.path.dirname(self.observations_path) or "."
        base = os.path.splitext(os.path.basename(self.observations_path))[0]
        ext = os.path.splitext(self.observations_path)[1]

        candidates = []
        for name in os.listdir(directory):
            if not name.startswith(base + ".") or not name.endswith(ext):
                continue
            full = os.path.join(directory, name)
            candidates.append((os.path.getmtime(full), full))

        candidates.sort(reverse=True)
        for _, path in candidates[self.rotation_keep_days:]:
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------

    def write_observation(self, reading: ChannelReading) -> None:
        self._maybe_rotate(reading.event_time)
        record = {
            "type": "observation",
            **self._serialize_reading(reading),
        }
        self._append(self.observations_path, record)

    # write() y write_event() sin cambios.
    # ============================================================
    # LECTURA
    # ============================================================

    def read_events(self) -> list[dict[str, Any]]:
        """
        Lee todos los eventos históricos.

        Devuelve los registros JSON sin convertirlos todavía
        en EventCandidate.

        La conversión pertenece a Memory.
        """
        return self._read_jsonl(self.events_path)

    def read_observations(self) -> list[dict[str, Any]]:
        """
        Lee todas las observaciones históricas.

        Devuelve registros JSON sin convertirlos todavía
        en ChannelReading.
        """
        return self._read_jsonl(self.observations_path)

    def _observation_files(self) -> list[str]:
        """El archivo vigente y los rotados (observations.YYYYMMDD.jsonl), para reconstruir historia."""
        directory = os.path.dirname(self.observations_path) or "."
        base, ext = os.path.splitext(os.path.basename(self.observations_path))
        files = []
        try:
            for name in os.listdir(directory):
                full = os.path.join(directory, name)
                if (name.startswith(base + ".") and name.endswith(ext)
                        and os.path.abspath(full) != os.path.abspath(self.observations_path)):
                    files.append(full)             # el vigente se añade aparte: no debe contarse dos veces
        except OSError:
            pass
        files.append(self.observations_path)
        return [p for p in files if os.path.exists(p)]

    def read_observations_since(self, cutoff: datetime) -> list[ChannelReading]:
        """Lecturas VALID con event_time >= cutoff, de todos los archivos (vigente + rotados), en orden
        cronológico. Tolera líneas corruptas. FROZEN/ERR/MISSING se descartan: no aportan al store."""
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        out: list[ChannelReading] = []
        for path in self._observation_files():
            try:
                if os.path.getmtime(path) < cutoff.timestamp():
                    continue                      # el archivo dejó de escribirse antes del corte
            except OSError:
                continue
            for rec in self._read_jsonl(path):
                try:
                    if rec.get("quality") != DataQuality.VALID.value or rec.get("value") is None:
                        continue
                    t = datetime.fromisoformat(rec["event_time"])
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if t < cutoff:
                        continue
                    out.append(ChannelReading(
                        device_id=int(rec["device_id"]), channel=str(rec["channel"]), value=float(rec["value"]),
                        unit=str(rec.get("unit", "")), quality=DataQuality.VALID, event_time=t,
                        raw=str(rec.get("raw", ""))))
                except (KeyError, ValueError, TypeError):
                    continue
        out.sort(key=lambda r: r.event_time)
        return out

    # ============================================================
    # LECTURA JSONL
    # ============================================================

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
                    # Una línea corrupta no debe impedir
                    # recuperar todo el histórico.
                    continue
                if isinstance(record, dict):
                    records.append(record)

        return records

    def write_event(
        self,
        event: EventCandidate,
        device: str = "",
    ) -> None:
        record = {
            "type": "event",
            "device": device,
            "inicio": event.inicio.isoformat(),
            "fin": event.fin.isoformat(),
            "duracion_min": event.duracion_min,
            "variables_afectadas": list(event.variables_afectadas),
            "magnitud": {
                key: float(value)
                for key, value in event.magnitud.items()
            },
            "patron": event.patron,
            "volvio_a_base": event.volvio_a_base,
            "evidencia": [
                self._serialize_reading(r)
                for r in event.evidencia
            ],
        }
        self._append(self.events_path, record)

    def write(
        self,
        informe: str,
        timestamp: datetime,
        device: str,
        window_min: int,
    ) -> None:
        record = {
            "type": "report",
            "ts": timestamp.isoformat(),
            "device": device,
            "window_min": window_min,
            "informe": informe,
        }
        self._append(self.path, record)

    def write_observations(
        self,
        readings: list[ChannelReading],
    ) -> None:
        for reading in readings:
            self.write_observation(reading)

    @staticmethod
    def _serialize_reading(
        reading: ChannelReading,
    ) -> dict[str, Any]:
        return {
            "device_id": reading.device_id,
            "channel": reading.channel,
            "value": reading.value,
            "unit": reading.unit,
            "quality": reading.quality.value,
            "event_time": reading.event_time.isoformat(),
            "raw": reading.raw,
        }

    @staticmethod
    def _append(
        path: str,
        record: dict[str, Any],
    ) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as file:
            json.dump(record, file, ensure_ascii=False, allow_nan=False)
            file.write("\n")