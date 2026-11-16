from __future__ import annotations
# core.series_store
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    Almacena lecturas recientes y las agrupa en buckets de 1 minuto.

    Las estadísticas de temperatura se calculan usando cobertura temporal:
    cada lectura representa el intervalo comprendido entre su timestamp
    y el timestamp de la siguiente lectura.

    Una lectura VALID aporta a:
      - mean
      - minimum
      - maximum
      - covered_seconds

    Los periodos sin lectura VALID se contabilizan como gap_seconds.
    """

    BUCKET_SECONDS = 60

    def __init__(self, max_hours: float = 4.0) -> None:
        if max_hours <= 0:
            raise ValueError("max_hours debe ser mayor que cero.")

        self.max_hours = float(max_hours)
        self.max_age = timedelta(hours=self.max_hours)

        self._readings: dict[
            tuple[int, str],
            list[ChannelReading],
        ] = {}

    # ------------------------------------------------------------------
    # INGESTA
    # ------------------------------------------------------------------

    def ingest(self, reading: ChannelReading) -> None:
        """
        Incorpora una lectura.

        Las lecturas con timestamp anterior a la última almacenada
        se ignoran para evitar romper la continuidad temporal.
        """

        key = (reading.device_id, reading.channel)

        series = self._readings.setdefault(key, [])

        if series and reading.event_time < series[-1].event_time:
            return

        series.append(reading)

        self._prune(reading.event_time)

    # ------------------------------------------------------------------
    # CONSULTA
    # ------------------------------------------------------------------

    def latest(
        self,
        device_id: int,
        channel: str,
    ) -> ChannelReading | None:
        """
        Devuelve la última lectura disponible.
        """

        series = self._readings.get((device_id, channel))

        if not series:
            return None

        return series[-1]

    def latest_value(
        self,
        device_id: int,
        channel: str,
    ) -> float | None:
        """
        Devuelve el último valor numérico disponible de una lectura
        VALID o FROZEN.
        """

        reading = self.latest(device_id, channel)

        if reading is None:
            return None

        if reading.value is None:
            return None

        if reading.quality not in {
            DataQuality.VALID,
            DataQuality.FROZEN,
        }:
            return None

        return reading.value

    def buckets(
        self,
        device_id: int,
        channel: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bucket]:
        """
        Construye y devuelve buckets de 1 minuto.

        Si start/end no se proporcionan, se utilizan todas las lecturas
        disponibles de la serie.
        """

        series = self._readings.get((device_id, channel), [])

        if not series:
            return []

        readings = series

        if start is not None:
            readings = [
                reading
                for reading in readings
                if reading.event_time >= start
            ]

        if end is not None:
            readings = [
                reading
                for reading in readings
                if reading.event_time <= end
            ]

        if not readings:
            return []

        return self._build_buckets(
            readings,
            start=start,
            end=end,
        )

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

    def _prune(self, now: datetime) -> None:
        """
        Elimina lecturas anteriores a max_hours.
        """

        cutoff = now - self.max_age

        for key, series in list(self._readings.items()):
            first_valid_index = 0

            while (
                first_valid_index < len(series)
                and series[first_valid_index].event_time < cutoff
            ):
                first_valid_index += 1

            if first_valid_index:
                self._readings[key] = series[first_valid_index:]

            if not self._readings[key]:
                del self._readings[key]

    def get_series(
        self,
        device_id: int,
        channel: str,
        start: datetime | None = None,
        end: datetime | None = None,
        minutes: float | None = None,
    ) -> list[ChannelReading]:
        """
        Devuelve las lecturas de un canal concreto.

        Se puede consultar mediante:
            - start/end
            - minutes

        Si se utiliza minutes y no se proporciona end,
        se toma como referencia la última lectura disponible
        del canal.
        """

        if minutes is not None and minutes <= 0:
            raise ValueError(
                "minutes debe ser mayor que cero."
            )

        series = self._readings.get(
            (device_id, channel),
            [],
        )

        if not series:
            return []

        readings = list(series)

        # ---------------------------------------------------------
        # Determinar ventana por minutos
        # ---------------------------------------------------------

        if minutes is not None:

            reference = (
                end
                if end is not None
                else readings[-1].event_time
            )

            start = (
                reference
                - timedelta(minutes=minutes)
            )

        # ---------------------------------------------------------
        # Aplicar límites
        # ---------------------------------------------------------

        if start is not None:
            readings = [
                reading
                for reading in readings
                if reading.event_time >= start
            ]

        if end is not None:
            readings = [
                reading
                for reading in readings
                if reading.event_time <= end
            ]

        return readings
    def all_series(
        self,
        minutes: float | None = None,
    ) -> dict[str, list[ChannelReading]]:
        """
        Devuelve todas las series disponibles agrupadas por
        dispositivo y canal.

        Las claves tienen el formato:

            "{device_id}:{channel}"

        Ejemplo:

            {
                "201:Temp Producto": [...],
                "201:Temp Evaporador": [...],
                "202:Temp Producto": [...],
            }

        Si minutes se especifica, solamente devuelve las lecturas
        correspondientes a esa ventana temporal.
        """

        if minutes is not None and minutes <= 0:
            raise ValueError(
                "minutes debe ser mayor que cero."
            )

        latest = max(
            (
                series[-1].event_time
                for series in self._readings.values()
                if series
            ),
            default=None,
        )

        if latest is None:
            return {}

        if minutes is None:
            cutoff = None
        else:
            cutoff = (
                latest
                - timedelta(minutes=minutes)
            )

        result: dict[
            str,
            list[ChannelReading],
        ] = {}

        for (
            device_id,
            channel,
        ), readings in self._readings.items():

            if cutoff is not None:
                selected = [
                    reading
                    for reading in readings
                    if reading.event_time >= cutoff
                ]
            else:
                selected = list(readings)

            if not selected:
                continue

            key = f"{device_id}:{channel}"

            result[key] = selected

        return result

    def channels(
        self,
        device_id: int | None = None,
    ) -> list[str]:
        """
        Devuelve los nombres de canal conocidos.

        Si device_id es None:
            devuelve todos los canales con formato
            "device_id:channel".

        Si se proporciona device_id:
            devuelve solamente los canales de ese dispositivo.
        """

        if device_id is None:
            return sorted(
                f"{d}:{c}"
                for (d, c) in self._readings.keys()
            )

        return sorted(
            c
            for (d, c) in self._readings.keys()
            if d == device_id
        )

    @staticmethod
    def _floor_to_minute(timestamp: datetime) -> datetime:
        """
        Redondea hacia abajo al inicio del minuto.
        """

        return timestamp.replace(
            second=0,
            microsecond=0,
        )
