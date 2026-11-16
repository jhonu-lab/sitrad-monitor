from __future__ import annotations
import json
import math
import random
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.contracts import ChannelReading, DataQuality, Snapshot
from core.channel_catalog import ALL_SPECS, CONTROLLER_SPECS, channels_config  # noqa: F401
import logging
logger = logging.getLogger(__name__)

CTRL = "controlador"
MP = "multipower"

CH_TEMP = "Temp Cámara"
CH_SP = "Setpoint"
CH_DESVIO = "Desvío vs Setpoint"
CH_REFR = "Estado Refrigeración"
CH_DEFR = "Estado Deshielo"
CH_PUERTA = "Puerta Abierta"
CH_HOT = "Salida HOT"

ALL_CHANNELS = [(sp.name, sp.unit) for sp in ALL_SPECS]
_CONTROLLER_NAMES = {sp.name for sp in CONTROLLER_SPECS}


DEFAULT_PARAMS = {
    # Controlador (parámetros programables del MT-512E)
    "setpoint": -20.0,
    "differential": 2.0,          # REFR ON en SP+diff, OFF en SP
    "min_off_min": 3.0,           # anti-ciclo corto
    "defrost_every_min": 270.0,
    "defrost_dur_min": 30.0,

    # Térmica de la cámara
    "ambient_mean": 28.0,
    "ambient_amp": 4.0,
    "k_envelope": 0.0035,         # °C/min por °C de diferencia
    "cooling_rate": 0.30,         # °C/min con compresor sano
    "defrost_heat": 0.02,

    # Eléctrica. HIPÓTESIS DE SIMULACIÓN: M1 alimenta la cava y M2 es
    # otro circuito independiente. Confirmar con el ingeniero.
    "m1_base_kw": 24.0,
    "m1_comp_kw": 11.0,
    "m1_heater_kw": 6.0,
    "m2_base_kw": 28.9,
    "v_phase": 230.0,
    "freq": 60.0,

    # Producto caliente (modelo de dos nodos: aire de la cámara <-> producto)
    "k_prod_air": 0.008,          # °C/min que el producto le cede al aire por °C de diferencia
    "k_air_prod": 0.006,          # fracción/min con que el producto se enfría hacia el aire
    "prod_report_c": 4.0,         # el simulador anota cuándo el PRODUCTO llega a esta temperatura
}


@dataclass(frozen=True)
class Episode:
    name: str
    anomalo: bool
    weight: float
    min_minutes: float
    max_minutes: float
    description: str = ""
    # "diagnostico": lo evalúa el diagnóstico de fallas del bot.
    # "informe":     importa al informe de temperatura (la termocupla lo ve).
    scope: str = "diagnostico"


