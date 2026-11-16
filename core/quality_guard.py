from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from core.contracts import ChannelReading, DataQuality


@dataclass(frozen=True)
class QualityIssue:
    channel: str
    consecutive_bad: int
    since: datetime
    qualities: tuple[str, ...]


class QualityGuard:
    """
    Capa entre la fuente y SeriesStore.

    Dos funciones independientes:

        1. apply(reading) → reading
           Degrada a FROZEN cuando detecta N lecturas consecutivas
           con el mismo valor. Streaming, mantiene estado interno
           por canal.

        2. issues(readings) → list[QualityIssue]
           Agrupa rachas finales con quality ≠ VALID. Batch,
           stateless.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}

        self.frozen_exempt = frozenset(
            config.get("flat_signal_exempt_channels", ())
        )

        self.min_consecutive_bad = int(
            config.get("quality_guard_min_consecutive_bad", 3)
        )
        self.frozen_min_samples = int(
            config.get("quality_guard_frozen_min_samples", 120)
        )

        # Estado para apply().
        self._last_value: dict[str, float] = {}
        self._same_count: dict[str, int] = {}

    # ========================================================
    # APPLY (streaming)
    # ========================================================

    def apply(self, reading: ChannelReading) -> ChannelReading:
        """
        Devuelve la lectura, posiblemente con quality=FROZEN si
        el canal lleva frozen_min_samples lecturas con el mismo
        valor.

        No modifica la lectura original: los ChannelReading son
        inmutables. Devuelve una copia cuando hay downgrade.
        """
        # Sin valor → resetear contador para ese canal.
        if reading.value is None:
            self._reset(reading.channel)
            return reading

        if reading.channel in self.frozen_exempt or reading.unit == "estado":
            self._reset(reading.channel)
            return reading

        value = float(reading.value)
        last = self._last_value.get(reading.channel)

        if last is not None and last == value:
            count = self._same_count.get(reading.channel, 1) + 1
        else:
            count = 1

        self._last_value[reading.channel] = value
        self._same_count[reading.channel] = count

        if (
            count >= self.frozen_min_samples
            and reading.quality == DataQuality.VALID
        ):
            return replace(reading, quality=DataQuality.FROZEN)

        return reading

    # ========================================================
    # ISSUES (batch)
    # ========================================================

    def issues(
        self,
        readings: list[ChannelReading],
    ) -> list[QualityIssue]:
        by_channel: dict[str, list[ChannelReading]] = {}
        for r in readings:
            by_channel.setdefault(r.channel, []).append(r)

        issues: list[QualityIssue] = []
        for channel, rs in by_channel.items():
            rs = sorted(rs, key=lambda x: x.event_time)
            trailing: list[ChannelReading] = []
            for r in reversed(rs):
                if r.quality != DataQuality.VALID:
                    trailing.append(r)
                else:
                    break
            if len(trailing) < self.min_consecutive_bad:
                continue
            trailing.reverse()
            issues.append(QualityIssue(
                channel=channel,
                consecutive_bad=len(trailing),
                since=trailing[0].event_time,
                qualities=tuple(sorted({r.quality.value for r in trailing})),
            ))
        return issues

    # ========================================================
    # UTILIDADES
    # ========================================================

    def _reset(self, channel: str) -> None:
        self._last_value.pop(channel, None)
        self._same_count.pop(channel, None)