from __future__ import annotations
# core.series_store
import threading
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from core.contracts import ChannelReading, DataQuality


@dataclass(frozen=True)
class Bucket:
    start: datetime
    end: datetime
    mean: float | None
    minimum: float | None
    maximum: float | None
    covered_seconds: float
    gap_seconds: float
    quality: DataQuality


class SeriesStore:
    """
    Almacena lecturas recientes por (device_id, canal) y las agrupa en buckets de 1 minuto.

    Las estadísticas de temperatura se calculan usando cobertura temporal:
    cada lectura representa el intervalo comprendido entre su timestamp
    y el timestamp de la siguiente lectura.

    Retención:
        * max_hours          para los canales "largos" (catalogados).
        * short_max_hours    para el resto (passthrough), si se indica
                             `is_long`. Con 137 canales a 30 s, 7 días de
                             passthrough son ~2,7 M objetos en memoria.

    Concurrencia: el ciclo principal escribe y el hilo del agente lee.
    Todas las operaciones toman un lock y las consultas devuelven copias.
    """

    BUCKET_SECONDS = 60
    # La poda se hace por lotes: solo cuando lo más viejo supera el corte
    # en más de este margen (evita mover la lista en cada lectura).
    PRUNE_SLACK = timedelta(minutes=10)
    # Barrido completo (series de canales que dejaron de reportar).
    SWEEP_EVERY = timedelta(minutes=30)

    def __init__(
        self,
        max_hours: float = 4.0,
        short_max_hours: float | None = None,
        is_long: Callable[[str], bool] | None = None,
    ) -> None:
        if max_hours <= 0:
            raise ValueError("max_hours debe ser mayor que cero.")

        self.max_hours = float(max_hours)
        self.max_age = timedelta(hours=self.max_hours)
        if short_max_hours is not None and is_long is not None:
            self.short_max_age = timedelta(hours=min(float(short_max_hours), self.max_hours))
        else:
            self.short_max_age = self.max_age
        self._is_long = is_long

        self._readings: dict[tuple[int, str], list[ChannelReading]] = {}
        self._times: dict[tuple[int, str], list[datetime]] = {}
        self._max_age_of: dict[tuple[int, str], timedelta] = {}
        self._lock = threading.RLock()
        self._last_sweep: datetime | None = None
        self._latest: datetime | None = None

    # ------------------------------------------------------------------
    # INGESTA
    # ------------------------------------------------------------------

    def _age_for(self, key: tuple[int, str]) -> timedelta:
        age = self._max_age_of.get(key)
        if age is None:
            if self._is_long is None or self._is_long(key[1]):
                age = self.max_age
            else:
                age = self.short_max_age
            self._max_age_of[key] = age
        return age

    def ingest(self, reading: ChannelReading) -> None:
        """
        Incorpora una lectura.

        Las lecturas con timestamp igual o anterior a la última almacenada
        se ignoran (duplicados al reiniciar / continuidad temporal).
        """
        key = (reading.device_id, reading.channel)
        t = reading.event_time
        with self._lock:
            times = self._times.get(key)
            if times is None:
                times = self._times[key] = []
                self._readings[key] = []
            elif times and t <= times[-1]:
                return
            times.append(t)
            self._readings[key].append(reading)
            if self._latest is None or t > self._latest:
                self._latest = t

            # Poda por lotes de ESTA serie.
            cutoff = t - self._age_for(key)
            if times[0] < cutoff - self.PRUNE_SLACK:
                self._cut(key, cutoff)

            if self._last_sweep is None:
                self._last_sweep = t
            elif t - self._last_sweep >= self.SWEEP_EVERY:
                self._last_sweep = t
                self._sweep(t)

    def ingest_many(self, readings) -> None:
        for r in readings:
            self.ingest(r)

    def _cut(self, key: tuple[int, str], cutoff: datetime) -> None:
        times = self._times[key]
        i = bisect_left(times, cutoff)
        if i:
            del times[:i]
            del self._readings[key][:i]
        if not times:
            del self._times[key]
            del self._readings[key]

    def _sweep(self, now: datetime) -> None:
        """Poda todas las series (incluidas las que ya no reciben datos)."""
        for key in list(self._times):
            self._cut(key, now - self._age_for(key))

    def _prune(self, now: datetime) -> None:
        """Compatibilidad: poda completa respecto a `now`."""
        with self._lock:
            self._sweep(now)

    # ------------------------------------------------------------------
    # CONSULTA
    # ------------------------------------------------------------------

    def latest(self, device_id: int, channel: str) -> ChannelReading | None:
        """Devuelve la última lectura disponible."""
        with self._lock:
            series = self._readings.get((device_id, channel))
            return series[-1] if series else None

    def latest_value(self, device_id: int, channel: str) -> float | None:
        """Último valor numérico de una lectura VALID o FROZEN."""
        reading = self.latest(device_id, channel)
        if reading is None or reading.value is None:
            return None
        if reading.quality not in {DataQuality.VALID, DataQuality.FROZEN}:
            return None
        return reading.value

    def latest_times(self, device_id: int) -> dict[str, datetime]:
        """Canal -> timestamp de su última lectura (para el chequeo de salud)."""
        with self._lock:
            return {c: t[-1] for (d, c), t in self._times.items() if d == device_id and t}

    def _slice(self, key, start, end) -> list[ChannelReading]:
        times = self._times.get(key)
        if not times:
            return []
        i = bisect_left(times, start) if start is not None else 0
        j = bisect_right(times, end) if end is not None else len(times)
        return self._readings[key][i:j]

    def buckets(
        self,
        device_id: int,
        channel: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bucket]:
        """Buckets de 1 minuto (todas las lecturas si no hay start/end)."""
        with self._lock:
            readings = self._slice((device_id, channel), start, end)
        if not readings:
            return []
        return self._build_buckets(readings, start=start, end=end)

    # ------------------------------------------------------------------
    # BUCKETS
    # ------------------------------------------------------------------

    def _build_buckets(
        self,
        readings: list[ChannelReading],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bucket]:
        """
        Agrupa las lecturas en ventanas de 1 minuto.

        La media es ponderada por tiempo:

            mean =
                sum(valor * segundos_cubiertos)
                / sum(segundos_cubiertos)

        Cada lectura representa el intervalo hasta la siguiente lectura.

        Ejemplo con polling de 5 s:

            12:00:00 -> -18.0
            12:00:05 -> -18.2
            12:00:10 -> -18.1

        Entonces:

            -18.0 representa 5 s
            -18.2 representa 5 s
            -18.1 representa el siguiente intervalo
        """

        if not readings:
            return []

        readings = sorted(
            readings,
            key=lambda reading: reading.event_time,
        )

        first_time = readings[0].event_time
        last_time = readings[-1].event_time

        bucket_start = self._floor_to_minute(
            start if start is not None else first_time
        )

        bucket_end_limit = (
            end
            if end is not None
            else last_time
        )

        if bucket_end_limit < bucket_start:
            return []

        buckets: list[Bucket] = []

        index = 0

        while bucket_start <= bucket_end_limit:
            bucket_end = bucket_start + timedelta(
                seconds=self.BUCKET_SECONDS
            )

            bucket_readings = []

            while index < len(readings):
                reading = readings[index]

                if reading.event_time >= bucket_end:
                    break

                if reading.event_time >= bucket_start:
                    bucket_readings.append(reading)

                index += 1

            bucket = self._build_single_bucket(
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                readings=bucket_readings,
            )

            buckets.append(bucket)

            bucket_start = bucket_end

        return buckets

    def _build_single_bucket(
        self,
        bucket_start: datetime,
        bucket_end: datetime,
        readings: list[ChannelReading],
    ) -> Bucket:
        """
        Construye un bucket individual.
        """

        if not readings:
            return Bucket(
                start=bucket_start,
                end=bucket_end,
                mean=None,
                minimum=None,
                maximum=None,
                covered_seconds=0.0,
                gap_seconds=self.BUCKET_SECONDS,
                quality=DataQuality.MISSING,
            )

        valid_readings = [
            reading
            for reading in readings
            if reading.quality == DataQuality.VALID
            and reading.value is not None
        ]

        if valid_readings:
            weighted_sum = 0.0
            covered_seconds = 0.0

            minimum = min(
                reading.value
                for reading in valid_readings
                if reading.value is not None
            )

            maximum = max(
                reading.value
                for reading in valid_readings
                if reading.value is not None
            )

            for index, reading in enumerate(valid_readings):
                interval_start = max(
                    reading.event_time,
                    bucket_start,
                )

                if index + 1 < len(valid_readings):
                    next_time = valid_readings[index + 1].event_time
                else:
                    next_time = bucket_end

                interval_end = min(
                    next_time,
                    bucket_end,
                )

                duration = (
                    interval_end - interval_start
                ).total_seconds()

                if duration <= 0:
                    continue

                weighted_sum += (
                    float(reading.value) * duration
                )

                covered_seconds += duration

            if covered_seconds > 0:
                mean = weighted_sum / covered_seconds
            else:
                mean = None

            gap_seconds = max(
                0.0,
                self.BUCKET_SECONDS - covered_seconds,
            )

            return Bucket(
                start=bucket_start,
                end=bucket_end,
                mean=mean,
                minimum=minimum,
                maximum=maximum,
                covered_seconds=covered_seconds,
                gap_seconds=gap_seconds,
                quality=DataQuality.VALID,
            )

        has_err = any(
            reading.quality == DataQuality.ERR
            for reading in readings
        )

        if has_err:
            return Bucket(
                start=bucket_start,
                end=bucket_end,
                mean=None,
                minimum=None,
                maximum=None,
                covered_seconds=0.0,
                gap_seconds=self.BUCKET_SECONDS,
                quality=DataQuality.ERR,
            )

        return Bucket(
            start=bucket_start,
            end=bucket_end,
            mean=None,
            minimum=None,
            maximum=None,
            covered_seconds=0.0,
            gap_seconds=self.BUCKET_SECONDS,
            quality=DataQuality.MISSING,
        )

    # ------------------------------------------------------------------
    # RETENCIÓN
    # ------------------------------------------------------------------

    def get_series(
        self,
        device_id: int,
        channel: str,
        start: datetime | None = None,
        end: datetime | None = None,
        minutes: float | None = None,
    ) -> list[ChannelReading]:
        """
        Lecturas de un canal, por start/end o por minutes.

        Con minutes y sin end, la referencia es la última lectura del canal.
        """
        if minutes is not None and minutes <= 0:
            raise ValueError("minutes debe ser mayor que cero.")
        key = (device_id, channel)
        with self._lock:
            times = self._times.get(key)
            if not times:
                return []
            if minutes is not None:
                reference = end if end is not None else times[-1]
                start = reference - timedelta(minutes=minutes)
            return self._slice(key, start, end)

    def device_series(
        self,
        device_id: int,
        minutes: float | None = None,
        channels: Callable[[str], bool] | None = None,
    ) -> dict[str, list[ChannelReading]]:
        """
        Canal -> lecturas de UN dispositivo en la ventana `minutes`, contada
        desde la última lectura de ese dispositivo. `channels` filtra canales.
        """
        if minutes is not None and minutes <= 0:
            raise ValueError("minutes debe ser mayor que cero.")
        with self._lock:
            keys = [k for k in self._times if k[0] == device_id
                    and (channels is None or channels(k[1]))]
            if not keys:
                return {}
            cutoff = None
            if minutes is not None:
                latest = max(self._times[k][-1] for k in keys)
                cutoff = latest - timedelta(minutes=minutes)
            out: dict[str, list[ChannelReading]] = {}
            for k in keys:
                sel = self._slice(k, cutoff, None)
                if sel:
                    out[k[1]] = sel
            return out

    def all_series(
        self,
        minutes: float | None = None,
    ) -> dict[str, list[ChannelReading]]:
        """
        Todas las series con claves "{device_id}:{channel}".

        Con minutes, la ventana se cuenta desde la última lectura global.
        Para un solo dispositivo es mucho más barato `device_series`.
        """
        if minutes is not None and minutes <= 0:
            raise ValueError("minutes debe ser mayor que cero.")
        with self._lock:
            if self._latest is None:
                return {}
            cutoff = None if minutes is None else self._latest - timedelta(minutes=minutes)
            result: dict[str, list[ChannelReading]] = {}
            for (device_id, channel) in self._times:
                sel = self._slice((device_id, channel), cutoff, None)
                if sel:
                    result[f"{device_id}:{channel}"] = sel
            return result

    def channels(self, device_id: int | None = None) -> list[str]:
        """
        Nombres de canal conocidos ("device_id:channel" si device_id es None).
        """
        with self._lock:
            keys = list(self._times)
        if device_id is None:
            return sorted(f"{d}:{c}" for (d, c) in keys)
        return sorted(c for (d, c) in keys if d == device_id)

    def device_ids(self) -> list[int]:
        with self._lock:
            return sorted({d for d, _ in self._times})

    @staticmethod
    def _floor_to_minute(timestamp: datetime) -> datetime:
        """
        Redondea hacia abajo al inicio del minuto.
        """

        return timestamp.replace(
            second=0,
            microsecond=0,
        )