# Duraciones pensadas para cadencia de 120 s (8+ muestras por episodio).
EPISODE_CATALOG: dict[str, Episode] = {
    e.name: e
    for e in (
        Episode(
            "normal",
            False,
            0.50,
            60,
            180,
            "Operación normal: ciclos de compresor y deshielo programado.",
        ),
        Episode(
            "entrada_producto",
            False,
            0.08,
            15,
            25,
            "Puerta abierta unos minutos y producto tibio.",
        ),
        Episode(
            "carga_grande",
            False,
            0.06,
            30,
            60,
            "Mucha carga térmica: el compresor trabaja más, sin falla.",
        ),
        Episode(
            "puerta_prolongada",
            True,
            0.06,
            15,
            25,
            "Puerta abierta de forma prolongada.",
        ),
        Episode(
            "fallo_refrigeracion",
            True,
            0.06,
            40,
            70,
            "REFR activo, el compresor consume menos y la cámara sube.",
        ),
        Episode(
            "compresor_sin_arranque",
            True,
            0.06,
            30,
            60,
            "REFR pide frío pero M1 no muestra consumo de compresor.",
        ),
        Episode(
            "deshielo_extendido",
            True,
            0.04,
            45,
            65,
            "El deshielo no termina: resistencias encendidas de más.",
        ),
        Episode(
            "baja_tension",
            True,
            0.04,
            30,
            60,
            "Caída de tensión en M1: se ve solo en el Multipower.",
        ),
        Episode(
            "sensor_congelado",
            True,
            0.05,
            40,
            70,
            "Temp Cámara repite el mismo valor mientras la cámara varía.",
        ),
        Episode(
            "sin_comunicacion",
            True,
            0.05,
            20,
            40,
            "Un instrumento (controlador o Multipower) deja de responder.",
        ),
        # ---- Lo que más ocurre en una cava y ve la termocupla -------------
        Episode(
            "aperturas_frecuentes", False, 0.06, 40, 90,
            "Despacho: muchas aperturas cortas de puerta seguidas.", "informe",
        ),
        Episode(
            "ingreso_producto_caliente", False, 0.04, 150, 240,
            "Ingresa producto caliente (34-40 °C): el aire sube y el producto "
            "tarda horas en enfriarse.", "informe",
        ),
        Episode(
            "temperatura_ambiente_alta", False, 0.02, 120, 240,
            "Ola de calor: el compresor trabaja más para lo mismo.", "informe",
        ),
        Episode(
            "cambio_setpoint", False, 0.02, 90, 180,
            "El operario cambia el setpoint del controlador.", "informe",
        ),
        Episode(
            "evaporador_hielo", True, 0.03, 120, 240,
            "El evaporador se congela poco a poco: la capacidad cae y la "
            "temperatura deriva hacia arriba.", "informe",
        ),
        Episode(
            "perdida_refrigerante", True, 0.02, 120, 240,
            "Fuga lenta de refrigerante: menos capacidad y menos consumo.", "informe",
        ),
        Episode(
            "ventilador_evaporador", True, 0.02, 60, 150,
            "El ventilador del evaporador falla: poca circulación de aire, "
            "el compresor consume normal.", "informe",
        ),
        Episode(
            "puerta_mal_cerrada", True, 0.03, 90, 240,
            "Empaque o puerta mal sellados: entra calor sin parar.", "informe",
        ),
        Episode(
            "corte_energia", True, 0.02, 20, 50,
            "Se va la luz: ningún instrumento reporta y la cámara se calienta.", "informe",
        ),
        Episode(
            "sensor_ruido", True, 0.03, 40, 90,
            "La termocupla da picos aislados (falso contacto): la cámara "
            "no cambia, la lectura sí.", "informe",
        ),
    )
}


# Guiones reproducibles (minutos desde el inicio de la simulación). Entre
# episodios la cava opera normal. Sirven para demostrar siempre lo mismo.
SCENARIO_PRESETS: dict[str, list[dict]] = {
    "jornada_tipica": [
        {"at_min": 150, "episode": "aperturas_frecuentes", "duration_min": 60},
        {"at_min": 300, "episode": "ingreso_producto_caliente", "duration_min": 200},
        {"at_min": 700, "episode": "aperturas_frecuentes", "duration_min": 45},
        {"at_min": 900, "episode": "puerta_prolongada", "duration_min": 20},
    ],
    "producto_caliente": [
        {"at_min": 130, "episode": "ingreso_producto_caliente", "duration_min": 240},
    ],
    "falla_gradual": [
        {"at_min": 130, "episode": "evaporador_hielo", "duration_min": 240},
    ],
    "perdida_refrigerante": [
        {"at_min": 130, "episode": "perdida_refrigerante", "duration_min": 240},
    ],
    "ventilador_evaporador": [
        {"at_min": 130, "episode": "ventilador_evaporador", "duration_min": 120},
    ],
    "corte_energia": [
        {"at_min": 130, "episode": "corte_energia", "duration_min": 40},
    ],
    "puerta_mal_cerrada": [
        {"at_min": 130, "episode": "puerta_mal_cerrada", "duration_min": 180},
    ],
    "ola_de_calor": [
        {"at_min": 130, "episode": "temperatura_ambiente_alta", "duration_min": 240},
    ],
    "sensor_con_ruido": [
        {"at_min": 130, "episode": "sensor_ruido", "duration_min": 90},
    ],
    "cambio_setpoint": [
        {"at_min": 130, "episode": "cambio_setpoint", "duration_min": 150},
    ],
    "dia_con_incidentes": [
        {"at_min": 150, "episode": "aperturas_frecuentes", "duration_min": 50},
        {"at_min": 260, "episode": "ingreso_producto_caliente", "duration_min": 220},
        {"at_min": 620, "episode": "evaporador_hielo", "duration_min": 200},
        {"at_min": 900, "episode": "corte_energia", "duration_min": 35},
        {"at_min": 1100, "episode": "puerta_mal_cerrada", "duration_min": 120},
    ],
}


