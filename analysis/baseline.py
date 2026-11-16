from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from core.fastmath import mean, stdev
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from analysis.engine import AnalysisEngine
from analysis.memory import HistoricalMemory
from core.contracts import (
    Baseline,
    ChannelReading,
    DataQuality,
)
from core.file_log import FileLog


from core.clock import local_tz


# ============================================================
# RESULTADOS
# ============================================================

@dataclass(frozen=True)
class BinaryProfile:
    """Perfil de un canal binario (p. ej. Estado Compresor)."""
    channel: str
    fraction_on: float
    on_median_min: float
    on_p95_min: float
    off_median_min: float
    off_p95_min: float
    period_median_min: float
    period_p95_min: float
    n_samples: int
    n_on_streaks: int
    n_off_streaks: int


@dataclass(frozen=True)
class TemperatureProfile:
    """Perfil de un canal de temperatura (°C)."""
    channel: str
    overall_mean: float
    overall_std: float
    by_hour: dict[int, tuple[float, float]]
    n_samples: int


@dataclass(frozen=True)
class RecoveryProfile:
    """Tiempo típico de recuperación por patrón de evento."""
    by_pattern: dict[str, float]
    n_events: int


# ============================================================
# HELPERS
# ============================================================

def _safe_median(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def _safe_percentile(values: list[float], p: float) -> float:
    """Percentil por interpolación lineal inclusiva."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(s[int(k)])
    return float(s[f] * (c - k) + s[c] * (k - f))


# ============================================================
# BEHAVIOR PROFILE
# ============================================================

class BehaviorProfile:
    """
    Perfil de comportamiento aprendido por canal.

    Alimentado desde lecturas crudas (ChannelReading). No usa
    eventos ni patrones: aprende de la señal.

    Uso:

        profile = BehaviorProfile()
        profile.warm_start(file_log)     # opcional, sobrevive reinicios
        profile.ingest_many(readings)    # en cada ciclo del Reporter
        profile.fit()                    # cada N minutos

        bp = profile.get_binary("Estado Compresor")
        if bp and not profile.is_learning():
            ...  # comparar lectura actual contra bp

    En estado 'learning' los perfiles existen, pero el consumidor
    NO debería usarlos para decidir si algo es anómalo.
    """

    DEFAULT_LEARNING_HOURS = 2.0
    DEFAULT_MAX_READINGS = 100_000
    # Magnitudes continuas que se perfilan como una temperatura (media/σ por
    # hora). El contador kWh NO va aquí a propósito.
    CONTINUOUS_UNITS = {"V", "A", "kW", "kVA", "kVAr"}

    def __init__(
        self,
        config: dict | None = None,
        analysis: AnalysisEngine | None = None,
    ) -> None:
        config = config or {}
        self.config = config
        self.analysis = analysis or AnalysisEngine(config)

        self.learning_hours = float(
            config.get("learning_hours", self.DEFAULT_LEARNING_HOURS)
        )
        self.flat_signal_p95_cap = int(
            config.get("flat_signal_p95_cap", 40)
        )
        self.max_readings_per_channel = int(
            config.get(
                "max_readings_per_channel",
                self.DEFAULT_MAX_READINGS,
            )
        )

        self._readings: dict[str, list[ChannelReading]] = defaultdict(list)
        self._first_ts: datetime | None = None
        self._last_ts: datetime | None = None
        self.local_tz = local_tz(config.get("tz_name", "America/Bogota"))
        self._binary: dict[str, BinaryProfile] = {}
        self._temperature: dict[str, TemperatureProfile] = {}
        self._recovery: RecoveryProfile = RecoveryProfile(
            by_pattern={}, n_events=0,
        )
        self._baselines: dict[str, Baseline] = {}
        self._last_ingested_ts: dict[str, datetime] = {}
        self._flat_streak_p95: dict[str, int] = {}
        self._fitted_at: datetime | None = None
        self._frozen_classifications: dict[str, str] = {}

    # ---------------------------------------------------------
    # INGESTA
    # ---------------------------------------------------------

    def ingest(self, reading: ChannelReading) -> None:
        self.ingest_many((reading,))

    def ingest_many(
        self,
        readings: Iterable[ChannelReading],
    ) -> None:
        for r in readings:
            if r.value is None or r.quality != DataQuality.VALID:
                continue

            # Dedup por canal + timestamp. Sin esto, reingestar
            # la ventana de 30 min cada 30 s duplica ~50×.
            last = self._last_ingested_ts.get(r.channel)
            if last is not None and r.event_time <= last:
                continue

            self._readings[r.channel].append(r)
            self._last_ingested_ts[r.channel] = r.event_time

            if self._first_ts is None or r.event_time < self._first_ts:
                self._first_ts = r.event_time
            if self._last_ts is None or r.event_time > self._last_ts:
                self._last_ts = r.event_time

        # Poda por stride si un canal excede el límite.
        for channel, readings in self._readings.items():
            if len(readings) > self.max_readings_per_channel:
                step = max(
                    1,
                    len(readings) // self.max_readings_per_channel,
                )
                self._readings[channel] = readings[::step]

    # ---------------------------------------------------------
    # WARM START
    # ---------------------------------------------------------

    def warm_start(self, file_log: FileLog) -> int:
        """
        Carga observations.jsonl y las ingiere.
        Devuelve el número de lecturas cargadas.
        """
        self._frozen_classifications.clear()
        records = file_log.read_observations()
        n = 0
        for rec in records:
            try:
                reading = ChannelReading(
                    device_id=int(rec["device_id"]),
                    channel=str(rec["channel"]),
                    value=(
                        None
                        if rec.get("value") is None
                        else float(rec["value"])
                    ),
                    unit=str(rec.get("unit", "")),
                    quality=DataQuality(rec["quality"]),
                    event_time=datetime.fromisoformat(
                        rec["event_time"]
                    ),
                    raw=str(rec.get("raw", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self.ingest(reading)
            n += 1
        return n

    # ---------------------------------------------------------
    # ESTADO
    # ---------------------------------------------------------

    def is_learning(self) -> bool:
        """
        True mientras el span temporal de las lecturas no alcance
        learning_hours. En estado learning, el consumidor no debe
        usar el perfil para decidir normalidad.
        """
        return self.learning_hours_elapsed() < self.learning_hours

    def learning_hours_elapsed(self) -> float:
        if self._first_ts is None or self._last_ts is None:
            return 0.0
        return (
            self._last_ts - self._first_ts
        ).total_seconds() / 3600.0

    def n_readings(self) -> int:
        return sum(len(v) for v in self._readings.values())

    def channels(self) -> list[str]:
        return sorted(self._readings.keys())

    # ---------------------------------------------------------
    # FIT
    # ---------------------------------------------------------

    def fit(self) -> None:
        """
        Recalcula todos los perfiles desde las lecturas cargadas.
        Coste O(n) sobre el total de lecturas.
        """
        self._binary.clear()
        self._temperature.clear()
        self._baselines.clear()

        for channel, readings in self._readings.items():
            if not readings:
                continue

            # Baseline global vía AnalysisEngine (pieza reutilizada).
            hours = max(1.0, self.learning_hours_elapsed() or 1.0)
            try:
                self._baselines[channel] = self.analysis.baseline(
                    readings,
                    hours=hours,
                )
            except ValueError:
                pass

            kind = self._classify(channel, readings)
            if kind == "binary":
                self._binary[channel] = self._fit_binary(channel, readings)
            elif kind == "temperature":
                self._temperature[channel] = self._fit_temperature(
                    channel, readings,
                )

            if kind != "binary":
                p95 = self._compute_flat_streak_p95(readings)
                if p95 is not None:
                    self._flat_streak_p95[channel] = p95
                

        self._fitted_at = self._last_ts
    def _compute_flat_streak_p95(
        self,
        readings: list[ChannelReading],
    ) -> int | None:
        """
        p95 de la duración (en muestras) de rachas de valor idéntico.

        Excluye la primera y última racha (pueden estar truncadas
        por los bordes del histórico) y descarta las rachas que
        superan el cap, porque esas corresponden a congelamientos
        previos, no a comportamiento normal.
        """
        if len(readings) < 20:
            return None

        sorted_r = sorted(readings, key=lambda r: r.event_time)

        streaks: list[int] = []
        current_len = 1
        current_val = float(sorted_r[0].value)
        for r in sorted_r[1:]:
            v = float(r.value)
            if abs(v - current_val) < 1e-9:
                current_len += 1
            else:
                streaks.append(current_len)
                current_val = v
                current_len = 1
        streaks.append(current_len)

        # Bordes truncados.
        inner = streaks[1:-1] if len(streaks) >= 3 else streaks

        # Rachas anómalamente largas = congelamientos previos.
        cap = self.flat_signal_p95_cap
        inner = [s for s in inner if s <= cap]
        if not inner:
            return None

        inner.sort()
        idx = min(int(len(inner) * 0.95), len(inner) - 1)
        return int(inner[idx])
    def _classify(
        self,
        channel: str,
        readings: list[ChannelReading],
    ) -> str:
        # 1. Congelado permanente tras la primera decisión.
        cached = self._frozen_classifications.get(channel)
        if cached is not None:
            return cached

        # 2. Decidir con una ventana temprana, no con toda la
        #    historia. Las primeras ~30 muestras ya contienen un
        #    ciclo ON/OFF completo del compresor.
        sample = readings[:30]
        units = {r.unit for r in sample if r.unit}

        if units == {"estado"}:
            result = "binary"
        elif "°C" in units or units & self.CONTINUOUS_UNITS:
            result = "temperature"
        else:
            values = [
                round(float(r.value), 3)
                for r in sample
                if r.value is not None
            ]
            if values and len(set(values)) <= 3:
                result = "binary"
            else:
                result = "other"

        # 3. Congelar de inmediato, sin esperar al fin del
        #    aprendizaje. La clasificación no depende de si el
        #    perfil ya aprendió; solo decide qué canales reciben
        #    el detector de flat_signal.
        self._frozen_classifications[channel] = result
        return result
    # ---------------------------------------------------------
    # PERFIL BINARIO
    # ---------------------------------------------------------

    def _fit_binary(
        self,
        channel: str,
        readings: list[ChannelReading],
    ) -> BinaryProfile:
        sorted_r = sorted(
            readings, key=lambda r: r.event_time,
        )

        bits = [1 if float(r.value) > 0.5 else 0 for r in sorted_r]
        times = [r.event_time for r in sorted_r]

        on_samples = sum(bits)
        fraction_on = on_samples / len(bits) if bits else 0.0

        # Construir rachas contiguas.
        streaks: list[tuple[int, datetime, datetime]] = []
        for b, t in zip(bits, times):
            if streaks and streaks[-1][0] == b:
                streaks[-1] = (b, streaks[-1][1], t)
            else:
                streaks.append((b, t, t))

        # Descartar primera y última racha: pueden estar truncadas
        # por el inicio/fin del histórico.
        inner = (
            streaks[1:-1]
            if len(streaks) >= 3
            else streaks
        )

        on_streaks = [s for s in inner if s[0] == 1]
        off_streaks = [s for s in inner if s[0] == 0]

        def durations(ss):
            return [
                (last - first).total_seconds() / 60.0
                for _, first, last in ss
            ]

        on_durations = durations(on_streaks)
        off_durations = durations(off_streaks)

        # Periodo ON -> siguiente ON con un OFF intermedio.
        periods: list[float] = []
        for i in range(len(inner) - 2):
            if (
                inner[i][0] == 1
                and inner[i + 1][0] == 0
                and inner[i + 2][0] == 1
            ):
                periods.append(
                    (inner[i + 2][1] - inner[i][1]).total_seconds()
                    / 60.0
                )

        return BinaryProfile(
            channel=channel,
            fraction_on=round(fraction_on, 6),
            on_median_min=_safe_median(on_durations),
            on_p95_min=_safe_percentile(on_durations, 0.95),
            off_median_min=_safe_median(off_durations),
            off_p95_min=_safe_percentile(off_durations, 0.95),
            period_median_min=_safe_median(periods),
            period_p95_min=_safe_percentile(periods, 0.95),
            n_samples=len(bits),
            n_on_streaks=len(on_streaks),
            n_off_streaks=len(off_streaks),
        )

    # ---------------------------------------------------------
    # PERFIL DE TEMPERATURA
    # ---------------------------------------------------------

    def _fit_temperature(
        self,
        channel: str,
        readings: list[ChannelReading],
    ) -> TemperatureProfile:
        sorted_r = sorted(
            readings, key=lambda r: r.event_time,
        )

        values = [float(r.value) for r in sorted_r]
        overall_mean = mean(values)
        overall_std = (
            stdev(values) if len(values) >= 2 else 0.0
        )

        by_hour_raw: dict[int, list[float]] = defaultdict(list)
        for r in sorted_r:
            dt = r.event_time
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(local_tz("America/Bogota"))
            by_hour_raw[local.hour].append(float(r.value))

        by_hour: dict[int, tuple[float, float]] = {}
        for hour, vals in sorted(by_hour_raw.items()):
            by_hour[hour] = (
                mean(vals),
                stdev(vals) if len(vals) >= 2 else 0.0,
            )

        return TemperatureProfile(
            channel=channel,
            overall_mean=overall_mean,
            overall_std=overall_std,
            by_hour=by_hour,
            n_samples=len(values),
        )

    # ---------------------------------------------------------
    # RECUPERACIÓN (desde Memory)
    # ---------------------------------------------------------

    def update_recovery(
        self,
        memory: HistoricalMemory,
    ) -> None:
        """
        Calcula el tiempo típico de recuperación por patrón,
        usando solo eventos con volvio_a_base=True.
        """
        events = memory.get_events()

        by_pattern: dict[str, list[float]] = defaultdict(list)
        for event in events:
            if not event.volvio_a_base:
                continue
            by_pattern[event.patron].append(event.duracion_min)

        self._recovery = RecoveryProfile(
            by_pattern={
                p: mean(vals) for p, vals in by_pattern.items()
            },
            n_events=sum(len(v) for v in by_pattern.values()),
        )

    # ---------------------------------------------------------
    # CONSULTAS
    # ---------------------------------------------------------
    def is_binary(self, channel: str) -> bool:
        return channel in self._binary

    def get_flat_streak_p95(self, channel: str) -> int | None:
        return self._flat_streak_p95.get(channel)
    def get_binary(
        self, channel: str,
    ) -> BinaryProfile | None:
        return self._binary.get(channel)

    def get_temperature(
        self, channel: str,
    ) -> TemperatureProfile | None:
        return self._temperature.get(channel)

    def get_baseline(
        self, channel: str,
    ) -> Baseline | None:
        return self._baselines.get(channel)

    def get_recovery(self) -> RecoveryProfile:
        return self._recovery

    # ---------------------------------------------------------
    # SNAPSHOT
    # ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "learning": self.is_learning(),
            "learning_hours_elapsed": round(
                self.learning_hours_elapsed(), 2,
            ),
            "learning_hours_required": self.learning_hours,
            "n_readings": self.n_readings(),
            "channels": self.channels(),
            "binary": {
                ch: {
                    "fraction_on": p.fraction_on,
                    "on_median_min": p.on_median_min,
                    "on_p95_min": p.on_p95_min,
                    "off_median_min": p.off_median_min,
                    "off_p95_min": p.off_p95_min,
                    "period_median_min": p.period_median_min,
                    "period_p95_min": p.period_p95_min,
                    "n_samples": p.n_samples,
                }
                for ch, p in self._binary.items()
            },
            "temperature": {
                ch: {
                    "overall_mean": p.overall_mean,
                    "overall_std": p.overall_std,
                    "by_hour": dict(p.by_hour),
                    "n_samples": p.n_samples,
                }
                for ch, p in self._temperature.items()
            },
            "recovery": {
                "by_pattern": dict(self._recovery.by_pattern),
                "n_events": self._recovery.n_events,
            },
            "fitted_at": (
                self._fitted_at.isoformat()
                if self._fitted_at else None
            ),
            "flat_streak_p95": dict(self._flat_streak_p95),
            "classification_cache": dict(self._frozen_classifications),
        }


