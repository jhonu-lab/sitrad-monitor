from __future__ import annotations

# agent.context_serializer
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.contracts import (
    Baseline,
    ChannelReading,
    DataQuality,
    EventCandidate,
)


from core.clock import local_tz


class ContextSerializer:
    """
    Convierte información estructurada del sistema en contexto
    textual para el agente.

    IMPORTANTE:
    Este componente NO interpreta causas.

    Puede mostrar:
        - estadísticas
        - líneas base
        - cambios
        - patrones detectados
        - lecturas crudas
        - memoria histórica cuantitativa

    No debe afirmar:
        - puerta abierta
        - compresor encendido
        - desescarche
        - falla
        - causa física

    Esas interpretaciones pertenecen al Agent.

    Las fechas se almacenan internamente en UTC y solamente
    se convierten a America/Bogota al construir el texto.
    """
    def __init__(self, tz_name: str = "America/Bogota") -> None:
        self.local_tz = local_tz(tz_name)

    def to_local(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(self.local_tz)

    def format_local_datetime(self, dt: datetime) -> str:
        return self.to_local(dt).strftime("%d/%m/%Y %H:%M:%S")
    
    def build_context(
        self,
        metrics: dict[str, Any],
        events: list[EventCandidate],
        all_series: dict[str, list[ChannelReading]],
        device_name: str,
        history: list[dict[str, Any]] | None = None,
        profile: Any | None = None,
    ) -> str:

        lines: list[str] = []

        self._append_header(
            lines,
            metrics,
            device_name,
        )


        if events:
            self._append_events(
                lines,
                events,
            )

            # --------------------------------------------------
            # MEMORIA HISTÓRICA
            #
            # history[i] corresponde a events[i].
            # --------------------------------------------------
            self._append_history(
                lines,
                history,
            )

            if profile is not None:
                self._append_baseline_comparison(
                    lines,
                    events,
                    profile,
                )

        else:
            self._append_sensor_status(
                lines,
                all_series,
            )

            self._append_statistics(
                lines,
                metrics,
            )

            self._append_baselines(
                lines,
                metrics,
            )

        return "\n".join(lines)

    # =============================================================
    # ENCABEZADO
    # =============================================================

    def _append_header(
        cls,
        lines: list[str],
        metrics: dict[str, Any],
        device_name: str,
    ) -> None:

        lines.extend([
            f"EQUIPO: {device_name}",
            "",
        ])

        periodo_inicio = metrics.get(
            "periodo_inicio"
        )

        periodo_fin = metrics.get(
            "periodo_fin"
        )

        if (
            periodo_inicio is not None
            and periodo_fin is not None
        ):
            lines.append(
                "PERÍODO: "
                f"{cls.format_local_datetime(periodo_inicio)}"
                " → "
                f"{cls.format_local_datetime(periodo_fin)}"
                " (hora Colombia)"
            )

        duracion = metrics.get(
            "duracion_minutos"
        )

        if duracion is not None:
            lines.append(
                "DURACIÓN DEL PERÍODO: "
                f"{duracion} min"
            )

        cobertura = metrics.get(
            "cobertura_segundos"
        )

        if cobertura is not None:
            lines.append(
                "COBERTURA DE DATOS: "
                f"{cobertura} s"
            )

        lines.append("")

    # =============================================================
    # ESTADO DE SENSORES
    # =============================================================

    def _append_sensor_status(
        cls,
        lines: list[str],
        all_series: dict[str, list[ChannelReading]],
    ) -> None:

        lines.append(
            "ESTADO DE LOS SENSORES"
        )

        if not all_series:
            lines.append(
                "  Sin sensores disponibles."
            )
            lines.append("")
            return

        for channel in sorted(
            all_series
        ):

            series = all_series[
                channel
            ]

            valid = sum(
                1
                for reading in series
                if (
                    reading.quality
                    == DataQuality.VALID
                    and reading.value is not None
                )
            )

            errors = sum(
                1
                for reading in series
                if reading.quality
                == DataQuality.ERR
            )

            offline = sum(
                1
                for reading in series
                if reading.quality
                == DataQuality.OFFLINE
            )

            missing = sum(
                1
                for reading in series
                if reading.quality
                == DataQuality.MISSING
            )

            total = len(series)

            latest = cls._latest_reading(
                series
            )

            lines.append(
                f"  {channel}:"
            )

            lines.append(
                f"    Lecturas: {total}"
            )

            lines.append(
                f"    Válidas: {valid}"
            )

            lines.append(
                f"    ERR: {errors}"
            )

            lines.append(
                f"    OFFLINE: {offline}"
            )

            lines.append(
                f"    MISSING: {missing}"
            )

            if latest is not None:

                timestamp = (
                    cls.format_local_datetime(
                        latest.event_time
                    )
                )

                value = (
                    f"{latest.value}"
                    if latest.value is not None
                    else "N/A"
                )

                lines.append(
                    "    Última lectura: "
                    f"{timestamp} | "
                    f"{value} "
                    f"{latest.unit} | "
                    f"{latest.quality.value}"
                )

        lines.append("")

    # =============================================================
    # ESTADÍSTICAS
    # =============================================================

    @staticmethod
    def _append_statistics(
        lines: list[str],
        metrics: dict[str, Any],
    ) -> None:

        statistics = metrics.get(
            "stats",
            {},
        )

        if not statistics:
            return

        lines.extend([
            "ESTADÍSTICAS POR VARIABLE",
        ])

        for channel in sorted(
            statistics
        ):

            stats = statistics[
                channel
            ]

            lines.append(
                f"  {channel}:"
            )

            media = stats.get(
                "media"
            )

            if media is not None:
                lines.append(
                    f"    Media: {media}"
                )

            minimum = stats.get(
                "min"
            )

            if minimum is not None:
                lines.append(
                    f"    Mínimo: {minimum}"
                )

            maximum = stats.get(
                "max"
            )

            if maximum is not None:
                lines.append(
                    f"    Máximo: {maximum}"
                )

            std = stats.get(
                "desviacion_estandar"
            )

            if std is not None:
                lines.append(
                    "    Desviación estándar: "
                    f"{std}"
                )

            cobertura = stats.get(
                "cobertura_segundos"
            )

            if cobertura is not None:
                lines.append(
                    f"    Cobertura: {cobertura} s"
                )

        lines.append("")

    # =============================================================
    # BASELINES
    # =============================================================

    @staticmethod
    def _append_baselines(
        lines: list[str],
        metrics: dict[str, Any],
    ) -> None:

        baselines = metrics.get(
            "baselines",
            {},
        )

        if not baselines:
            return

        lines.append(
            "LÍNEAS BASE"
        )

        for channel in sorted(
            baselines
        ):

            baseline = baselines[
                channel
            ]

            if isinstance(
                baseline,
                Baseline,
            ):

                media = baseline.media

                std = (
                    baseline.desviacion_estandar
                )

                periodo = (
                    baseline.periodo_horas
                )

            elif isinstance(
                baseline,
                dict,
            ):

                media = baseline.get(
                    "media"
                )

                std = baseline.get(
                    "desviacion_estandar"
                )

                periodo = baseline.get(
                    "periodo_horas"
                )

            else:
                continue

            lines.append(
                f"  {channel}:"
            )

            lines.append(
                f"    Media: {media}"
            )

            lines.append(
                "    Desviación estándar: "
                f"{std}"
            )

            lines.append(
                "    Período utilizado: "
                f"{periodo} h"
            )

        lines.append("")

    # =============================================================
    # EVENTOS
    # =============================================================

    def _append_events(
        self,
        lines: list[str],
        events: list[EventCandidate],
    ) -> None:

        lines.append(
            "CANDIDATOS A EVENTO"
        )

        if not events:
            lines.append(
                "  No se detectaron candidatos "
                "a evento en el período."
            )

            lines.append("")
            return

        for index, event in enumerate(
            events,
            start=1,
        ):

            self._append_event(
                lines,
                index,
                event,
            )

        lines.append("")

    # =============================================================
    # EVENTO INDIVIDUAL
    # =============================================================

    def _append_event(
        cls,
        lines: list[str],
        index: int,
        event: EventCandidate,
    ) -> None:

        inicio = (
            cls.format_local_datetime(
                event.inicio
            )
        )

        fin = (
            cls.format_local_datetime(
                event.fin
            )
        )

        lines.extend([
            "",
            f"EVENTO {index}",
            f"  Inicio: {inicio}",
            f"  Fin: {fin}",
            (
                "  Duración: "
                f"{event.duracion_min} min"
            ),
            (
                "  Patrón detectado: "
                f"{event.patron}"
            ),
            (
                "  Volvió al comportamiento previo: "
                f"{'sí' if event.volvio_a_base else 'no'}"
            ),
            "",
            (
                "  RESUMEN POR VARIABLE "
                "(inicio → extremo → final):"
            ),
        ])

        by_channel: dict[
            str,
            list[ChannelReading],
        ] = {}

        for reading in event.evidencia:

            by_channel.setdefault(
                reading.channel,
                [],
            ).append(
                reading
            )

        for channel in sorted(
            by_channel
        ):

            readings = [
                reading
                for reading
                in by_channel[channel]
                if reading.value is not None
            ]

            if len(readings) < 2:
                continue

            readings.sort(
                key=lambda reading:
                reading.event_time
            )

            values = [
                float(reading.value)
                for reading in readings
            ]

            first = values[0]
            last = values[-1]

            minimum = min(values)
            maximum = max(values)

            unit = readings[0].unit

            if (
                abs(maximum - first)
                > abs(minimum - first)
            ):
                extremo = maximum
            else:
                extremo = minimum

            lines.append(
                f"    {channel}: "
                f"{first:.2f} → "
                f"{extremo:.2f} → "
                f"{last:.2f} {unit} "
                f"(rango "
                f"{minimum:.2f}.."
                f"{maximum:.2f})"
            )

    # =============================================================
    # MEMORIA HISTÓRICA
    # =============================================================

    @staticmethod
    def _append_history(
        lines: list[str],
        history: list[dict[str, Any]] | None,
    ) -> None:
        """
        Añade únicamente información cuantitativa de memoria
        histórica.

        NO interpreta causas.

        NO afirma que dos eventos tengan la misma causa.

        Solo muestra recurrencia, duración, frecuencia y
        distribución horaria.
        """

        lines.extend([
            "",
            "MEMORIA HISTÓRICA",
        ])

        if not history:
            lines.append(
                "  No existen eventos históricos similares."
            )
            lines.append("")
            return

        for index, similar in enumerate(
            history,
            start=1,
        ):

            if not isinstance(
                similar,
                dict,
            ):
                continue

            cantidad = similar.get(
                "cantidad",
                0,
            )

            duracion = similar.get(
                "duracion_tipica_min"
            )

            frecuencia = similar.get(
                "frecuencia_por_dia"
            )

            patron = similar.get(
                "patron"
            )

            lines.append(
                f"  Evento {index}:"
            )

            lines.append(
                "    Eventos similares: "
                f"{cantidad}"
            )

            if duracion is not None:
                lines.append(
                    "    Duración típica: "
                    f"{duracion} min"
                )

            if frecuencia is not None:
                lines.append(
                    "    Frecuencia histórica: "
                    f"{frecuencia} eventos/día"
                )

            if patron:
                lines.append(
                    "    Patrón histórico: "
                    f"{patron}"
                )

            hours = similar.get(
                "horas_habituales",
                [],
            )

            if hours:

                formatted_hours = []

                for item in hours:

                    if not isinstance(
                        item,
                        dict,
                    ):
                        continue

                    hour = item.get(
                        "hora"
                    )

                    occurrences = item.get(
                        "ocurrencias"
                    )

                    if (
                        hour is None
                        or occurrences is None
                    ):
                        continue

                    formatted_hours.append(
                        f"{int(hour):02d}:00 "
                        f"({occurrences})"
                    )

                if formatted_hours:

                    lines.append(
                        "    Horas habituales: "
                        + ", ".join(
                            formatted_hours
                        )
                    )

        lines.append("")

    # =============================================================
    # COMPARACIÓN CON EL PERFIL
    # =============================================================

    @classmethod
    def _append_baseline_comparison(
        cls,
        lines: list[str],
        events: list[EventCandidate],
        profile: Any,
    ) -> None:
        """
        Añade una comparación numérica contra el perfil aprendido.

        Formato por canal:
            Compresor ON 25 min; típico 6 min (p95 8 min)
        """
        if not events:
            return

        lines.extend(["", "COMPARACIÓN CON EL PERFIL"])

        seen: set[tuple[str, str]] = set()

        for event in events:
            # Racha binaria: si el evento toca un canal binario,
            # comparar duración contra on_p95_min / off_p95_min.
            for channel in event.variables_afectadas:
                bp = profile.get_binary(channel)
                if bp is None:
                    continue
                key = (channel, "racha")
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"  {channel}: "
                    f"típico ON {bp.on_median_min:.1f} min "
                    f"(p95 {bp.on_p95_min:.1f}) | "
                    f"OFF {bp.off_median_min:.1f} min "
                    f"(p95 {bp.off_p95_min:.1f}) | "
                    f"periodo {bp.period_median_min:.1f} min | "
                    f"duty {bp.fraction_on:.2f}"
                )

            # Temperatura: comparar media global y σ.
            for channel in event.variables_afectadas:
                tp = profile.get_temperature(channel)
                if tp is None:
                    continue
                key = (channel, "temperatura")
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"  {channel}: "
                    f"media {tp.overall_mean:.2f} °C | "
                    f"σ {tp.overall_std:.2f} | "
                    f"n={tp.n_samples}"
                )

        lines.append("")

    # =============================================================
    # HALLAZGOS
    # =============================================================


    # =============================================================
    # UTILIDADES
    # =============================================================

    @staticmethod
    def _latest_reading(
        series: list[ChannelReading],
    ) -> ChannelReading | None:

        if not series:
            return None

        return max(
            series,
            key=lambda reading:
            reading.event_time,
        )