def scenario_names() -> list[str]:
    return ["random"] + sorted(SCENARIO_PRESETS)


class CavaRandomSource:
    """
    Un solo modelo físico alimenta ambos instrumentos:

        temperatura  --(histéresis)-->  REFR (controlador)
        REFR + compresor sano  ------>  kW en M1 (Multipower)

    Los episodios cambian la física (capacidad, puerta, deshielo,
    tensión) o la calidad de la lectura. Nunca escriben el síntoma.
    """

    SUBSTEP_S = 30.0

    def __init__(
        self,
        config: dict,
        seed: int = 42,
        min_minutes: float | None = None,
        max_minutes: float | None = None,
    ) -> None:
        self.device_id = int(config.get("device_id", 201))
        self.device_name = config.get("device_name", "Cava Colanta")

        self.p = {
            **DEFAULT_PARAMS,
            **{k: v for k, v in config.items() if k in DEFAULT_PARAMS},
        }

        self._rng = random.Random(seed)
        # Registro de la VERDAD (qué episodio ocurrió y cuándo): permite
        # medir aciertos del bot sin depender de la memoria de nadie.
        # Horas iniciales SIN fallas aleatorias: el bot aprende primero su
        # comportamiento normal y las fallas llegan cuando ya puede evaluarlas.
        self._normal_warmup_h = float(config.get("normal_warmup_hours", 0.0))
        self._first_select_at: datetime | None = None
        self._truth_path = config.get("truth_log")
        if self._truth_path:
            os.makedirs(os.path.dirname(self._truth_path) or ".", exist_ok=True)
            open(self._truth_path, "w", encoding="utf-8").close()
        self._force_min = min_minutes
        self._force_max = max_minutes

        self._current_episode: Episode | None = None
        self._current_episode_count = 0
        self._episode_ends_at: datetime | None = None
        self._episode_started_at: datetime | None = None
        self._forced_episode: str | None = None
        self._sticky_episode: str | None = None
        self._fx = self._neutral_fx()

        # Producto caliente (persiste más allá del episodio que lo trajo).
        self._prod: dict | None = None
        self._ep_start_min = 0.0
        self._ep_dur_min = 0.0

        # Guion reproducible (en lugar de episodios aleatorios).
        script = config.get("scenario_script")
        self._script = (sorted(script, key=lambda e: e["at_min"]) if script else None)
        self._script_idx = 0
        self._t0: datetime | None = None
        self._true_log: list | None = [] if config.get("record_true_series") else None

        self._last_poll: datetime | None = None
        self._T = self.p["setpoint"] + self.p["differential"] * 0.5
        self._refr = False
        self._last_off_min = -99.0
        self._sim_min = 0.0

        self._since_defrost = self._rng.uniform(
            0.0, self.p["defrost_every_min"]
        )
        self._defr = False
        self._defr_left = 0.0

        self._v_grid_dev = 0.0
        self._m2_drift = 0.0

        # Demanda activa filtrada: constante de tiempo de 15 minutos.
        self._demand = {"M1": None, "M2": None}

        self._e = {"M1": 147289.7, "M2": 122805.8}
        self._frozen_value: float | None = None
        self._comm_target: str | None = None
        self._last_m1: dict | None = None
        self._last_m2: dict | None = None

    # ---------------- API pública ----------------

    @classmethod
    def episodes(cls) -> tuple[Episode, ...]:
        return tuple(EPISODE_CATALOG.values())

    @property
    def current_episode(self) -> Episode | None:
        return self._current_episode

    def current_episode_is_anomalo(self) -> bool:
        return bool(
            self._current_episode and self._current_episode.anomalo
        )

    def set_scenario(self, name: str, sticky: bool = False) -> None:
        if name not in EPISODE_CATALOG:
            raise ValueError(
                f"Episodio desconocido: {name}. "
                f"Disponibles: {sorted(EPISODE_CATALOG)}"
            )

        self._forced_episode = name
        self._sticky_episode = name if sticky else None

    @property
    def true_series(self) -> list:
        """[(instante, temperatura REAL del aire)] si record_true_series=True."""
        return list(self._true_log or [])

    def poll(self, now: datetime) -> Snapshot:
        if self._t0 is None:
            self._t0 = now
        self._maybe_switch_episode(now)
        self._advance(now)
        if self._true_log is not None:
            self._true_log.append((now, self._T))

        return Snapshot(
            source_id="cava_sim",
            processing_time=now,
            readings=tuple(self._emit(now)),
        )

    # ---------------- Episodios ----------------

    @staticmethod
    def _neutral_fx() -> dict:
        return {
            "door_until_min": 0.0,
            "extra_gain": 0.0,
            "decay_tau": 0.0,
            "capacity": 1.0,
            "comp_power": 1.0,
            "comp_runs": True,
            "v_factor": 1.0,
            "f2_extra": 0.0,
            "ext_gain": 0.0,          # entrada de calor constante (empaque malo)
            "cap_ramp": None,         # (capacidad inicial, final, fracción del episodio que dura la caída)
            "pow_ramp": None,         # (consumo inicial, final, fracción) del compresor
            "power_cut": False,       # sin energía: nada reporta ni enfría
            "amb_add": 0.0,           # °C extra de temperatura ambiente
            "setpoint_add": 0.0,      # el operario cambió el setpoint
            "door_pulses": [],        # aperturas cortas [(inicio_min, fin_min), ...]
            "spike_prob": 0.0,        # picos aislados en la lectura de la termocupla
        }

    def _maybe_switch_episode(self, now: datetime) -> None:
        if self._script is not None:
            elapsed = (now - self._t0).total_seconds() / 60.0
            if (self._script_idx < len(self._script)
                    and elapsed >= self._script[self._script_idx]["at_min"]):
                it = self._script[self._script_idx]
                self._script_idx += 1
                self._select(now, it["episode"], duration=it.get("duration_min"))
            elif self._current_episode is None or now >= self._episode_ends_at:
                nxt = (self._script[self._script_idx]["at_min"] - elapsed
                       if self._script_idx < len(self._script) else 120.0)
                self._select(now, "normal", duration=max(1.0, nxt))
            return

        if self._forced_episode is not None:
            self._select(now, self._forced_episode)
            self._forced_episode = None

        elif self._sticky_episode is not None:
            if (
                self._current_episode is None
                or self._current_episode.name != self._sticky_episode
                or now >= self._episode_ends_at
            ):
                self._select(now, self._sticky_episode)

        elif (
            self._current_episode is None
            or now >= self._episode_ends_at
        ):
            first = self._current_episode is None
            self._select(now, "normal" if first else None)

    def _select(self, now: datetime, name: str | None,
                duration: float | None = None) -> None:
        if self._first_select_at is None:
            self._first_select_at = now
        if (name is None and self._script is None and self._normal_warmup_h > 0
                and (now - self._first_select_at).total_seconds()
                < self._normal_warmup_h * 3600.0):
            name = "normal"
        if name is None:
            names = list(EPISODE_CATALOG)
            name = self._rng.choices(
                names,
                [EPISODE_CATALOG[n].weight for n in names],
            )[0]

        ep = EPISODE_CATALOG[name]

        lo = (
            self._force_min
            if self._force_min is not None
            else ep.min_minutes
        )
        hi = (
            self._force_max
            if self._force_max is not None
            else ep.max_minutes
        )

        dur = float(duration) if duration is not None else self._rng.uniform(lo, hi)
        self._ep_start_min = self._sim_min
        self._ep_dur_min = dur

        self._current_episode = ep
        self._episode_started_at = now
        self._episode_ends_at = now + timedelta(minutes=dur)
        logger.info(
            "[SIM] Episodio '%s' (%s) %s -> %s UTC (%.0f min)",
            name, "ANOMALO" if ep.anomalo else "normal",
            now.strftime("%H:%M"), self._episode_ends_at.strftime("%H:%M"), dur,
        )
        first = self._current_episode_count == 0
        self._current_episode_count += 1
        if self._truth_path:
            with open(self._truth_path, "a", encoding="utf-8", newline="\n") as fh:
                json.dump({
                    "name": name, "anomalo": ep.anomalo, "scope": ep.scope, "first": first,
                    "start": now.isoformat(),
                    "end": self._episode_ends_at.isoformat(),
                }, fh, ensure_ascii=False)
                fh.write("\n")
        self._frozen_value = None
        self._comm_target = None
        self._fx = self._neutral_fx()

        fx, r = self._fx, self._rng

        if name == "entrada_producto":
            fx.update(
                door_until_min=self._sim_min + r.uniform(4, 6),
                extra_gain=0.20,
                decay_tau=8.0,
            )

        elif name == "carga_grande":
            fx.update(extra_gain=0.05)

        elif name == "puerta_prolongada":
            fx.update(
                door_until_min=self._sim_min + dur * 0.75,
                extra_gain=0.45,
            )

        elif name == "fallo_refrigeracion":
            fx.update(capacity=0.20, comp_power=0.55)

        elif name == "compresor_sin_arranque":
            fx.update(comp_runs=False)

        elif name == "deshielo_extendido":
            self._defr, self._defr_left = True, dur
            self._since_defrost = 0.0

        elif name == "baja_tension":
            fx.update(v_factor=0.88, f2_extra=0.03)

        elif name == "sin_comunicacion":
            self._comm_target = r.choice([CTRL, MP])

        elif name == "aperturas_frecuentes":
            t, pulses = self._sim_min + r.uniform(1, 4), []
            while t < self._sim_min + dur:
                d = r.uniform(0.7, 2.0)
                pulses.append((t, t + d))
                t += d + r.uniform(4, 12)
            fx.update(door_pulses=pulses, extra_gain=0.25)

        elif name == "ingreso_producto_caliente":
            fx.update(door_until_min=self._sim_min + r.uniform(6, 12), extra_gain=0.20)
            t0 = r.uniform(34.0, 40.0)
            self._prod = {"T": t0, "T0": t0, "marked": False, "since": now}

        elif name == "temperatura_ambiente_alta":
            fx.update(amb_add=12.0)

        elif name == "cambio_setpoint":
            fx.update(setpoint_add=r.choice([2.0, 3.0, -1.0]))

        elif name == "evaporador_hielo":
            fx.update(cap_ramp=(1.0, 0.10, 0.6))

        elif name == "perdida_refrigerante":
            fx.update(cap_ramp=(1.0, 0.08, 0.6), pow_ramp=(1.0, 0.50, 0.6))

        elif name == "ventilador_evaporador":
            fx.update(capacity=0.18)

        elif name == "puerta_mal_cerrada":
            fx.update(ext_gain=0.11)

        elif name == "corte_energia":
            fx.update(power_cut=True, comp_runs=False)

        elif name == "sensor_ruido":
            fx.update(spike_prob=0.10)

        # sensor_congelado: el valor se captura en _emit.

    # ---------------- Física ----------------

    def _local_hour(self, now: datetime) -> float:
        return (now.hour + now.minute / 60.0 - 5.0) % 24.0

    def _advance(self, now: datetime) -> None:
        if self._last_poll is None:
            self._last_poll = now
            return

        total = max(
            0.0,
            (now - self._last_poll).total_seconds(),
        )
        self._last_poll = now

        n = max(1, int(math.ceil(total / self.SUBSTEP_S)))
        dt_min = (total / n) / 60.0

        for _ in range(n):
            self._step(dt_min, now)

    def _door_is_open(self) -> bool:
        fx = self._fx
        if self._sim_min < fx["door_until_min"]:
            return True
        return any(a <= self._sim_min < b for a, b in fx["door_pulses"])

    def _progress(self) -> float:
        if self._ep_dur_min <= 0:
            return 0.0
        return min(1.0, max(0.0, (self._sim_min - self._ep_start_min) / self._ep_dur_min))

    def _capacity_now(self) -> float:
        fx = self._fx
        if fx["cap_ramp"]:
            c0, c1, frac = fx["cap_ramp"]
            return c0 + (c1 - c0) * min(1.0, self._progress() / frac)
        return fx["capacity"]

    def _comp_power_now(self) -> float:
        fx = self._fx
        if fx["pow_ramp"]:
            c0, c1, frac = fx["pow_ramp"]
            return c0 + (c1 - c0) * min(1.0, self._progress() / frac)
        return fx["comp_power"]

    def _write_marker(self, name: str, now: datetime, prod: dict) -> None:
        """Hecho de verdad que el bot NO puede ver (temperatura del producto)."""
        if not self._truth_path:
            return
        with open(self._truth_path, "a", encoding="utf-8", newline="\n") as fh:
            json.dump({"kind": "marker", "name": name, "at": now.isoformat(),
                       "since": prod["since"].isoformat(),
                       "product_t0": round(prod["T0"], 1)}, fh, ensure_ascii=False)
            fh.write("\n")

    def _control_temp(self) -> float:
        # Con el sensor congelado, el controlador actúa con ese valor.
        if (
            self._current_episode
            and self._current_episode.name == "sensor_congelado"
            and self._frozen_value is not None
        ):
            return self._frozen_value

        return self._T

    @property
    def compressor_running(self) -> bool:
        return self._refr and self._fx["comp_runs"] and not self._fx["power_cut"]

    def _step(self, dt: float, now: datetime) -> None:
        p, fx, rng = self.p, self._fx, self._rng
        self._sim_min += dt

        # Deshielo programado
        if not self._defr:
            self._since_defrost += dt

            if self._since_defrost >= p["defrost_every_min"]:
                self._defr, self._defr_left = (
                    True,
                    p["defrost_dur_min"],
                )
                self._since_defrost = 0.0

        else:
            self._defr_left -= dt

            if self._defr_left <= 0:
                self._defr = False

        # Controlador: histéresis + anti-ciclo corto
        tc = self._control_temp()
        sp = p["setpoint"] + fx["setpoint_add"]

        if fx["power_cut"]:
            self._refr = False

        elif self._defr:
            self._refr = False

        elif self._refr and tc <= sp:
            self._refr, self._last_off_min = (
                False,
                self._sim_min,
            )

        elif (
            not self._refr
            and tc >= sp + p["differential"]
            and self._sim_min - self._last_off_min >= p["min_off_min"]
        ):
            self._refr = True

        # Térmica
        hour = self._local_hour(now)

        amb = p["ambient_mean"] + fx["amb_add"] + p["ambient_amp"] * math.sin(
            2 * math.pi * (hour - 15.0) / 24.0
        )

        gain = (
            p["k_envelope"] * (amb - self._T)
            + rng.gauss(0, 0.002) * dt**0.5
        )

        door_open = self._door_is_open()
        extra = fx["extra_gain"]

        if fx["decay_tau"] > 0 and self._episode_started_at:
            el = (
                now - self._episode_started_at
            ).total_seconds() / 60.0

            extra *= math.exp(-el / fx["decay_tau"])

        if (
            door_open
            or fx["decay_tau"] > 0
            or (
                self._current_episode
                and self._current_episode.name == "carga_grande"
            )
        ):
            gain += extra

        if self._defr and not fx["power_cut"]:
            gain += p["defrost_heat"]

        gain += fx["ext_gain"]

        # Producto caliente: cede calor al aire y se enfría hacia él.
        if self._prod is not None:
            tp = self._prod["T"]
            gain += p["k_prod_air"] * max(0.0, tp - self._T)
            self._prod["T"] = tp - p["k_air_prod"] * (tp - self._T) * dt
            if (not self._prod["marked"]
                    and self._prod["T"] <= p["prod_report_c"]):
                self._prod["marked"] = True
                self._write_marker("producto_en_4C", now, self._prod)
            if self._prod["T"] - self._T < 0.3:
                if not self._prod["marked"]:
                    self._write_marker("producto_en_4C", now, self._prod)
                self._prod = None

        cooling = 0.0

        if self.compressor_running:
            cooling = p["cooling_rate"] * self._capacity_now()

        self._T += dt * (gain - cooling)

        # Red y circuito independiente
        self._v_grid_dev += (
            -0.05 * self._v_grid_dev * dt
            + rng.gauss(0, 0.25) * dt**0.5
        )

        self._m2_drift += (
            -0.04 * self._m2_drift * dt
            + rng.gauss(0, 0.12) * dt**0.5
        )

        # Energía (contador acumulado)
        m1, m2 = self._power_m1(hour), self._power_m2(hour)

        self._e["M1"] += m1["p"] * dt / 60.0
        self._e["M2"] += m2["p"] * dt / 60.0

        self._last_m1, self._last_m2 = m1, m2

        # Demanda activa: media móvil exponencial con tau = 15 minutos.
        alpha = 1.0 - math.exp(-dt / 15.0)

        for tag, pw in (("M1", m1), ("M2", m2)):
            prev = self._demand[tag]

            self._demand[tag] = (
                pw["p"]
                if prev is None
                else prev + alpha * (pw["p"] - prev)
            )

    # ---------------- Eléctrica ----------------

    def _power_m1(self, hour: float) -> dict:
        p, fx, rng = self.p, self._fx, self._rng

        kw = (
            p["m1_base_kw"]
            + 1.2 * math.sin(2 * math.pi * (hour - 9) / 24)
        )

        if self.compressor_running:
            kw += (
                p["m1_comp_kw"]
                * self._comp_power_now()
                * rng.uniform(0.98, 1.02)
            )

        if self._defr:
            kw += p["m1_heater_kw"]

        kw += rng.gauss(0, 0.15)

        pf = (
            0.85 if self.compressor_running else 0.74
        ) + rng.gauss(0, 0.004)

        return {
            "p": max(kw, 0.5),
            "pf": min(max(pf, 0.6), 0.95),
        }

    def _power_m2(self, hour: float) -> dict:
        p, rng = self.p, self._rng

        kw = (
            p["m2_base_kw"]
            + self._m2_drift
            + 0.8 * math.sin(2 * math.pi * (hour - 11) / 24)
            + rng.gauss(0, 0.1)
        )

        return {
            "p": max(kw, 0.5),
            "pf": 0.80 + rng.gauss(0, 0.003),
        }

    def _meter(
        self,
        tag: str,
        v_factor: float,
        f2_extra: float,
        pw: dict,
    ) -> dict:
        rng = self._rng

        vg = (
            self.p["v_phase"] + self._v_grid_dev
        ) * v_factor

        v = [vg + rng.gauss(0, 0.8) for _ in range(3)]
        v[1] *= 1.0 - f2_extra

        s0 = 0.335 + rng.gauss(0, 0.002)
        shares = [s0, 1.0 - s0 - 0.335, 0.335]
        pk = [pw["p"] * s for s in shares]

        pf = pw["pf"]
        cur = [
            pk[i] * 1000.0 / (v[i] * pf)
            for i in range(3)
        ]

        tan = math.tan(math.acos(pf))

        def sym(xs):
            """100 menos el desbalance máximo respecto al promedio (%)."""
            m = sum(xs) / 3.0

            if m <= 0:
                return 0.0

            return round(
                100.0
                - 100.0 * max(abs(x - m) for x in xs) / m,
                1,
            )

        return {
            f"{tag} Tensión F1": round(v[0]),
            f"{tag} Tensión F2": round(v[1]),
            f"{tag} Tensión F3": round(v[2]),

            f"{tag} Corriente F1": round(cur[0], 1),
            f"{tag} Corriente F2": round(cur[1], 1),
            f"{tag} Corriente F3": round(cur[2], 1),

            f"{tag} Potencia Activa": round(pw["p"], 2),
            f"{tag} Potencia Reactiva": round(pw["p"] * tan, 2),
            f"{tag} Potencia Aparente": round(pw["p"] / pf, 2),
            f"{tag} Factor de Potencia": round(pf, 2),
            f"{tag} Frecuencia": round(
                self.p["freq"] + rng.gauss(0, 0.02),
                2,
            ),

            f"{tag} Simetría Tensión": sym(v),
            f"{tag} Simetría Corriente": sym(cur),
            f"{tag} Demanda Activa": round(
                self._demand[tag]
                if self._demand[tag] is not None
                else pw["p"],
                2,
            ),

            f"{tag} Energía Activa": round(self._e[tag], 1),
        }

    # ---------------- Emisión ----------------

    def _emit(self, now: datetime) -> list[ChannelReading]:
        p, rng = self.p, self._rng

        ep = (
            self._current_episode.name
            if self._current_episode
            else "normal"
        )

        shown = round(self._T + rng.gauss(0, 0.05), 1)

        if self._fx["spike_prob"] and rng.random() < self._fx["spike_prob"]:
            shown = round(shown + rng.choice([-1, 1]) * rng.uniform(4.0, 9.0), 1)

        if ep == "sensor_congelado":
            if self._frozen_value is None:
                self._frozen_value = shown

            shown = self._frozen_value

        door = self._door_is_open()
        sp_now = p["setpoint"] + self._fx["setpoint_add"]

        values: dict[str, float] = {
            CH_TEMP: shown,
            CH_SP: sp_now,
            CH_DESVIO: round(shown - sp_now, 1),
            CH_REFR: 1.0 if self._refr else 0.0,
            CH_DEFR: 1.0 if self._defr else 0.0,
            CH_PUERTA: 1.0 if door else 0.0,
            CH_HOT: 0.0,
        }

        hour = self._local_hour(now)

        m1_pw = self._last_m1 or self._power_m1(hour)
        m2_pw = self._last_m2 or self._power_m2(hour)

        values.update(
            self._meter(
                "M1",
                self._fx["v_factor"],
                self._fx["f2_extra"],
                m1_pw,
            )
        )

        values.update(
            self._meter(
                "M2",
                1.0,
                0.0,
                m2_pw,
            )
        )

        out = []

        for name, unit in ALL_CHANNELS:
            inst = CTRL if name in _CONTROLLER_NAMES else MP

            if self._fx["power_cut"] or (
                ep == "sin_comunicacion"
                and inst == self._comm_target
            ):
                out.append(
                    ChannelReading(
                        self.device_id,
                        name,
                        None,
                        unit,
                        DataQuality.MISSING,
                        now,
                        "",
                    )
                )

            else:
                v = float(values[name])

                out.append(
                    ChannelReading(
                        self.device_id,
                        name,
                        v,
                        unit,
                        DataQuality.VALID,
                        now,
                        str(v),
                    )
                )

        return out