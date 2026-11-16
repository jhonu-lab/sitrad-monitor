from __future__ import annotations

from datetime import timedelta
from math import sqrt
from core.fastmath import mean, stdev
from typing import Sequence

from core.contracts import (
    Baseline,
    ChannelReading,
    Change,
    DataQuality,
)


class AnalysisEngine:
    """
    Caja de herramientas matemática para el agente.

    Esta clase NO:
    - interpreta causas;
    - genera informes;
    - envía mensajes;
    - decide si un evento es una alarma;
    - asigna nombres humanos a eventos.

    Solo transforma series de datos en información matemática.

    detect_changes() reconoce dos tipos principales de estructura:

    1. Cambios bruscos:
       diferencia importante entre lecturas consecutivas.

    2. Cambios progresivos:
       diferencia sostenida entre medias de ventanas temporales.

    Esto permite detectar tanto:

        20.0 -> 21.5

    como:

        20.0 -> 20.1 -> 20.2 -> 20.3 -> ...

    sin interpretar todavía qué causó el cambio.
    """

    def __init__(
        self,
        config: dict | None = None,
    ) -> None:

        config = config or {}

        # =========================================================
        # SIGNIFICANCIA ESTADÍSTICA
        # =========================================================

        self.SIGNIFICANCE_Z = float(
            config.get(
                "min_significance_slope",
                1.96,
            )
        )

        # =========================================================
        # DETECCIÓN DE CAMBIOS
        # =========================================================

        # Umbral general para cambios de canales no primarios.
        #
        # Evita considerar cada paso de cuantización como un
        # evento independiente.
        self.min_change_delta = float(
            config.get(
                "min_change_delta",
                0.10,
            )
        )

        # Umbral específico para los canales que disparan eventos.
        #
        # Por ejemplo:
        #
        # Temp Producto -> 0.5 °C
        #
        # Esto NO significa que todos los canales necesiten 0.5.
        self.min_delta_primary = float(
            config.get(
                "min_delta_canal_primario",
                0.5,
            )
        )

        self.primary_channels = tuple(
            config.get(
                "channels_que_disparan_eventos",
                (),
            )
        )

        # Número de puntos utilizados para comparar ventanas.
        #
        # Con polling de 5 s:
        #
        # 6 puntos ≈ 30 segundos.
        #
        # Por tanto se compara aproximadamente:
        #
        # ventana A: 30 s
        # ventana B: 30 s
        #
        self.change_window_points = int(
            config.get(
                "change_window_points",
                6,
            )
        )

        if self.change_window_points < 2:
            self.change_window_points = 2

        # Multiplicador de dispersión utilizado para detectar
        # cambios consecutivos claramente superiores al ruido.
        self.change_sigma = float(
            config.get(
                "change_sigma",
                3.0,
            )
        )

        # Separación temporal máxima permitida entre dos lecturas
        # consecutivas antes de considerar que existe un hueco.
        #
        # Si el polling es de 5 s, 60 s es deliberadamente amplio.
        self.max_change_gap_seconds = float(
            config.get(
                "max_change_gap_seconds",
                60.0,
            )
        )

        # Cambio mínimo entre medias de ventanas para considerar
        # que existe una variación progresiva.
        #
        # Si no se especifica, se utiliza el mismo umbral general.
        self.min_window_delta = float(
            config.get(
                "min_window_delta",
                self.min_change_delta,
            )
        )

    # =============================================================
    # ESTADÍSTICAS
    # =============================================================

    def stats(
        self,
        series: Sequence[ChannelReading],
    ) -> dict:
        """
        Calcula estadísticas descriptivas de una serie.

        Devuelve:
            media
            mínimo
            máximo
            desviación estándar
            cobertura en segundos
            cantidad de lecturas válidas
        """

        valid = self._valid_readings(series)

        if not valid:
            return {
                "n": 0,
                "media": None,
                "min": None,
                "max": None,
                "desviacion_estandar": None,
                "cobertura_segundos": 0.0,
            }

        valid = sorted(
            valid,
            key=lambda reading: reading.event_time,
        )

        values = [
            float(reading.value)
            for reading in valid
        ]

        timestamps = [
            reading.event_time
            for reading in valid
        ]

        return {
            "n": len(values),
            "media": mean(values),
            "min": min(values),
            "max": max(values),
            "desviacion_estandar": (
                stdev(values)
                if len(values) >= 2
                else 0.0
            ),
            "cobertura_segundos": (
                timestamps[-1] - timestamps[0]
            ).total_seconds(),
        }

    # =============================================================
    # PENDIENTE
    # =============================================================

    def slope(
        self,
        series: Sequence[ChannelReading],
    ) -> float | None:
        """
        Calcula la pendiente lineal de la serie en unidades/hora.

        Devuelve None cuando la pendiente no es estadísticamente
        significativa.

        No interpreta si la pendiente es buena, mala, normal
        o anormal.
        """

        valid = self._valid_readings(series)

        if len(valid) < 3:
            return None

        valid = sorted(
            valid,
            key=lambda reading: reading.event_time,
        )

        t0 = valid[0].event_time

        x = [
            (
                reading.event_time - t0
            ).total_seconds() / 3600.0
            for reading in valid
        ]

        y = [
            float(reading.value)
            for reading in valid
        ]

        x_mean = mean(x)
        y_mean = mean(y)

        numerator = sum(
            (xi - x_mean) * (yi - y_mean)
            for xi, yi in zip(x, y)
        )

        denominator = sum(
            (xi - x_mean) ** 2
            for xi in x
        )

        if denominator <= 0:
            return None

        slope = numerator / denominator

        # ---------------------------------------------------------
        # Error estándar de la pendiente.
        # ---------------------------------------------------------

        residuals = [
            yi - (
                y_mean
                + slope * (xi - x_mean)
            )
            for xi, yi in zip(x, y)
        ]

        degrees_of_freedom = len(valid) - 2

        if degrees_of_freedom <= 0:
            return None

        residual_variance = (
            sum(
                residual ** 2
                for residual in residuals
            )
            / degrees_of_freedom
        )

        slope_standard_error = sqrt(
            residual_variance / denominator
        )

        if slope_standard_error == 0:
            return slope

        t_stat = abs(
            slope / slope_standard_error
        )

        if t_stat < self.SIGNIFICANCE_Z:
            return None

        return slope

    # =============================================================
    # BASELINE
    # =============================================================

    def baseline(
        self,
        series: Sequence[ChannelReading],
        hours: float,
    ) -> Baseline:
        """
        Calcula la línea base estadística de una serie.

        'hours' representa el período histórico solicitado.
        """

        if hours <= 0:
            raise ValueError(
                "hours debe ser mayor que cero."
            )

        valid = self._valid_readings(series)

        if not valid:
            raise ValueError(
                "No hay lecturas válidas para construir la baseline."
            )

        values = [
            float(reading.value)
            for reading in valid
        ]

        variable = valid[0].channel

        return Baseline(
            variable=variable,
            media=mean(values),
            desviacion_estandar=(
                stdev(values)
                if len(values) >= 2
                else 0.0
            ),
            periodo_horas=hours,
        )

    # =============================================================
    # CAMBIOS
    # =============================================================

    def detect_changes(
        self,
        series: Sequence[ChannelReading],
    ) -> list[Change]:
        """
        Detecta cambios matemáticos en una serie.

        Se utilizan dos mecanismos:

        1. Cambios bruscos:
           diferencia entre muestras consecutivas.

        2. Cambios progresivos:
           diferencia entre la media de una ventana y la media
           de la ventana inmediatamente anterior.

        No interpreta la causa del cambio.

        Ejemplo de cambio brusco:

            20.0
            20.0
            21.5

        Ejemplo de rampa:

            20.0
            20.1
            20.2
            20.3
            20.4
            20.5
            ...

        """

        valid = self._valid_readings(series)

        if len(valid) < 3:
            return []

        valid = sorted(
            valid,
            key=lambda reading: reading.event_time,
        )

        changes: list[Change] = []

        # =========================================================
        # 1. CAMBIOS BRUSCOS
        # =========================================================

        abrupt_changes = self._detect_abrupt_changes(
            valid
        )

        changes.extend(
            abrupt_changes
        )
        

        # =========================================================
        # 2. CAMBIOS PROGRESIVOS
        # =========================================================

        if len(valid) >= (
            self.change_window_points * 2
        ):
            ramp_changes = self._detect_window_changes(
                valid
            )

            changes.extend(
                ramp_changes
            )
        # =========================================================
        # 3. DERIVAS LENTAS
        # =========================================================
        #
        # _detect_drift recibe los cambios ya detectados y decide
        # internamente si la serie ya está explicada por ellos.

        drift_changes = self._detect_drift(
            valid,
            changes,
        )

        changes.extend(
            drift_changes
        )

        # =========================================================
        # ORDENAR Y ELIMINAR DUPLICADOS
        # =========================================================

        changes.sort(
            key=lambda change: (
                change.inicio,
                change.fin,
            )
        )

        return self._deduplicate_changes(
            changes
        )

    # =============================================================
    # CAMBIOS BRUSCOS
    # =============================================================

    def _detect_abrupt_changes(
        self,
        valid: list[ChannelReading],
    ) -> list[Change]:
        """
        Detecta saltos entre lecturas consecutivas.

        La detección combina:

            1. umbral estadístico;
            2. umbral mínimo absoluto.

        Esto evita que una señal cuantizada de 0.1 °C produzca
        eventos constantemente debido a pequeñas diferencias.
        """

        if len(valid) < 3:
            return []

        deltas: list[float] = []

        for previous, current in zip(
            valid,
            valid[1:],
        ):
            dt = (
                current.event_time
                - previous.event_time
            ).total_seconds()

            # Ignorar diferencias atravesando huecos grandes.
            if (
                dt <= 0
                or dt > self.max_change_gap_seconds
            ):
                continue

            deltas.append(
                float(current.value)
                - float(previous.value)
            )

        if not deltas:
            return []

        delta_mean = mean(deltas)

        if len(deltas) >= 2:
            delta_std = stdev(deltas)
        else:
            delta_std = 0.0

        statistical_threshold = (
            abs(delta_mean)
            + (
                self.change_sigma
                * delta_std
            )
        )

        changes: list[Change] = []

        for previous, current in zip(valid, valid[1:]):
            dt = (
                current.event_time
                - previous.event_time
            ).total_seconds()

            if (
                dt <= 0
                or dt > self.max_change_gap_seconds
            ):
                continue

            delta = (
                float(current.value)
                - float(previous.value)
            )

            # -----------------------------------------------------
            # Umbral mínimo según el canal.
            # -----------------------------------------------------

            min_delta = self._minimum_delta_for_channel(
                previous.channel
            )

            # -----------------------------------------------------
            # Para un cambio brusco deben cumplirse AMBOS:
            #
            # 1. superar el umbral estadístico;
            # 2. superar el mínimo absoluto.
            #
            # Así evitamos que un 0.1 cuantizado sea considerado
            # importante simplemente porque la señal estaba casi
            # completamente quieta.
            # -----------------------------------------------------

            if abs(delta) < min_delta:
                continue

            if abs(delta) < statistical_threshold:
                continue

            changes.append(
                Change(
                    inicio=previous.event_time,
                    fin=current.event_time,
                    tipo="escalon",
                    delta=delta,
                    valor_inicial=float(
                        previous.value
                    ),
                    valor_final=float(
                        current.value
                    ),
                )
            )

        return changes

    # =============================================================
    # CAMBIOS DE VENTANA / RAMPAS
    # =============================================================

    def _detect_window_changes(
        self,
        valid: list[ChannelReading],
    ) -> list[Change]:
        """
        Detecta cambios progresivos comparando dos ventanas
        consecutivas.

        Ejemplo con N=6:

            ventana anterior:
                [20.0, 20.1, 20.1, 20.2, 20.2, 20.3]

            ventana actual:
                [20.3, 20.4, 20.4, 20.5, 20.5, 20.6]

        Se compara:

            media_actual - media_anterior

        en lugar de depender de una sola diferencia de 0.1.

        La ventana avanza punto a punto.
        """

        window_size = self.change_window_points

        if len(valid) < window_size * 2:
            return []

        changes: list[Change] = []

        # ---------------------------------------------------------
        # Para no reaccionar a la primera pequeña diferencia,
        # exigimos que la diferencia entre ventanas supere el
        # mínimo absoluto.
        # ---------------------------------------------------------

        for index in range(
            window_size * 2 - 1,
            len(valid),
        ):

            previous_start = (
                index
                - (window_size * 2)
                + 1
            )

            previous_end = (
                index
                - window_size
                + 1
            )

            current_start = previous_end
            current_end = index + 1

            previous_window = valid[
                previous_start:previous_end
            ]

            current_window = valid[
                current_start:current_end
            ]

            if (
                len(previous_window)
                < window_size
                or len(current_window)
                < window_size
            ):
                continue

            # -----------------------------------------------------
            # Comprobar huecos temporales.
            # -----------------------------------------------------

            if not self._window_is_contiguous(
                previous_window
            ):
                continue

            if not self._window_is_contiguous(
                current_window
            ):
                continue

            previous_mean = mean(
                float(reading.value)
                for reading in previous_window
            )

            current_mean = mean(
                float(reading.value)
                for reading in current_window
            )

            delta = (
                current_mean
                - previous_mean
            )

            channel = valid[index].channel

            min_delta = max(
                self._minimum_delta_for_channel(
                    channel
                ),
                self.min_window_delta,
            )

            if abs(delta) < min_delta:
                continue

            # -----------------------------------------------------
            # Comprobar que el cambio es consistente.
            #
            # No basta con que la media cambie.
            #
            # Se calcula la dispersión combinada de ambas
            # ventanas.
            # -----------------------------------------------------

            previous_values = [
                float(reading.value)
                for reading in previous_window
            ]

            current_values = [
                float(reading.value)
                for reading in current_window
            ]

            combined_values = (
                previous_values
                + current_values
            )

            # El ruido se estima con las diferencias punto a punto:
            # en una rampa la desviación estándar de los valores
            # incluye la propia rampa y ocultaría el cambio real.
            step_diffs = [
                b - a
                for a, b in zip(
                    combined_values,
                    combined_values[1:],
                )
            ]

            if len(step_diffs) >= 2:
                noise = stdev(
                    step_diffs
                )
            else:
                noise = 0.0

            # -----------------------------------------------------
            # Si el ruido de la señal es demasiado grande respecto
            # al cambio, no considerarlo todavía una estructura
            # clara.
            #
            # Para señales muy estables:
            #
            #     delta = 0.5
            #     noise = 0.02
            #
            # claramente detectable.
            #
            # Para señales muy ruidosas:
            #
            #     delta = 0.5
            #     noise = 0.5
            #
            # no necesariamente.
            # -----------------------------------------------------

            if noise > 0:
                significance_threshold = (
                    self.change_sigma
                    * noise
                )

                if abs(delta) < significance_threshold:
                    continue

            previous = previous_window[0]
            current = current_window[-1]

            changes.append(
                Change(
                    inicio=previous.event_time,
                    fin=current.event_time,
                    tipo="cambio_tendencia",
                    delta=delta,
                    valor_inicial=previous_mean,
                    valor_final=current_mean,
                )
            )

        return changes
    # =============================================================
    # DERIVAS LENTAS
    # =============================================================
    def _detect_drift(
        self,
        valid: list[ChannelReading],
        existing_changes: list[Change],
    ) -> list[Change]:
        """
        Detecta derivas lentas comparando la media de los primeros
        N puntos con la media de los últimos N puntos de la serie.

        No se aplica si la serie ya está explicada por cambios
        locales:

            - muchos cambios (escalones/rampas): serie estructurada,
              no una deriva monótona.
            - un cambio con magnitud comparable al delta acumulado:
              la deriva sería redundante.
        """

        # Series cortas no son fiables para estimar deriva.
        if len(valid) < 20:
            return []

        # ---------------------------------------------------------
        # Guarda 1: si la serie ya tiene varios cambios, no es
        # una deriva. Es una serie estructurada.
        # ---------------------------------------------------------

        if len(existing_changes) >= 3:
            return []

        # N: al menos 5 puntos, como máximo 1/4 de la serie.
        n = max(5, len(valid) // 4)

        first = valid[:n]
        last = valid[-n:]

        first_mean = mean(float(r.value) for r in first)
        last_mean = mean(float(r.value) for r in last)
        delta = last_mean - first_mean

        channel = valid[0].channel
        min_delta = self._minimum_delta_for_channel(channel)

        if abs(delta) < min_delta:
            return []

        # ---------------------------------------------------------
        # Guarda 2: si algún cambio existente ya explica la mayor
        # parte del delta acumulado, la deriva sería redundante.
        # ---------------------------------------------------------

        max_existing = max(
            (abs(c.delta) for c in existing_changes),
            default=0.0,
        )

        if max_existing >= abs(delta) * 0.5:
            return []

        # ---------------------------------------------------------
        # Descartar si hay un escalón dominante interno.
        # ---------------------------------------------------------

        max_step = 0.0
        for previous, current in zip(valid, valid[1:]):
            dt = (
                current.event_time - previous.event_time
            ).total_seconds()
            if dt <= 0 or dt > self.max_change_gap_seconds:
                continue
            step = abs(
                float(current.value) - float(previous.value)
            )
            if step > max_step:
                max_step = step

        if max_step > abs(delta) * 0.5:
            return []

        # ---------------------------------------------------------
        # Descartar oscilaciones: si el signo del cambio en la
        # primera mitad y en la segunda son opuestos, no es
        # una deriva monótona.
        # ---------------------------------------------------------

        half = len(valid) // 2
        first_half = (
            float(valid[half - 1].value)
            - float(valid[0].value)
        )
        second_half = (
            float(valid[-1].value)
            - float(valid[half].value)
        )

        if first_half * second_half < 0:
            return []

        return [
            Change(
                inicio=valid[0].event_time,
                fin=valid[-1].event_time,
                tipo="deriva",
                delta=delta,
                valor_inicial=first_mean,
                valor_final=last_mean,
            )
        ]
    # =============================================================
    # UMBRAL POR CANAL
    # =============================================================

    def _minimum_delta_for_channel(
        self,
        channel: str,
    ) -> float:
        """
        Devuelve el cambio mínimo absoluto aplicable al canal.

        Los canales primarios utilizan el umbral específico:

            min_delta_canal_primario

        Los demás utilizan:

            min_change_delta
        """

        if channel in self.primary_channels:
            return self.min_delta_primary

        return self.min_change_delta

    # =============================================================
    # CONTINUIDAD TEMPORAL
    # =============================================================

    def _window_is_contiguous(
        self,
        window: Sequence[ChannelReading],
    ) -> bool:
        """
        Comprueba que no existan huecos temporales excesivos
        dentro de una ventana.
        """

        if len(window) < 2:
            return True

        for previous, current in zip(
            window,
            window[1:],
        ):
            dt = (
                current.event_time
                - previous.event_time
            ).total_seconds()

            if (
                dt <= 0
                or dt > self.max_change_gap_seconds
            ):
                return False

        return True

    # =============================================================
    # DEDUPLICACIÓN
    # =============================================================

    def _deduplicate_changes(
        self,
        changes: list[Change],
    ) -> list[Change]:
        """
        Evita devolver múltiples cambios prácticamente
        idénticos producidos por ventanas deslizantes.

        No intenta fusionar eventos completos.

        Solo elimina detecciones que:

        - comienzan muy cerca;
        - terminan muy cerca;
        - tienen prácticamente la misma magnitud;
        - tienen el mismo sentido.

        Esto permite que EventEngine posteriormente agrupe
        fenómenos distintos.
        """

        if not changes:
            return []

        result: list[Change] = []

        merge_window = timedelta(
            seconds=max(
                10.0,
                self.max_change_gap_seconds,
            )
        )

        for change in changes:

            duplicate = False

            for previous in reversed(result):

                if (
                    change.inicio
                    - previous.inicio
                    > merge_window
                ):
                    break

                same_direction = (
                    (
                        change.delta >= 0
                        and previous.delta >= 0
                    )
                    or (
                        change.delta < 0
                        and previous.delta < 0
                    )
                )

                if not same_direction:
                    continue

                start_difference = abs(
                    (
                        change.inicio
                        - previous.inicio
                    ).total_seconds()
                )

                end_difference = abs(
                    (
                        change.fin
                        - previous.fin
                    ).total_seconds()
                )

                magnitude_difference = abs(
                    abs(change.delta)
                    - abs(previous.delta)
                )

                if (
                    change.tipo == previous.tipo          # ← NUEVA LÍNEA
                    and start_difference
                    <= merge_window.total_seconds()
                    and end_difference
                    <= merge_window.total_seconds()
                    and magnitude_difference
                    <= max(
                        self.min_change_delta,
                        0.01,
                    )
                ):
                    duplicate = True
                    break

            if not duplicate:
                result.append(change)

        return result

    # =============================================================
    # ENERGÍA
    # =============================================================

    def energy(
        self,
        power_series: Sequence[ChannelReading],
        unit: str,
    ) -> float:
        """
        Integra una serie de potencia respecto al tiempo.

        unit:
            "kW"  -> resultado en kWh
            "kVA" -> resultado en kVAh

        Utiliza integración trapezoidal.

        Los huecos excesivamente grandes se ignoran para no
        inventar consumo entre dos lecturas alejadas.
        """

        normalized_unit = unit.strip().lower()

        if normalized_unit not in {
            "kw",
            "kva",
        }:
            raise ValueError(
                "unit debe ser 'kW' o 'kVA'."
            )

        valid = self._valid_readings(
            power_series
        )

        if len(valid) < 2:
            return 0.0

        valid = sorted(
            valid,
            key=lambda reading: reading.event_time,
        )

        energy = 0.0

        max_gap_hours = (
            self.max_change_gap_seconds
            / 3600.0
        )

        for previous, current in zip(
            valid,
            valid[1:],
        ):

            dt_hours = (
                current.event_time
                - previous.event_time
            ).total_seconds() / 3600.0

            if dt_hours <= 0:
                continue

            # Evitar integrar artificialmente a través de huecos.
            if dt_hours > max_gap_hours:
                continue

            p1 = float(previous.value)
            p2 = float(current.value)

            energy += (
                (p1 + p2) / 2.0
            ) * dt_hours

        return energy

    # =============================================================
    # CORRELACIÓN
    # =============================================================

    def correlate(
        self,
        series_a: Sequence[ChannelReading],
        series_b: Sequence[ChannelReading],
    ) -> float:
        """
        Calcula el coeficiente de correlación de Pearson.

        Nota:
        esta implementación mantiene el comportamiento actual
        de emparejar las series por posición.

        La alineación temporal precisa podrá sustituirse después
        por interpolación/resampling o coincidencia temporal.
        """

        a = self._valid_readings(series_a)
        b = self._valid_readings(series_b)

        if len(a) < 2 or len(b) < 2:
            return 0.0

        a = sorted(
            a,
            key=lambda reading: reading.event_time,
        )

        b = sorted(
            b,
            key=lambda reading: reading.event_time,
        )

        n = min(
            len(a),
            len(b),
        )

        a = a[:n]
        b = b[:n]

        values_a = [
            float(reading.value)
            for reading in a
        ]

        values_b = [
            float(reading.value)
            for reading in b
        ]

        mean_a = mean(values_a)
        mean_b = mean(values_b)

        numerator = sum(
            (x - mean_a) * (y - mean_b)
            for x, y in zip(
                values_a,
                values_b,
            )
        )

        denominator_a = sqrt(
            sum(
                (x - mean_a) ** 2
                for x in values_a
            )
        )

        denominator_b = sqrt(
            sum(
                (y - mean_b) ** 2
                for y in values_b
            )
        )

        denominator = (
            denominator_a
            * denominator_b
        )

        if denominator == 0:
            return 0.0

        return numerator / denominator

    # =============================================================
    # UTILIDADES INTERNAS
    # =============================================================

    @staticmethod
    def _valid_readings(
        series: Sequence[ChannelReading],
    ) -> list[ChannelReading]:
        """
        Filtra únicamente lecturas numéricas y válidas.
        """

        return [
            reading
            for reading in series
            if (
                reading.quality
                == DataQuality.VALID
                and reading.value is not None
            )
        ]