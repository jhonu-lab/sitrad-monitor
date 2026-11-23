from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from core.contracts import (
    ChannelReading,
    DataQuality,
    EventCandidate,
)
from core.file_log import FileLog


from core.clock import local_tz


class HistoricalMemory:
    """
    Memoria histórica del sistema.

    Utiliza FileLog como almacenamiento persistente.

    Responsabilidades:

        - buscar eventos similares
        - resumir frecuencia histórica de eventos
        - calcular duración típica
        - identificar horas habituales
        - mantener estadísticas históricas por variable
        - calcular comportamiento típico por hora/día

    NO interpreta causas.

    NO genera informes.

    NO decide si un evento es una alarma.
    """

    def __init__(
        self,
        file_log: FileLog,
        tz_name: str = "America/Bogota",
    ) -> None:
        self.file_log = file_log
        self.local_tz = local_tz(tz_name)

        # --------------------------------------------------------
        # Estado estadístico de las variables.
        #
        # variable -> lista de valores históricos
        #
        # Actualmente se alimenta de la evidencia de eventos.
        # En una fase posterior se conectará directamente con
        # observations.jsonl para representar también el
        # comportamiento normal.
        # --------------------------------------------------------

        self._values: dict[
            str,
            list[float],
        ] = defaultdict(list)

        # --------------------------------------------------------
        # Eventos históricos cargados.
        # --------------------------------------------------------

        self._events: list[
            EventCandidate
        ] = []

        self._load_history()

    # ============================================================
    # CARGA
    # ============================================================

    def _load_history(self) -> None:
        """
        Carga el histórico existente desde FileLog.
        """

        records = self.file_log.read_events()

        for record in records:

            try:

                event = self._event_from_dict(
                    record
                )

            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue

            self._events.append(
                event
            )

            for reading in event.evidencia:

                if (
                    reading.quality
                    == DataQuality.VALID
                    and reading.value is not None
                ):

                    self._values[
                        reading.channel
                    ].append(
                        float(
                            reading.value
                        )
                    )

    # ============================================================
    # REGISTRAR EVENTO
    # ============================================================

    def record_event(
        self,
        event: EventCandidate,
        device: str = "",
    ) -> None:
        """
        Persiste y registra un nuevo EventCandidate.
        """

        self.file_log.write_event(
            event,
            device=device,
        )

        self._events.append(
            event
        )

        for reading in event.evidencia:

            if (
                reading.quality
                == DataQuality.VALID
                and reading.value is not None
            ):

                self._values[
                    reading.channel
                ].append(
                    float(
                        reading.value
                    )
                )

    # ============================================================
    # BÚSQUEDA DE EVENTOS SIMILARES
    # ============================================================

    def find_similar(
        self,
        event: EventCandidate,
    ) -> dict[str, Any]:
        """
        Resume los eventos históricos similares al evento recibido.

        Se consideran similares principalmente los eventos que
        comparten:

            - patrón
            - variables afectadas

        La similitud también considera:

            - duración
            - magnitud
            - recuperación

        Devuelve información agregada para que el Agent pueda
        interpretar la recurrencia histórica.

        Ejemplo:

        {
            "cantidad": 8,
            "duracion_tipica_min": 2.7,
            "frecuencia_por_dia": 1.33,
            "horas_habituales": [
                {
                    "hora": 14,
                    "ocurrencias": 3
                }
            ],
            "patron": "ascenso_rapido_recuperacion",
            "variables": [
                "Temp Evaporador",
                "Temp Producto"
            ]
        }
        """

        similar = self.find_similar_events(
            event,
            max_results=len(self._events),
        )

        if not similar:

            return {
                "cantidad": 0,
                "duracion_tipica_min": None,
                "frecuencia_por_dia": 0.0,
                "horas_habituales": [],
                "patron": event.patron,
                "variables": sorted(
                    event.variables_afectadas
                ),
            }

        durations = [
            historical.duracion_min
            for historical in similar
            if historical.duracion_min >= 0
        ]

        duration_typical = (
            mean(durations)
            if durations
            else None
        )

        frequency_per_day = (
            self._frequency_per_day(
                similar
            )
        )

        usual_hours = (
            self._usual_hours(
                similar
            )
        )

        return {
            "cantidad": len(similar),
            "duracion_tipica_min": (
                None
                if duration_typical is None
                else round(
                    duration_typical,
                    2,
                )
            ),
            "frecuencia_por_dia": round(
                frequency_per_day,
                2,
            ),
            "horas_habituales": usual_hours,
            "patron": event.patron,
            "variables": sorted(
                event.variables_afectadas
            ),
        }

    # ============================================================
    # FRECUENCIA POR DÍA
    # ============================================================

    @staticmethod
    def _frequency_per_day(
        events: list[EventCandidate],
    ) -> float:
        """
        Calcula la frecuencia media de eventos por día.

        Utiliza el intervalo entre la primera y última ocurrencia.
        Si todos los eventos están en el mismo día, se considera
        un período mínimo de un día.
        """

        if not events:
            return 0.0

        timestamps = [
            event.inicio
            for event in events
        ]

        first = min(timestamps)
        last = max(timestamps)

        elapsed_seconds = (
            last - first
        ).total_seconds()

        elapsed_days = (
            elapsed_seconds / 86400.0
        )

        elapsed_days = max(
            elapsed_days,
            1.0,
        )

        return len(events) / elapsed_days

    # ============================================================
    # HORAS HABITUALES
    # ============================================================

    def _usual_hours(
        self,
        events: list[EventCandidate],
    ) -> list[dict[str, int]]:
        """
        Cuenta las horas locales en las que aparecen los eventos.

        Las horas se presentan en America/Bogota.

        Devuelve como máximo las 5 horas con mayor frecuencia.
        """

        counts: dict[
            int,
            int,
        ] = defaultdict(int)

        for event in events:

            dt = event.inicio

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=ZoneInfo("UTC")
                )

            local_dt = dt.astimezone(
                self.local_tz
            )

            counts[
                local_dt.hour
            ] += 1

        ordered = sorted(
            counts.items(),
            key=lambda item: (
                -item[1],
                item[0],
            ),
        )

        return [
            {
                "hora": hour,
                "ocurrencias": count,
            }
            for hour, count in ordered[:5]
        ]

    # ============================================================
    # EVENTOS SIMILARES
    # ============================================================

    def find_similar_events(
        self,
        event: EventCandidate,
        max_results: int = 20,
    ) -> list[EventCandidate]:
        """
        Busca eventos históricos parecidos.

        La similitud se basa inicialmente en:

            1. mismo patrón
            2. variables afectadas similares
            3. magnitudes aproximadamente similares
            4. duración aproximadamente similar
            5. recuperación similar

        NO intenta determinar la causa del evento.
        """

        if max_results <= 0:
            return []

        candidates: list[
            tuple[float, EventCandidate]
        ] = []

        target_variables = set(
            event.variables_afectadas
        )

        for historical in self._events:

            score = self._similarity_score(
                event,
                historical,
                target_variables,
            )

            if score <= 0:
                continue

            candidates.append(
                (
                    score,
                    historical,
                )
            )

        candidates.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        return [
            historical
            for _, historical in candidates[
                :max_results
            ]
        ]

    # ============================================================
    # SIMILITUD
    # ============================================================

    @staticmethod
    def _similarity_score(
        event: EventCandidate,
        historical: EventCandidate,
        target_variables: set[str],
    ) -> float:
        """
        Calcula una puntuación de similitud.

        0 = no similar.

        Cuanto mayor sea el valor, más parecido es.
        """

        score = 0.0

        # --------------------------------------------------------
        # Evitar compararse consigo mismo
        # --------------------------------------------------------

        if (
            event.inicio == historical.inicio
            and event.fin == historical.fin
        ):
            return 0.0

        # --------------------------------------------------------
        # Patrón
        # --------------------------------------------------------

        if event.patron == historical.patron:

            score += 4.0

        # --------------------------------------------------------
        # Variables
        # --------------------------------------------------------

        historical_variables = set(
            historical.variables_afectadas
        )

        union = (
            target_variables
            | historical_variables
        )

        intersection = (
            target_variables
            & historical_variables
        )

        if union:

            jaccard = (
                len(intersection)
                / len(union)
            )

            score += (
                3.0 * jaccard
            )

        # --------------------------------------------------------
        # Duración
        # --------------------------------------------------------

        duration_a = max(
            event.duracion_min,
            0.001,
        )

        duration_b = max(
            historical.duracion_min,
            0.001,
        )

        duration_ratio = (
            min(
                duration_a,
                duration_b,
            )
            / max(
                duration_a,
                duration_b,
            )
        )

        score += (
            1.5
            * duration_ratio
        )

        # --------------------------------------------------------
        # Magnitudes
        #
        # EventCandidate usa claves como:
        #
        #     delta_temp_producto
        #
        # No usa directamente:
        #
        #     Temp Producto
        #
        # --------------------------------------------------------

        common_variables = (
            target_variables
            & historical_variables
        )

        if common_variables:

            magnitude_scores = []

            for variable in common_variables:

                key = (
                    "delta_"
                    + variable.lower()
                    .replace(" ", "_")
                )

                a = abs(
                    float(
                        event.magnitud.get(
                            key,
                            0.0,
                        )
                    )
                )

                b = abs(
                    float(
                        historical.magnitud.get(
                            key,
                            0.0,
                        )
                    )
                )

                if a == 0.0 and b == 0.0:

                    magnitude_scores.append(
                        1.0
                    )

                    continue

                if a == 0.0 or b == 0.0:

                    magnitude_scores.append(
                        0.0
                    )

                    continue

                magnitude_ratio = (
                    min(a, b)
                    / max(a, b)
                )

                magnitude_scores.append(
                    magnitude_ratio
                )

            if magnitude_scores:

                score += (
                    2.0
                    * mean(
                        magnitude_scores
                    )
                )

        # --------------------------------------------------------
        # Recuperación
        # --------------------------------------------------------

        if (
            event.volvio_a_base
            == historical.volvio_a_base
        ):

            score += 0.5

        return score

    # ============================================================
    # BASELINE
    # ============================================================


    # ============================================================
    # BASELINE ACTUAL
    # ============================================================


    # ============================================================
    # COMPORTAMIENTO TÍPICO
    # ============================================================


    # ============================================================
    # COMPORTAMIENTO HORARIO
    # ============================================================


    # ============================================================
    # COMPORTAMIENTO DIARIO
    # ============================================================


    # ============================================================
    # EVENTOS TÍPICOS
    # ============================================================


    # ============================================================
    # RECONSTRUIR EVENTCANDIDATE
    # ============================================================

    @staticmethod
    def _event_from_dict(
        record: dict[str, Any],
    ) -> EventCandidate:
        """
        Reconstruye un EventCandidate desde JSON.
        """

        evidence = []

        for raw_reading in record.get(
            "evidencia",
            [],
        ):

            evidence.append(
                ChannelReading(
                    device_id=int(
                        raw_reading[
                            "device_id"
                        ]
                    ),
                    channel=str(
                        raw_reading[
                            "channel"
                        ]
                    ),
                    value=(
                        None
                        if raw_reading.get(
                            "value"
                        )
                        is None
                        else float(
                            raw_reading[
                                "value"
                            ]
                        )
                    ),
                    unit=str(
                        raw_reading.get(
                            "unit",
                            "",
                        )
                    ),
                    quality=DataQuality(
                        raw_reading[
                            "quality"
                        ]
                    ),
                    event_time=datetime.fromisoformat(
                        raw_reading[
                            "event_time"
                        ]
                    ),
                    raw=str(
                        raw_reading.get(
                            "raw",
                            "",
                        )
                    ),
                )
            )

        return EventCandidate(
            inicio=datetime.fromisoformat(
                record["inicio"]
            ),
            fin=datetime.fromisoformat(
                record["fin"]
            ),
            duracion_min=float(
                record[
                    "duracion_min"
                ]
            ),
            variables_afectadas=tuple(
                record.get(
                    "variables_afectadas",
                    [],
                )
            ),
            magnitud={
                str(key): float(value)
                for key, value
                in record.get(
                    "magnitud",
                    {},
                ).items()
            },
            patron=str(
                record["patron"]
            ),
            evidencia=tuple(
                evidence
            ),
            volvio_a_base=bool(
                record.get(
                    "volvio_a_base",
                    False,
                )
            ),
        )
    def get_events(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[EventCandidate]:
        """
        Devuelve eventos históricos dentro de un intervalo.

        No realiza interpretación causal.
        """

        events = list(self._events)

        if start is not None:
            events = [
                event
                for event in events
                if event.inicio >= start
            ]

        if end is not None:
            events = [
                event
                for event in events
                if event.inicio <= end
            ]

        return sorted(
            events,
            key=lambda event: event.inicio,
        )