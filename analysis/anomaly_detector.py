from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Literal
from bisect import bisect_left
from analysis.baseline import BehaviorProfile
from analysis.engine import AnalysisEngine
from core.contracts import ChannelReading, DataQuality
AnomalyKind = Literal[
    "out_of_profile", "flat_signal", "missing", "persistent_drift",
    "state_overrun", "cross_inconsistency",
]


@dataclass(frozen=True)
class AnomalyState:
    channel: str
    kind: AnomalyKind
    severity: float          # 0..1
    since: datetime
    evidence: dict
class _Level:
    """Media móvil acotada (aprende el nivel típico de una variable)."""

    __slots__ = ("n", "mean")

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0

    def add(self, x: float, cap: int = 400) -> None:
        self.n += 1
        self.mean += (x - self.mean) / min(self.n, cap)

class AnomalyDetector:
    """
    Compara las lecturas recientes contra BehaviorProfile y
    devuelve una lista de AnomalyState.

    Detecta cuatro tipos:

        flat_signal       → N lecturas idénticas consecutivas
        missing           → N lecturas con quality ≠ VALID
        out_of_profile    → lectura fuera de k·σ_hora
        persistent_drift  → pendiente monótona fuera de rango

    Reglas:

        - flat_signal y missing son ESTRUCTURALES: se emiten
          aunque el perfil esté en estado 'learning'.
        - out_of_profile y persistent_drift son ESTADÍSTICAS:
          solo se emiten cuando el perfil ha aprendido.
        - El detector es stateless. La duración del estado la
          acumula el consumidor (Reporter).

    Config:
        flat_signal_min_samples        (int,   default 20)
        missing_min_consecutive_bad    (int,   default 3)
        out_of_profile_k_sigma         (float, default 3.0)
        persistent_drift_min_minutes   (float, default 10.0)
        persistent_drift_min_sigma     (float, default 2.0)
    """

    def __init__(
        self,
        engine: AnalysisEngine | None = None,
        config: dict | None = None,
    ) -> None:
        config = config or {}
        self.engine = engine or AnalysisEngine(config)

        self.flat_signal_min_samples = int(
            config.get("flat_signal_min_samples", 20)
        )
        self.missing_min_consecutive_bad = int(
            config.get("missing_min_consecutive_bad", 3)
        )
        self.out_of_profile_k_sigma = float(
            config.get("out_of_profile_k_sigma", 3.0)
        )
        self.persistent_drift_min_sigma = float(
            config.get("persistent_drift_min_sigma", 5.0)
        )
        self.persistent_drift_min_total = float(
            config.get("persistent_drift_min_total", 1.0)
        )
        self.persistent_drift_min_minutes = float(
            config.get("persistent_drift_min_minutes", 3.0)
        )
        self.flat_signal_factor = float(
            config.get("flat_signal_factor", 2.0)
        )
        # Canales constantes POR DISEÑO (setpoint, desvío, frecuencia...):
        # no deben generar "señal plana".
        self.flat_signal_exempt = frozenset(
            config.get("flat_signal_exempt_channels", ())
        )
        # Reglas entre instrumentos: cuando `driver` (0/1) está en 1,
        # `driven` debe subir sobre su nivel con driver=0.
        self.consistency_rules = list(config.get("consistency_rules", ()))
        self._levels: dict[str, dict] = {}
        # Canales sobre los que aplica out_of_profile / persistent_drift.
        # Vacío = todos.
        self.profiled_channels = frozenset(config.get("profiled_channels", ()))
        # Pisos contra z-scores espurios: tras poco aprendizaje la σ por hora
        # puede ser minúscula (p. ej. tensión: σ≈0.8 V sobre 230 V) y cualquier
        # deriva normal supera k·σ. Una desviación solo cuenta si además es
        # relevante en magnitud relativa a la media del canal.
        self.oop_min_rel_sigma = float(
            config.get("out_of_profile_min_rel_sigma", 0.01))
        self.oop_min_rel_dev = float(
            config.get("out_of_profile_min_rel_dev", 0.03))
        # Excursiones ESPERADAS: mientras `when_channel` está en 1 (más
        # `grace_minutes`), no se evalúan los canales `suppress`.
        self.contextual_masks = list(config.get("contextual_masks", ()))
        # Duración máxima (min) de un estado en 1; pasada, anomalía.
        self.state_run_limits_min = dict(config.get("state_run_limits_min", {}))

    # ========================================================
    # API
    # ========================================================

    def detect(
        self,
        series_by_channel: dict[str, list[ChannelReading]],
        profile: BehaviorProfile | None = None,
    ) -> list[AnomalyState]:

        states: list[AnomalyState] = []
        masked = self._masked_channels(series_by_channel)

        for channel, readings in series_by_channel.items():
            if not readings:
                continue

            overrun = self._detect_state_overrun(channel, readings)
            if overrun is not None:
                states.append(overrun)

            if channel in masked:
                # Solo se omite lo estadístico. La falta de datos y la señal
                # plana SÍ cuentan: durante un deshielo la temperatura debe
                # moverse, así que una lectura quieta es un sensor congelado.
                missing = self._detect_missing(channel, readings)
                if missing is not None:
                    states.append(missing)
                if (
                    channel not in self.flat_signal_exempt
                    and not self._should_skip_flat(channel, readings, profile)
                ):
                    flat = self._detect_flat_signal(channel, readings, profile)
                    if flat is not None:
                        states.append(flat)
                continue
            missing = self._detect_missing(channel, readings)
            if missing is not None:
                states.append(missing)

            # flat_signal solo aplica a canales no binarios.
            if (
                channel not in self.flat_signal_exempt
                and not self._should_skip_flat(channel, readings, profile)
            ):
                flat = self._detect_flat_signal(channel, readings, profile)
                if flat is not None:
                    states.append(flat)

            if profile is None or profile.is_learning():
                continue
            if self.profiled_channels and channel not in self.profiled_channels:
                continue

            oop = self._detect_out_of_profile(channel, readings, profile)
            if oop is not None:
                states.append(oop)

            drift = self._detect_persistent_drift(channel, readings, profile)
            if drift is not None:
                states.append(drift)
        for rule in self.consistency_rules:
            st = self._detect_consistency(rule, series_by_channel)
            if st is not None:
                states.append(st)

        return states
    # ========================================================
    # CONSISTENCIA ENTRE INSTRUMENTOS
    # ========================================================

    def consistency_levels(self) -> dict[str, dict]:
        """Niveles aprendidos por regla (para el diagnóstico)."""
        return {
            name: {"on": lv["on"].mean, "off": lv["off"].mean,
                   "n_on": lv["on"].n, "n_off": lv["off"].n}
            for name, lv in self._levels.items()
        }

    @staticmethod
    def _nearest(ts: list, vals: list, t, tol_s: float):
        if not ts:
            return None
        i = bisect_left(ts, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ts):
                d = abs((ts[j] - t).total_seconds())
                if d <= tol_s and (best is None or d < best[0]):
                    best = (d, vals[j])
        return None if best is None else best[1]

    def _detect_consistency(self, rule: dict, series_by_channel):
        name = rule["name"]
        drv = series_by_channel.get(rule["driver"]) or []
        out = series_by_channel.get(rule["driven"]) or []
        good = (DataQuality.VALID, DataQuality.FROZEN)
        drv = [r for r in drv if r.value is not None and r.quality in good]
        out = [r for r in out if r.value is not None and r.quality in good]
        if not drv or not out:
            return None

        tol = float(rule.get("tolerance_seconds", 90.0))
        o_t = [r.event_time for r in out]
        o_v = [float(r.value) for r in out]
        ign = []
        for ch in rule.get("ignore_when", ()):
            rs = [r for r in (series_by_channel.get(ch) or [])
                  if r.value is not None and r.quality in good]
            ign.append(([r.event_time for r in rs],
                        [float(r.value) for r in rs]))

        pairs = []                       # (t, bit, valor)
        for r in sorted(drv, key=lambda x: x.event_time):
            v = self._nearest(o_t, o_v, r.event_time, tol)
            if v is None:
                continue
            if any((self._nearest(t_, v_, r.event_time, tol) or 0) > 0.5
                   for t_, v_ in ign):
                continue                 # contexto ya explicado (deshielo)
            pairs.append((r.event_time, 1 if float(r.value) > 0.5 else 0, v))
        if not pairs:
            return None

        lv = self._levels.setdefault(
            name, {"on": _Level(), "off": _Level(), "last_t": None})
        dead = float(rule.get("dead_ratio", 0.4))
        partial = float(rule.get("partial_ratio", 0.8))
        learn_min = int(rule.get("learn_min", 15))
        min_rise = float(rule.get("min_rise", 3.0))
        k = int(rule.get("min_samples", 3))

        def ratio(v: float):
            rise = lv["on"].mean - lv["off"].mean
            if lv["on"].n < learn_min or lv["off"].n < learn_min or rise < min_rise:
                return None
            return (v - lv["off"].mean) / rise

        # Aprendizaje: solo lecturas nuevas; ON solo si no es sospechosa.
        for i, (t, bit, v) in enumerate(pairs):
            if lv["last_t"] is not None and t <= lv["last_t"]:
                continue
            if bit == 0:
                lv["off"].add(v)
            else:
                prev_on = i > 0 and pairs[i - 1][1] == 1
                rt = ratio(v)
                if prev_on and (rt is None or rt >= partial):
                    lv["on"].add(v)
                elif not prev_on and rt is None:
                    lv["on"].add(v)
        lv["last_t"] = pairs[-1][0]

        # Evaluación: racha final con driver=1 (sin la 1.ª muestra tras el arranque).
        run = []
        for i in range(len(pairs) - 1, 0, -1):
            t, bit, v = pairs[i]
            if bit != 1 or pairs[i - 1][1] != 1:
                break
            rt = ratio(v)
            if rt is None or rt >= partial:
                break
            run.append((t, v, rt))
        if len(run) < k:
            return None
        run.reverse()
        mean_rt = mean(x[2] for x in run)
        mean_v = mean(x[1] for x in run)
        pattern = "sin_consumo" if mean_rt < dead else "consumo_bajo"
        sev = min(1.0, (0.5 if pattern == "sin_consumo" else 0.3)
                  + len(run) / (4.0 * k))
        return AnomalyState(
            channel=rule["driven"], kind="cross_inconsistency", severity=sev,
            since=run[0][0],
            evidence={
                "rule": name, "driver": rule["driver"], "pattern": pattern,
                "observed": round(mean_v, 2),
                "expected_on": round(lv["on"].mean, 2),
                "expected_off": round(lv["off"].mean, 2),
                "ratio": round(mean_rt, 2), "samples": len(run),
            },
        )
    # ========================================================
    # MISSING
    # ========================================================
    def _should_skip_flat(
        self,
        channel: str,
        readings: list[ChannelReading],
        profile: BehaviorProfile | None,
    ) -> bool:
        # 1. Si el perfil ya aprendió, manda.
        if profile is not None and not profile.is_learning():
            return profile.is_binary(channel)

        # 2. Fallback local.
        units = {r.unit for r in readings if r.unit}
        if units == {"estado"}:
            return True
        if "°C" in units:
            return False

        values = [
            round(float(r.value), 1)
            for r in readings
            if r.value is not None
        ]
        if not values:
            return False

        unique = sorted(set(values))
        if len(unique) <= 3:
            return True

        # 3. Clusters: dos grupos separados por un gap grande.
        if len(unique) >= 4:
            gaps = [
                unique[i + 1] - unique[i]
                for i in range(len(unique) - 1)
            ]
            max_gap = max(gaps)
            median_gap = sorted(gaps)[len(gaps) // 2]
            if max_gap >= 5 * max(median_gap, 0.01):
                return True

        return False
    def _detect_missing(
        self,
        channel: str,
        readings: list[ChannelReading],
    ) -> AnomalyState | None:
        # Solo MISSING/OFFLINE/ERR son fallos de comunicación.
        # FROZEN es un valor válido que el QualityGuard marcó como
        # sospechosamente estable. No es "sin datos".
        bad = {
            DataQuality.MISSING,
            DataQuality.OFFLINE,
            DataQuality.ERR,
        }
        trailing = []
        for r in reversed(readings):
            if r.quality in bad:
                trailing.append(r)
            else:
                break

        if len(trailing) < self.missing_min_consecutive_bad:
            return None
        since = trailing[0].event_time
        severity = min(
            1.0,
            len(trailing) / (self.missing_min_consecutive_bad * 3),
        )
        qualities = sorted({r.quality.value for r in trailing})

        return AnomalyState(
            channel=channel,
            kind="missing",
            severity=severity,
            since=since,
            evidence={
                "consecutive_bad": len(trailing),
                "qualities": qualities,
            },
        )

    # ========================================================
    # FLAT SIGNAL
    # ========================================================    
    def _detect_flat_signal(self, channel, readings, profile=None):
        valid = [
            r for r in readings
            if r.quality in (DataQuality.VALID, DataQuality.FROZEN)
            and r.value is not None
        ]

        threshold = self.flat_signal_min_samples
        if profile is not None:
            p95 = profile.get_flat_streak_p95(channel)
            if p95 is not None and p95 > 0:
                adaptive = int(p95 * self.flat_signal_factor)
                threshold = max(threshold, adaptive)

        if len(valid) < threshold:
            return None

        # Buscar la racha REAL de valores idénticos que termina
        # en la última lectura. `since` es el inicio de esa racha,
        # no la ventana deslizante. Sin esto, `since` avanza cada
        # tick y la política nunca consolida el incidente.
        last_val = float(valid[-1].value)
        streak_start = len(valid) - 1
        for i in range(len(valid) - 2, -1, -1):
            if abs(float(valid[i].value) - last_val) > 1e-9:
                break
            streak_start = i

        streak_len = len(valid) - streak_start
        if streak_len < threshold:
            return None

        since = valid[streak_start].event_time
        severity = min(1.0, streak_len / max(threshold * 2, 1))

        return AnomalyState(
            channel=channel,
            kind="flat_signal",
            severity=severity,
            since=since,
            evidence={
                "frozen_value": last_val,
                "samples": streak_len,
                "threshold": threshold,
                "included_frozen": True,
            },
        )
    # ========================================================
    # OUT OF PROFILE
    # ========================================================

    def _detect_out_of_profile(
        self,
        channel: str,
        readings: list[ChannelReading],
        profile: BehaviorProfile,
    ) -> AnomalyState | None:

        tp = profile.get_temperature(channel)
        if tp is None or tp.n_samples < 30:
            return None

        valid = [
            r for r in readings
            if r.quality == DataQuality.VALID and r.value is not None
        ]
        if not valid:
            return None

        latest = valid[-1]
        value = float(latest.value)

        # Media/σ por hora. Si la hora no tiene datos propios,
        # cae al agregado global.
        hour = latest.event_time.hour
        if hour in tp.by_hour and tp.by_hour[hour][1] > 0:
            mu, sigma = tp.by_hour[hour]
        else:
            mu, sigma = tp.overall_mean, tp.overall_std

        if sigma <= 1e-9:
            return None

        scale = max(abs(mu), 1e-9)
        sigma = max(sigma, self.oop_min_rel_sigma * scale)
        dev = abs(value - mu)
        if dev < self.oop_min_rel_dev * scale:
            return None

        z = dev / sigma
        if z < self.out_of_profile_k_sigma:
            return None

        severity = min(1.0, z / (2 * self.out_of_profile_k_sigma))
        return AnomalyState(
            channel=channel,
            kind="out_of_profile",
            severity=severity,
            since=latest.event_time,
            evidence={
                "value": value,
                "mu": mu,
                "sigma": sigma,
                "z": z,
                "hour": hour,
            },
        )

    # ========================================================
    # PERSISTENT DRIFT
    # ========================================================

    def _detect_persistent_drift(
        self,
        channel: str,
        readings: list[ChannelReading],
        profile: BehaviorProfile,
    ) -> AnomalyState | None:

        tp = profile.get_temperature(channel)
        if tp is None or tp.overall_std <= 1e-9:
            return None

        valid = [
            r for r in readings
            if r.quality == DataQuality.VALID and r.value is not None
        ]
        if len(valid) < 30:
            return None

        latest = valid[-1].event_time
        cutoff = latest - timedelta(minutes=self.persistent_drift_min_minutes)
        window = [r for r in valid if r.event_time >= cutoff]
        if len(window) < 10:
            return None

        slope = self.engine.slope(window)  # unidades/hora
        if slope is None:
            return None

        span_hours = (
            window[-1].event_time - window[0].event_time
        ).total_seconds() / 3600.0
        if span_hours <= 0:
            return None

        total_change = abs(slope) * span_hours
        threshold = self.persistent_drift_min_sigma * tp.overall_std

        # Cambio absoluto mínimo: evita disparos con σ pequeñas.
        if total_change < self.persistent_drift_min_total:
            return None

        if total_change < threshold:
            return None

        severity = min(1.0, total_change / (threshold * 3))
        return AnomalyState(
            channel=channel,
            kind="persistent_drift",
            severity=severity,
            since=window[0].event_time,
            evidence={
                "slope_per_hour": slope,
                "total_change": total_change,
                "window_minutes": self.persistent_drift_min_minutes,
                "threshold": threshold,
            },
        )

    @staticmethod
    def _looks_binary(readings: list[ChannelReading]) -> bool:
        units = {r.unit for r in readings if r.unit}
        if units == {"estado"}:
            return True
        values = [float(r.value) for r in readings if r.value is not None]
        if not values:
            return False
        return all(
            abs(v) < 1e-9 or abs(v - 1.0) < 1e-9
            for v in values
        )


    @staticmethod
    def _bit_run(readings):
        """(valor_actual, minutos en ese valor) para un canal de estado."""
        valid = [r for r in readings if r.value is not None
                 and r.quality in (DataQuality.VALID, DataQuality.FROZEN)]
        if not valid:
            return None, 0.0
        cur = 1 if float(valid[-1].value) > 0.5 else 0
        start = valid[-1].event_time
        for r in reversed(valid[:-1]):
            if (1 if float(r.value) > 0.5 else 0) != cur:
                break
            start = r.event_time
        return cur, (valid[-1].event_time - start).total_seconds() / 60.0

    def _masked_channels(self, series_by_channel) -> set[str]:
        out: set[str] = set()
        for rule in self.contextual_masks:
            rs = series_by_channel.get(rule["when_channel"])
            if not rs:
                continue
            valid = [r for r in rs if r.value is not None
                     and r.quality in (DataQuality.VALID, DataQuality.FROZEN)]
            if not valid:
                continue
            latest = valid[-1].event_time
            grace = timedelta(minutes=float(rule.get("grace_minutes", 0)))
            active = any(float(r.value) > 0.5 and latest - r.event_time <= grace
                         for r in valid)
            # Si el estado excede su límite deja de ser "esperado".
            limit = self.state_run_limits_min.get(rule["when_channel"])
            if active and limit is not None:
                cur, run = self._bit_run(rs)
                if cur == 1 and run > limit:
                    active = False
            if active:
                out.update(rule.get("suppress", ()))
        return out

    def _detect_state_overrun(self, channel, readings):
        limit = self.state_run_limits_min.get(channel)
        if limit is None:
            return None
        cur, run = self._bit_run(readings)
        if cur != 1 or run <= limit:
            return None
        valid = [r for r in readings if r.value is not None]
        return AnomalyState(
            channel=channel, kind="state_overrun",
            severity=min(1.0, run / (limit * 2.0)),
            since=valid[-1].event_time - timedelta(minutes=run),
            evidence={"minutes_on": round(run, 1), "limit_min": limit},
        )