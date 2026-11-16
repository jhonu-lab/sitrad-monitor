from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean

from core.contracts import (
    ChannelReading,
    DataQuality,
    EventCandidate,
    Change,
)
from analysis.engine import AnalysisEngine


class EventEngine:
    """
    Detecta patrones temporales a partir de varias series.

    Este motor NO interpreta causas.

    Ejemplo:

        detecta:
            ascenso rápido + recuperación

    pero NO decide:

            "puerta abierta"

    Esa interpretación corresponde al agente.
    """

    def __init__(
        self,
        analysis: AnalysisEngine | None = None,
        config: dict | None = None,
        grouping_window_seconds: float = 60.0,
    ) -> None:
        config = config or {}
        self.analysis = analysis or AnalysisEngine()
        self.grouping_window = timedelta(
            seconds=config.get(
                "correlation_tolerance_seconds",
                grouping_window_seconds,
            )
        )

        # Margen alrededor del evento usado SOLO para clasificar la
        # forma. Permite ver la recuperación aunque 'fin' (que viene
        # de un salto entre lecturas) llegue antes de que termine.
        self.pattern_margin = timedelta(
            seconds=float(
                config.get(
                    "pattern_margin_seconds",
                    60.0,
                )
            )
        )

    # =============================================================
    # API PRINCIPAL
    # =============================================================
    def step(self, series_by_channel):
        if not series_by_channel:
            return []

        # Sólo los canales declarados en channels_que_disparan_eventos
        # pueden originar un EventCandidate.
        primary = set(self.analysis.primary_channels)

        changes_by_channel: dict[str, list[Change]] = {}

        for channel, series in series_by_channel.items():
            if primary and channel not in primary:
                continue  # ← línea clave
            changes = self.analysis.detect_changes(series)
            if changes:
                changes_by_channel[channel] = changes

        if not changes_by_channel:
            return []

        groups = self._group_changes(
            changes_by_channel
        )

        events: list[EventCandidate] = []

        for group in groups:
            event = self._build_event(
                group,
                series_by_channel,
            )

            if event is not None:
                events.append(event)

        return events

    # =============================================================
    # AGRUPACIÓN TEMPORAL
    # =============================================================

    def _group_changes(
        self,
        changes_by_channel: dict[
            str,
            list[Change],
        ],
    ) -> list[list[tuple[str, Change]]]:
        """
        Agrupa cambios de distintos canales que pertenecen
        aproximadamente al mismo fenómeno temporal.

        No requiere que todos los canales cambien.

        Ejemplo:

            Temp Producto
                20:48:55

            Potencia Activa
                20:49:02

            Temp Evaporador
                20:49:10

        pueden pertenecer al mismo candidato.
        """

        flattened: list[
            tuple[str, Change]
        ] = []

        for channel, changes in changes_by_channel.items():
            for change in changes:
                flattened.append(
                    (channel, change)
                )

        flattened.sort(
            key=lambda item: item[1].inicio
        )

        groups: list[
            list[tuple[str, Change]]
        ] = []

        for channel, change in flattened:

            placed = False

            for group in reversed(groups):
                group_end = max(
                    item[1].fin
                    for item in group
                )

                if (
                    change.inicio - group_end
                    <= self.grouping_window
                ):
                    group.append(
                        (channel, change)
                    )
                    placed = True
                    break

            if not placed:
                groups.append(
                    [(channel, change)]
                )

        return groups

    # =============================================================
    # CONSTRUCCIÓN DEL EVENTO
    # =============================================================

    def _build_event(
        self,
        group: list[tuple[str, Change]],
        series_by_channel: dict[
            str,
            list[ChannelReading],
        ],
    ) -> EventCandidate | None:

        if not group:
            return None

        inicio = min(
            change.inicio
            for _, change in group
        )

        fin = max(
            change.fin
            for _, change in group
        )

        duracion_min = (
            fin - inicio
        ).total_seconds() / 60.0

        variables = tuple(
            sorted(
                {
                    channel
                    for channel, _ in group
                }
            )
        )

        patron = self._identify_pattern(
            group,
            series_by_channel,
            inicio,
            fin,
        )

        magnitud = self._calculate_magnitudes(
            group,
            series_by_channel,
            inicio,
            fin,
        )

        evidencia = self._collect_evidence(
            series_by_channel,
            inicio,
            fin,
        )

        volvio_a_base = self._returned_to_base(
            series_by_channel,
            variables,
            inicio,
            fin,
        )

        return EventCandidate(
            inicio=inicio,
            fin=fin,
            duracion_min=round(
                duracion_min,
                2,
            ),
            variables_afectadas=variables,
            magnitud=magnitud,
            patron=patron,
            evidencia=evidencia,
            volvio_a_base=volvio_a_base,
        )

    # =============================================================
    # IDENTIFICACIÓN DEL PATRÓN
    # =============================================================

    def _identify_pattern(
        self,
        group: list[tuple[str, Change]],
        series_by_channel: dict[
            str,
            list[ChannelReading],
        ],
        inicio: datetime,
        fin: datetime,
    ) -> str:

        patterns: list[str] = []

        for channel in {channel for channel, _ in group}:
            series = self._window(
                series_by_channel[channel],
                inicio - self.pattern_margin,
                fin + self.pattern_margin,
            )

            pattern = self._classify_series_shape(
                series
            )

            if pattern:
                patterns.append(pattern)

        if not patterns:
            return "cambio_transitorio"

        # ---------------------------------------------------------
        # Prioridad:
        #
        # 1. ascenso + recuperación
        # 2. oscilación
        # 3. escalón
        # 4. deriva
        # ---------------------------------------------------------

        if (
            "ascenso_rapido_recuperacion"
            in patterns
        ):
            return "ascenso_rapido_recuperacion"

        if "ascenso_rapido_recuperacion" in patterns:
            return "ascenso_rapido_recuperacion"

        if "descenso_rapido_recuperacion" in patterns:
            return "descenso_rapido_recuperacion"

        if "oscilacion_periodica" in patterns:
            return "oscilacion_periodica"

        if "deriva_lenta" in patterns:
            return "deriva_lenta"

        if "escalon" in patterns:
            return "escalon"


        return patterns[0]

    # =============================================================
    # FORMA DE UNA SERIE
    # =============================================================

    def _classify_series_shape(
        self,
        series: list[ChannelReading],
    ) -> str | None:
        """
        Clasifica la forma temporal de una serie.

        Patrones reconocidos:

            - ascenso_rapido_recuperacion
            - descenso_rapido_recuperacion
            - oscilacion_periodica
            - escalon
            - deriva_lenta
            - cambio_transitorio

        La clasificación se realiza exclusivamente a partir de
        las lecturas. No conoce el escenario del simulador ni
        interpreta causas.
        """

        valid = [
            reading
            for reading in series
            if (
                reading.quality == DataQuality.VALID
                and reading.value is not None
            )
        ]

        if len(valid) < 6:
            return None

        valid = sorted(
            valid,
            key=lambda reading: reading.event_time,
        )

        values = [
            float(reading.value)
            for reading in valid
        ]

        inicio = values[0]
        fin = values[-1]
        pico = max(values)
        valle = min(values)

        rango = pico - valle

        # ---------------------------------------------------------
        # Sin variación suficiente no hay patrón clasificable.
        # ---------------------------------------------------------

        if rango < 0.3:
            return None

        subida_pico = pico - inicio
        recuperacion_alta = pico - fin

        bajada_valle = inicio - valle
        recuperacion_baja = fin - valle

        # =========================================================
        # 1. ASCENSO + RECUPERACIÓN
        # =========================================================

        if (
            subida_pico > rango * 0.4
            and recuperacion_alta > rango * 0.4
            and abs(fin - inicio) < rango * 0.4
        ):
            return "ascenso_rapido_recuperacion"

        # =========================================================
        # 2. DESCENSO + RECUPERACIÓN
        # =========================================================

        if (
            bajada_valle > rango * 0.4
            and recuperacion_baja > rango * 0.4
            and abs(fin - inicio) < rango * 0.4
        ):
            return "descenso_rapido_recuperacion"

        # =========================================================
        # 3. OSCILACIÓN PERIÓDICA
        # =========================================================

        if self._looks_periodic(
            valid
        ):
            return "oscilacion_periodica"

        # =========================================================
        # 4. ESCALÓN
        # =========================================================

        cambio_total = fin - inicio

        if abs(cambio_total) > rango * 0.6:
            return "escalon"

        # =========================================================
        # 5. DERIVA LENTA
        # =========================================================

        # La pendiente se calcula sobre toda la ventana ampliada.
        #
        # AnalysisEngine.slope() ya comprueba significancia
        # estadística.
        slope = self.analysis.slope(
            valid
        )

        if slope is not None:
            # Evitamos llamar deriva a un cambio que en realidad
            # es un salto muy grande.
            #
            # La deriva debe representar un cambio relativamente
            # progresivo respecto al rango total.
            total_change = abs(
                fin - inicio
            )

            if total_change >= 0.3:

                # El cambio final debe tener una dirección
                # coherente con la pendiente.
                if (
                    slope > 0
                    and fin > inicio
                ):
                    return "deriva_lenta"

                if (
                    slope < 0
                    and fin < inicio
                ):
                    return "deriva_lenta"

        # =========================================================
        # 6. CAMBIO TRANSITORIO
        # =========================================================

        return "cambio_transitorio"


    def _looks_periodic(
        self,
        series: list[ChannelReading],
    ) -> bool:
        """
        Determina si una serie presenta comportamiento oscilatorio.

        No basta con cruzar la media una vez.

        Se exige:

            1. suficientes lecturas;
            2. rango suficiente;
            3. varios cruces por la media;
            4. alternancia razonable de los valores;
            5. cierta regularidad temporal.

        El objetivo es evitar que el ruido normal se etiquete
        como oscilación periódica.
        """

        valid = [
            reading
            for reading in series
            if (
                reading.quality == DataQuality.VALID
                and reading.value is not None
            )
        ]

        if len(valid) < 8:
            return False

        values = [
            float(reading.value)
            for reading in valid
        ]

        average = mean(values)

        maximum = max(values)
        minimum = min(values)
        value_range = maximum - minimum

        if value_range < 0.3:
            return False

        # ---------------------------------------------------------
        # Detectar cruces de la media.
        # ---------------------------------------------------------

        crossings: list[datetime] = []

        previous_value = (
            values[0] - average
        )

        for reading, value in zip(
            valid[1:],
            values[1:],
        ):

            current_value = (
                value - average
            )

            # Cruce de positivo -> negativo
            # o negativo -> positivo.
            if (
                previous_value < 0
                and current_value >= 0
            ) or (
                previous_value > 0
                and current_value <= 0
            ):
                crossings.append(
                    reading.event_time
                )

            previous_value = current_value

        # ---------------------------------------------------------
        # Una oscilación debe cruzar varias veces la media.
        #
        # 4 cruces representan aproximadamente 2 ciclos.
        # ---------------------------------------------------------

        if len(crossings) < 4:
            return False

        # ---------------------------------------------------------
        # Analizar separación temporal entre cruces.
        # ---------------------------------------------------------

        intervals = []

        for previous, current in zip(
            crossings,
            crossings[1:],
        ):

            interval = (
                current - previous
            ).total_seconds()

            if interval > 0:
                intervals.append(
                    interval
                )

        if len(intervals) < 3:
            return False

        mean_interval = mean(
            intervals
        )

        if mean_interval <= 0:
            return False

        # ---------------------------------------------------------
        # Una oscilación razonablemente periódica no debería tener
        # intervalos completamente arbitrarios.
        #
        # Usamos una tolerancia relativamente amplia porque el
        # simulador tiene ruido.
        # ---------------------------------------------------------

        tolerance = 0.50

        regular_intervals = [
            interval
            for interval in intervals
            if (
                abs(
                    interval
                    - mean_interval
                )
                / mean_interval
                <= tolerance
            )
        ]

        if len(regular_intervals) < max(
            3,
            int(len(intervals) * 0.6),
        ):
            return False

        return True

    # =============================================================
    # MAGNITUDES
    # =============================================================

    def _calculate_magnitudes(
        self,
        group: list[tuple[str, Change]],
        series_by_channel: dict[str, list[ChannelReading]],
        inicio: datetime,
        fin: datetime,
    ) -> dict[str, float]:
        """
        Calcula la magnitud real de cada canal durante el evento:
        rango (max - min) dentro de la ventana, no el delta
        de un cambio puntual.
        """
        magnitudes: dict[str, float] = {}

        for channel, _ in group:

            series = self._window(
                series_by_channel[channel],
                inicio,
                fin,
            )

            values = [
                float(r.value)
                for r in series
                if r.quality == DataQuality.VALID
                and r.value is not None
            ]

            if len(values) < 2:
                continue

            rango = max(values) - min(values)

            key = (
                f"delta_{channel.lower()}"
                .replace(" ", "_")
            )

            current = magnitudes.get(key, 0.0)
            magnitudes[key] = max(current, rango)

        return {
            key: round(value, 4)
            for key, value in magnitudes.items()
        }

    # =============================================================
    # EVIDENCIA
    # =============================================================

    @staticmethod
    def _collect_evidence(
        series_by_channel: dict[
            str,
            list[ChannelReading],
        ],
        inicio: datetime,
        fin: datetime,
    ) -> tuple[ChannelReading, ...]:

        evidence: list[
            ChannelReading
        ] = []

        for series in series_by_channel.values():
            for reading in series:
                if (
                    inicio
                    <= reading.event_time
                    <= fin
                ):
                    evidence.append(
                        reading
                    )

        evidence.sort(
            key=lambda reading: reading.event_time
        )

        return tuple(evidence)

    # =============================================================
    # REGRESO A BASE
    # =============================================================

    def _returned_to_base(
        self,
        series_by_channel: dict[
            str,
            list[ChannelReading],
        ],
        variables: tuple[str, ...],
        inicio: datetime,
        fin: datetime,
    ) -> bool:

        for channel in variables:

            series = sorted(
                series_by_channel[channel],
                key=lambda reading: reading.event_time,
            )

            # Baseline: lecturas ANTERIORES al inicio del evento.
            before = [
                reading
                for reading in series
                if reading.event_time < inicio
                and reading.quality == DataQuality.VALID
                and reading.value is not None
            ]

            # Lecturas posteriores o iguales al fin del evento.
            after = [
                reading
                for reading in series
                if reading.event_time >= fin
                and reading.quality == DataQuality.VALID
                and reading.value is not None
            ]

            if len(before) < 3 or not after:
                continue

            baseline = mean(
                float(reading.value)
                for reading in before[-3:]
            )

            latest = after[-1]

            spread = self._spread(
                before[-3:]
            )

            tolerance = max(
                spread * 2.0,
                1e-9,
            )

            if abs(
                float(latest.value)
                - baseline
            ) <= tolerance:
                return True

        return False

    # =============================================================
    # UTILIDADES
    # =============================================================

    @staticmethod
    def _window(
        series: list[ChannelReading],
        inicio: datetime,
        fin: datetime,
    ) -> list[ChannelReading]:

        return [
            reading
            for reading in series
            if inicio
            <= reading.event_time
            <= fin
        ]

    @staticmethod
    def _spread(
        readings: list[ChannelReading],
    ) -> float:

        values = [
            float(reading.value)
            for reading in readings
            if (
                reading.quality == DataQuality.VALID
                and reading.value is not None)
            ]

        if len(values) < 2:
            return 0.0

        average = mean(values)

        variance = mean(
            (value - average) ** 2
            for value in values
        )

        return variance ** 0.5