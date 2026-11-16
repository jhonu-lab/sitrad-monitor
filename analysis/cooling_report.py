"""
analysis/cooling_report.py — informes de temperatura de la cámara.

El ingeniero no necesita datos: necesita un INFORME. Por ejemplo, el ciclo de
enfriamiento exigido por la norma: de cuánto subió la temperatura, cuánto
tardó en volver al límite (4 °C) y si cumplió.

Alcance actual: SOLO la termocupla de la cámara (la que lee el controlador).
No es la temperatura del producto: el informe lo dice con claridad. Si el
controlador también entrega puerta / deshielo, se usan solo como contexto
("coincidió con puerta abierta 14 min"), nunca como causa confirmada.

Todo se calcula en código a partir de las lecturas; las cifras no las inventa
nadie. No usa LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from core.clock import local_tz

GOOD = ("VALID", "FROZEN")
_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


# ----------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class CoolingConfig:
    limit_c: float = 4.0              # temperatura que exige la norma
    alert_after_min: float = 10.0     # aviso si lleva tanto sobre el límite
    settle_min: float = 10.0          # debe mantenerse ≤ límite para darlo por recuperado
    merge_min: float = 10.0           # dos subidas separadas menos que esto = un episodio
    max_recovery_min: float | None = None   # tiempo máximo permitido (None = no configurado)
    min_history: int = 3              # episodios previos para comparar con lo habitual
    # Aviso temprano (ver analysis/rise_watch.py)
    warn_slope_c_h: float = 3.0       # subida mínima para vigilar (°C/h)
    warn_r2: float = 0.7              # el ajuste de la recta debe ser bueno
    warn_horizon_min: float = 30.0    # solo avisa si llegaría al objetivo dentro de esto
    warn_cooldown_min: float = 30.0
    trend_window_min: float = 20.0
    door_warn_min: float = 5.0        # puerta abierta más de esto con la temperatura subiendo
    critical_c: float | None = None   # nivel crítico (defecto: límite + 4 °C)
    product_rules: dict = field(default_factory=dict)   # {"ternera": {"max_minutes_above": 120}}
    notify_defrost: bool = False      # ¿avisar de subidas que explica un deshielo normal?
    defrost_notify_above_min: float = 45.0   # ...salvo que duren más que esto
    camera_name: str = "la cámara"
    tz_name: str = "America/Bogota"

    @classmethod
    def from_dict(cls, d: dict | None) -> "CoolingConfig":
        d = d or {}
        mx = d.get("max_recovery_min")
        return cls(
            limit_c=float(d.get("temp_limit_c", 4.0)),
            alert_after_min=float(d.get("alert_after_min", 10.0)),
            settle_min=float(d.get("settle_min", 10.0)),
            merge_min=float(d.get("merge_min", 10.0)),
            max_recovery_min=None if mx is None else float(mx),
            min_history=int(d.get("min_history", 3)),
            warn_slope_c_h=float(d.get("warn_slope_c_h", 3.0)),
            warn_r2=float(d.get("warn_r2", 0.7)),
            warn_horizon_min=float(d.get("warn_horizon_min", 30.0)),
            warn_cooldown_min=float(d.get("warn_cooldown_min", 30.0)),
            trend_window_min=float(d.get("trend_window_min", 20.0)),
            door_warn_min=float(d.get("door_warn_min", 5.0)),
            critical_c=(None if d.get("critical_c") is None else float(d["critical_c"])),
            product_rules=dict(d.get("product_rules", {}) or {}),
            notify_defrost=bool(d.get("notify_defrost", False)),
            defrost_notify_above_min=float(d.get("defrost_notify_above_min", 45.0)),
            camera_name=str(d.get("camera_name", "la cámara")),
            tz_name=str(d.get("timezone", "America/Bogota")),
        )


# ----------------------------------------------------------------------
# Episodios
# ----------------------------------------------------------------------

@dataclass
class Excursion:
    start: datetime                   # primera lectura sobre el límite
    peak_t: datetime
    peak_c: float
    prev_c: float | None              # nivel previo (mediana de los 30 min antes)
    end: datetime | None              # primera lectura ≤ límite (recuperado); None = sigue arriba
    last_t: datetime                  # última lectura vista del episodio
    door_min: float = 0.0
    defrost: bool = False              # un deshielo estaba activo al empezar la subida
    defrost_end: datetime | None = None   # cuándo terminó ese deshielo
    last_c: float | None = None        # última temperatura vista
    refr_duty: float | None = None     # fracción del tiempo con orden de frío (REFR) durante el episodio
    gap_min: float = 0.0               # mayor hueco sin lecturas dentro del episodio
    n_above: int = 0                   # lecturas sobre el límite
    kind: str = ""                     # clasificación (ver classify)
    load: object = None                # producto registrado que entró justo antes de la subida
    gap_t0: datetime | None = None     # cuándo empezó el mayor hueco sin lecturas del episodio
    sp_now: float | None = None        # setpoint al empezar la subida
    sp_change: tuple | None = None     # (instante, antes, después) si cambió poco antes

    @property
    def recovered(self) -> bool:
        return self.end is not None

    @property
    def above_min(self) -> float:
        return ((self.end or self.last_t) - self.start).total_seconds() / 60.0

    @property
    def recovery_min(self) -> float | None:
        """Minutos desde el máximo hasta volver al límite."""
        if self.end is None:
            return None
        return max(0.0, (self.end - self.peak_t).total_seconds() / 60.0)

    @property
    def rise_c(self) -> float | None:
        return None if self.prev_c is None else self.peak_c - self.prev_c

    @property
    def key(self) -> str:
        return self.start.isoformat()


def _good(rs):
    return [r for r in rs if r.value is not None and r.quality.value in GOOD]


def find_excursions(readings, cfg: CoolingConfig, door=None, defrost=None, refr=None,
                    loads=None, setpoint=None) -> list[Excursion]:
    """Episodios con la temperatura por encima del límite, en orden cronológico."""
    rs = sorted(_good(readings), key=lambda r: r.event_time)
    out: list[Excursion] = []
    i, n = 0, len(rs)
    settle = timedelta(minutes=cfg.settle_min)
    merge = timedelta(minutes=cfg.merge_min)
    while i < n:
        if float(rs[i].value) <= cfg.limit_c:
            i += 1
            continue
        s = i
        peak_i = i
        j = i
        end_idx = None
        while j < n:
            v = float(rs[j].value)
            if v > float(rs[peak_i].value):
                peak_i = j
            if v <= cfg.limit_c:
                # ¿se mantiene ≤ límite durante settle? (o llega a una subida que sí debe unirse)
                k = j
                while k < n and float(rs[k].value) <= cfg.limit_c:
                    k += 1
                span = (rs[min(k, n) - 1].event_time - rs[j].event_time)
                if k < n and (rs[k].event_time - rs[j].event_time) < merge:
                    j = k                      # volvió a subir enseguida: mismo episodio
                    continue
                if span >= settle or k < n:
                    end_idx = j
                    j = k
                    break
                # los datos se acaban antes de confirmar el asentamiento:
                # todavía no se da por recuperado (puede volver a subir).
                end_idx = None
                j = k
                break
            j += 1
        start_t = rs[s].event_time
        base = [float(r.value) for r in rs[:s]
                if start_t - timedelta(minutes=30) <= r.event_time < start_t]
        prev_c = median(base) if base else None
        last_i = (end_idx if end_idx is not None else n - 1)
        exc = Excursion(
            start=start_t, peak_t=rs[peak_i].event_time, peak_c=float(rs[peak_i].value),
            prev_c=prev_c, end=(rs[end_idx].event_time if end_idx is not None else None),
            last_t=rs[last_i].event_time, last_c=float(rs[last_i].value))
        exc.n_above = sum(1 for r in rs[s:last_i + 1] if float(r.value) > cfg.limit_c)
        exc.gap_min, exc.gap_t0 = _max_gap(rs, exc.start - timedelta(minutes=5), (exc.end or exc.last_t) + timedelta(minutes=5))
        _add_context(exc, door, defrost, refr)
        if loads:
            # producto que entró en la hora previa a la subida (o hasta el máximo)
            near = [l for l in loads if exc.start - timedelta(minutes=60) <= l.t_in <= exc.peak_t]
            exc.load = max(near, key=lambda l: l.t_in) if near else None
        if setpoint:
            sps = [r for r in _good(setpoint) if r.event_time <= exc.start]
            exc.sp_now = float(sps[-1].value) if sps else None
            ch = setpoint_changes(setpoint, exc.start - timedelta(minutes=180), exc.peak_t)
            exc.sp_change = ch[-1] if ch else None
        exc.kind = classify(exc, cfg)
        out.append(exc)
        i = max(j, s + 1)
    return out


def _state_minutes(readings, t0: datetime, t1: datetime) -> float:
    """Minutos con estado=1 entre t0 y t1 (cada lectura vale hasta la siguiente)."""
    rs = sorted(_good(readings or []), key=lambda r: r.event_time)
    tot = 0.0
    for a, b in zip(rs, rs[1:]):
        if float(a.value) >= 0.5:
            lo, hi = max(a.event_time, t0), min(b.event_time, t1)
            if hi > lo:
                tot += (hi - lo).total_seconds() / 60.0
    return tot


def _on_intervals(readings, t0: datetime, t1: datetime):
    rs = sorted(_good(readings or []), key=lambda r: r.event_time)
    out, cur = [], None
    for r in rs:
        if r.event_time > t1:
            break
        on = float(r.value) >= 0.5
        if on and cur is None:
            cur = r.event_time
        elif not on and cur is not None:
            out.append((cur, r.event_time))
            cur = None
    if cur is not None:
        out.append((cur, rs[-1].event_time))
    return [(a, b) for a, b in out if b >= t0]


def _max_gap(rs, t0: datetime, t1: datetime):
    """(minutos, inicio) del mayor hueco entre lecturas que toque [t0, t1] (incluye el que termina en t0)."""
    best, at = 0.0, None
    for a, b in zip(rs, rs[1:]):
        if b.event_time >= t0 and a.event_time <= t1:
            m = (b.event_time - a.event_time).total_seconds() / 60.0
            if m > best:
                best, at = m, a.event_time
    return best, at


def _state_fraction(readings, t0: datetime, t1: datetime) -> float | None:
    """Fracción del tiempo con estado=1 entre t0 y t1 (None si no hay datos)."""
    rs = sorted(_good(readings or []), key=lambda r: r.event_time)
    on = tot = 0.0
    for a, b in zip(rs, rs[1:]):
        lo, hi = max(a.event_time, t0), min(b.event_time, t1)
        if hi > lo:
            d = (hi - lo).total_seconds()
            tot += d
            if float(a.value) >= 0.5:
                on += d
    return (on / tot) if tot > 0 else None


def _add_context(exc: Excursion, door, defrost, refr=None) -> None:
    t0 = exc.start - timedelta(minutes=30)
    t1 = exc.end or exc.last_t
    if door:
        # Solo la puerta abierta ANTES del máximo explica la subida; abrirla durante
        # la recuperación no es la causa del calor que ya había entrado.
        exc.door_min = _state_minutes(door, t0, min(t1, exc.peak_t + timedelta(minutes=5)))
    if defrost:
        # ¿hubo un deshielo en curso (o recién terminado) cuando empezó la subida?
        near = [(a, b) for a, b in _on_intervals(defrost, exc.start - timedelta(minutes=15), exc.peak_t)
                if a <= exc.start]
        if near:
            exc.defrost = True
            exc.defrost_end = max(b for _, b in near)
    if refr:
        # trabajo del compresor desde el máximo (la recuperación), o todo el episodio si sigue subiendo
        a = exc.peak_t if exc.recovered and (t1 - exc.peak_t).total_seconds() >= 600 else exc.start
        exc.refr_duty = _state_fraction(refr, a, t1)


DEFROST_TAIL_MIN = 30.0     # tras terminar el deshielo, la subida normal dura hasta ~esto


def defrost_explains(exc: Excursion, cfg: "CoolingConfig | None" = None) -> bool:
    """El deshielo explica TODA la subida: el máximo llegó poco después de que
    terminara y la duración total es la esperable para un deshielo."""
    if not exc.defrost or exc.defrost_end is None:
        return False
    if cfg is not None and exc.above_min >= cfg.defrost_notify_above_min:
        return False
    return exc.peak_t <= exc.defrost_end + timedelta(minutes=DEFROST_TAIL_MIN)


ATENCION = {"setpoint", "deshielo_largo", "capacidad", "sin_frio", "corte_datos", "sin_causa", "carga_con_puerta"}
CORTA_MIN = 45.0       # una apertura de puerta normal se recupera dentro de esto


def classify(exc: Excursion, cfg: CoolingConfig) -> str:
    """
    Tipo de episodio, a partir de lo que MIDEN la termocupla y el controlador.
    Son hipótesis compatibles con los datos, no causas confirmadas.
    """
    # Un tramo sin lecturas solo explica la subida si empezó antes del máximo; un hueco
    # posterior (durante la recuperación) no pudo causarla.
    if (exc.gap_min >= 10.0 and exc.gap_t0 is not None
            and exc.gap_t0 <= exc.peak_t + timedelta(minutes=2)):
        return "corte_datos"
    if exc.n_above <= 2 and (exc.rise_c or 0.0) >= 3.0 and exc.door_min < 1.0:
        return "lectura_espuria"
    if exc.load is not None:
        return "ingreso_producto"        # el operario registró producto justo antes de la subida
    if exc.sp_now is not None and exc.sp_now >= cfg.limit_c - 1.5:
        return "setpoint"                # con ese setpoint la cámara regula cerca o sobre el límite
    if defrost_explains(exc, cfg):
        return "deshielo"
    if exc.defrost:
        return "deshielo_largo"
    d = exc.refr_duty
    if exc.door_min >= 1.0:
        if exc.above_min <= CORTA_MIN:
            return "puerta"
        return "carga_con_puerta"
    if d is not None:
        if d >= 0.9:
            if (not exc.recovered) and exc.above_min < 45.0:
                return "en_curso"          # aún es pronto para decir si es carga o falla
            if (not exc.recovered) or exc.above_min > 90.0:
                return "capacidad"
            return "carga_termica"
        if d <= 0.3:
            return "sin_frio"
    return "sin_causa"


LABEL = {
    "deshielo": "deshielo",
    "deshielo_largo": "deshielo que se prolongó",
    "puerta": "puerta abierta",
    "carga_con_puerta": "puerta abierta y recuperación lenta, posible ingreso de producto",
    "carga_termica": "carga térmica alta",
    "capacidad": "posible pérdida de capacidad de enfriamiento",
    "sin_frio": "el controlador no pidió frío",
    "corte_datos": "tramo sin lecturas",
    "lectura_espuria": "lectura aislada (posible falso contacto)",
    "sin_causa": "sin causa visible",
    "en_curso": "en curso, aún es pronto para saber la causa",
    "ingreso_producto": "ingreso de producto",
    "setpoint": "setpoint alto o cambiado",
}

# Qué revisar según el tipo de episodio (conocimiento general de refrigeración; son sugerencias).
ACCION = {
    "deshielo": None,
    "deshielo_largo": "Revisar el fin de deshielo (sensor o tiempo programado) y que no quede hielo en el evaporador.",
    "puerta": "Verificar que la puerta cierre bien y reducir el tiempo que permanece abierta.",
    "carga_con_puerta": "Si no hubo ingreso de producto, revisar el cierre de la puerta; si lo hubo, registrarlo "
                        "(\"hay 500 kg de ternera...\") para seguirlo.",
    "carga_termica": "Revisar a qué temperatura ingresó el producto y el calor ambiente; repartir las cargas grandes.",
    "capacidad": "Revisar hielo en el evaporador, funcionamiento del ventilador y nivel de refrigerante; si hay "
                 "manómetros, la presión de succión y descarga (hoy no tengo esos datos).",
    "sin_frio": "Revisar el setpoint y la configuración del controlador.",
    "corte_datos": "Revisar la alimentación eléctrica y la comunicación con SITRAD.",
    "lectura_espuria": "Revisar el cableado y el conector de la termocupla.",
    "sin_causa": "Inspeccionar la cava en sitio: puerta, evaporador, ventilador y carga reciente.",
    "en_curso": "Seguir la evolución en los próximos minutos.",
    "setpoint": "Confirmar si el setpoint es intencional; con el límite en 4 °C debe quedar con margen "
                "(el compresor arranca por encima del setpoint). Si fue un cambio accidental, restablecerlo.",
    "ingreso_producto": "Comprobar que el producto no ingresó por encima de la temperatura prevista y, si son "
                        "cargas grandes, ingresarlas por tandas.",
}


# ----------------------------------------------------------------------
# Actividad de la cámara en un periodo (lo que miden termocupla y controlador)
# ----------------------------------------------------------------------

def door_activity(door, t0: datetime, t1: datetime):
    """(aperturas, minutos abierta, apertura más larga en min)."""
    rs = [r for r in sorted(_good(door or []), key=lambda r: r.event_time) if t0 <= r.event_time <= t1]
    opens, total, longest, start = 0, 0.0, 0.0, None
    prev = None
    for r in rs:
        on = float(r.value) >= 0.5
        if on and start is None:
            start, opens = r.event_time, opens + 1
        elif not on and start is not None:
            m = (r.event_time - start).total_seconds() / 60.0
            total += m
            longest = max(longest, m)
            start = None
        prev = r
    if start is not None and prev is not None:
        m = (prev.event_time - start).total_seconds() / 60.0
        total += m
        longest = max(longest, m)
    return opens, total, longest


def duty_blocks(refr, t0: datetime, t1: datetime, block_h: float = 4.0):
    """Fracción de tiempo con orden de frío, por bloques consecutivos."""
    out, a = [], t0
    step = timedelta(hours=block_h)
    while a + step <= t1 + timedelta(minutes=1):
        d = _state_fraction(refr, a, a + step)
        if d is not None:
            out.append(d)
        a += step
    return out


def setpoint_changes(sp, t0: datetime, t1: datetime, min_delta: float = 0.5):
    """[(instante, antes, después)] cuando el setpoint cambia de forma sostenida."""
    rs = [r for r in sorted(_good(sp or []), key=lambda r: r.event_time) if t0 <= r.event_time <= t1]
    out, cur = [], None
    for r in rs:
        v = float(r.value)
        if cur is None:
            cur = v
        elif abs(v - cur) >= min_delta:
            out.append((r.event_time, cur, v))
            cur = v
    return out


def data_gaps(readings, t0: datetime, t1: datetime, min_gap_min: float = 10.0):
    """[(desde, hasta, minutos)] tramos sin lecturas válidas de la termocupla."""
    pts = [r.event_time for r in sorted(_good(readings), key=lambda r: r.event_time) if t0 <= r.event_time <= t1]
    return [(a, b, (b - a).total_seconds() / 60.0) for a, b in zip(pts, pts[1:])
            if (b - a).total_seconds() / 60.0 >= min_gap_min]


# ----------------------------------------------------------------------
# Formato
# ----------------------------------------------------------------------

def _n(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _dur(minutes: float) -> str:
    m = int(round(minutes))
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h" if r == 0 else f"{h} h {r:02d} min"


class _Fmt:
    def __init__(self, tz_name: str) -> None:
        self.tz = local_tz(tz_name)

    def hm(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%H:%M")

    def dh(self, t: datetime) -> str:
        l = t.astimezone(self.tz)
        return f"{l.day} {_MESES[l.month - 1]} {l:%H:%M}"


# ----------------------------------------------------------------------
# Informe de un episodio
# ----------------------------------------------------------------------

def _usual(history: list[Excursion], cfg: CoolingConfig, exc: Excursion):
    prev = [h for h in history if h.recovered and h.key != exc.key and h.start < exc.start]
    if len(prev) < cfg.min_history:
        return None
    return len(prev), median(h.above_min for h in prev)


def _cause_line(exc: Excursion, cfg: CoolingConfig) -> str:
    k = exc.kind or classify(exc, cfg)
    duty = f"{exc.refr_duty:.0%}" if exc.refr_duty is not None else None
    if k == "setpoint" and exc.sp_now is not None:
        t = f"El setpoint está en {_n(exc.sp_now)} °C"
        if exc.sp_change is not None:
            tt, a0, b0 = exc.sp_change
            t += f" (cambió de {_n(a0)} a {_n(b0)} °C a las {_Fmt(cfg.tz_name).hm(tt)})"
        return (t + f". Con ese ajuste la cámara regula cerca o por encima del límite de {_n(cfg.limit_c, 0)} °C, "
                "porque el compresor arranca por encima del setpoint: no es una falla, es la configuración.")
    if k == "ingreso_producto" and exc.load is not None:
        hm = _Fmt(cfg.tz_name).hm(exc.load.t_in)
        t = f"Coincide con el ingreso de producto registrado: {exc.load.label()} a las {hm}"
        if exc.load.entry_temp_c is not None:
            t += f", que entró a {_n(exc.load.entry_temp_c)} °C"
        return t + ". El producto tibio le cede calor al aire de la cámara, y es lo esperable."
    if k == "deshielo":
        return ("Coincidió con un deshielo: es normal que la temperatura suba mientras "
                "se derrite la escarcha.")
    if k == "deshielo_largo":
        return ("Empezó con un deshielo, pero la subida duró más de lo normal para uno "
                f"({_dur(exc.above_min)} sobre el límite): conviene revisar la cava.")
    if k == "puerta":
        return (f"Coincidió con la puerta abierta ({_dur(exc.door_min)}), lo que explica "
                "la entrada de calor y una recuperación corta.")
    if k == "carga_con_puerta":
        return (f"Con la puerta abierta ({_dur(exc.door_min)}) y una recuperación lenta "
                f"({_dur(exc.above_min)} sobre el límite). Es compatible con el ingreso de "
                "producto caliente o una puerta abierta mucho tiempo; si no hubo carga, revisar.")
    if k == "carga_termica":
        return (f"Sin puerta ni deshielo. El compresor trabajó {duty} del tiempo mientras la cámara "
                "se recuperaba: es compatible con una carga térmica alta (producto caliente o calor "
                "ambiente) más que con una falla.")
    if k == "capacidad":
        return (f"Sin puerta ni deshielo. El compresor trabajó {duty} del tiempo y aun así la "
                "cámara no bajó con normalidad: es compatible con pérdida de capacidad de "
                "enfriamiento (evaporador con hielo, ventilador o refrigerante). Conviene revisar la cava.")
    if k == "en_curso":
        return (f"Sin puerta ni deshielo. El compresor trabaja {duty} del tiempo. Todavía es pronto para "
                "saber si es una carga térmica pasajera (producto tibio, calor) o una pérdida de capacidad; "
                "si en unos minutos no empieza a bajar, conviene revisar la cava.")
    if k == "sin_frio":
        return (f"Sin puerta ni deshielo, y el controlador pidió frío solo {duty} del tiempo "
                "con la cámara caliente: revisar el setpoint o el controlador.")
    if k == "corte_datos":
        return (f"Hubo {_dur(exc.gap_min)} sin lecturas durante el episodio (posible corte de energía "
                "o de comunicación): la subida puede deberse a eso.")
    if k == "lectura_espuria":
        return ("Fue una lectura aislada que subió de golpe y volvió: es compatible con un falso "
                "contacto de la termocupla más que con un calentamiento real.")
    return ("No hay una puerta abierta ni un deshielo que expliquen la subida. Con la "
            "termocupla sola no se puede saber la causa; conviene revisar la cava.")


def _render_episode_detail(exc: Excursion, history: list[Excursion], cfg: CoolingConfig,
                           temp_readings=None) -> str:
    f = _Fmt(cfg.tz_name)
    L = cfg.limit_c
    lines: list[str] = [f"INFORME DE TEMPERATURA — {cfg.camera_name}"]
    sube = (f"subió de {_n(exc.prev_c)} °C a un máximo de {_n(exc.peak_c)} °C"
            if exc.prev_c is not None else f"alcanzó un máximo de {_n(exc.peak_c)} °C")
    lines.append(
        f"Desde las {f.hm(exc.start)} la temperatura de {cfg.camera_name} superó el límite "
        f"de {_n(L, 0)} °C: {sube} a las {f.hm(exc.peak_t)}"
        + (f" (+{_n(exc.rise_c)} °C)." if exc.rise_c is not None else "."))
    if exc.recovered:
        rate = ((exc.peak_c - L) / (exc.recovery_min / 60.0)) if exc.recovery_min and exc.recovery_min >= 10 else None
        lines.append(
            f"Volvió a {_n(L, 0)} °C a las {f.hm(exc.end)}: {_dur(exc.recovery_min)} después del "
            f"máximo y {_dur(exc.above_min)} en total sobre el límite"
            + (f" (enfrió a {_n(rate)} °C/h)." if rate else "."))
        usual = _usual(history, cfg, exc)
        if usual:
            n_prev, med = usual
            if med > 0:
                diff = (exc.above_min / med - 1.0) * 100.0
                if abs(diff) >= 15:
                    word = "más lenta" if diff > 0 else "más rápida"
                    lines.append(
                        f"Frente a los {n_prev} episodios anteriores (mediana {_dur(med)} sobre "
                        f"el límite), esta recuperación fue {abs(diff):.0f}% {word}.")
                else:
                    lines.append(
                        f"Es similar a los {n_prev} episodios anteriores (mediana {_dur(med)}).")
        lines.append(_cause_line(exc, cfg))
        lines += _load_lines(exc, cfg, temp_readings)
        if cfg.max_recovery_min is not None:
            ok = exc.above_min <= cfg.max_recovery_min
            lines.append(
                f"Cumplimiento: {'✅ CUMPLE' if ok else '❌ NO CUMPLE'} — tardó {_dur(exc.above_min)} "
                f"y el máximo permitido es {_dur(cfg.max_recovery_min)}.")
        else:
            lines.append(f"Cumplimiento: ✅ volvió a ≤ {_n(L, 0)} °C. (No hay un tiempo máximo "
                         "configurado para evaluar el plazo.)")
    else:
        if exc.last_c is not None and exc.last_c <= L:
            lines.append(
                f"Ya bajó a {_n(exc.last_c)} °C (a las {f.hm(exc.last_t)}); se espera confirmar "
                f"que se mantiene {_dur(cfg.settle_min)} antes de darlo por recuperado.")
        else:
            lines.append(
                f"Aún no vuelve al límite: lleva {_dur(exc.above_min)} sobre {_n(L, 0)} °C "
                f"(última lectura a las {f.hm(exc.last_t)}).")
        lines.append(_cause_line(exc, cfg))
        lines += _load_lines(exc, cfg, temp_readings)
        lines.append("Cumplimiento: ❌ pendiente — se informará cuando se recupere.")
    if exc.gap_min >= 10.0 and (exc.kind or classify(exc, cfg)) != "corte_datos":
        lines.append(f"Ojo: hubo {_dur(exc.gap_min)} sin lecturas durante el episodio; en ese tramo la curva "
                     "real pudo ser distinta de la que se ve.")
    acc = ACCION.get(exc.kind or classify(exc, cfg))
    if acc:
        lines.append("Qué revisar: " + acc)
    lines.append("Nota: es la temperatura del aire de la cámara (termocupla), no la del producto.")
    return "\n".join(lines)


def _load_lines(exc: Excursion, cfg: CoolingConfig, temp_readings=None) -> list[str]:
    """Lo que el ingeniero quiere ver: desde que entró el producto, cuánto tardó la cámara en volver al límite."""
    l = exc.load
    if l is None:
        return []
    f = _Fmt(cfg.tz_name)
    out = []
    end = exc.end or exc.last_t
    since = (end - l.t_in).total_seconds() / 60.0
    if exc.recovered:
        out.append(f"Desde que entró el producto ({f.hm(l.t_in)}) la cámara tardó {_dur(since)} en volver a "
                   f"{_n(cfg.limit_c, 0)} °C.")
    else:
        out.append(f"Desde que entró el producto ({f.hm(l.t_in)}) han pasado {_dur(since)} y la cámara "
                   f"aún no vuelve a {_n(cfg.limit_c, 0)} °C.")
    if temp_readings:
        from analysis.product_log import ICON, assess_loads, render_load_line
        for r in assess_loads([l], temp_readings, cfg.limit_c, end, cfg.product_rules):
            out.append(f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name))
    return out


def _render_alert_detail(exc: Excursion, cfg: CoolingConfig) -> str:
    f = _Fmt(cfg.tz_name)
    return (
        f"🟠 {cfg.camera_name[:1].upper() + cfg.camera_name[1:]} sobre el límite\n"
        f"Lleva {_dur(exc.above_min)} por encima de {_n(cfg.limit_c, 0)} °C "
        f"(desde las {f.hm(exc.start)}); máximo hasta ahora {_n(exc.peak_c)} °C.\n"
        + (("Empezó con un deshielo, pero ya dura más de lo normal para uno.")
           if exc.defrost and not defrost_explains(exc, cfg)
           else _cause_line(exc, cfg))
        + "\nSe enviará el informe cuando vuelva al límite.")


# ----------------------------------------------------------------------
# Informe de un periodo (turno / día)
# ----------------------------------------------------------------------

def _render_period_detail(readings, t0: datetime, t1: datetime, cfg: CoolingConfig,
                          door=None, defrost=None, refr=None, setpoint=None, loads=None) -> str:
    f = _Fmt(cfg.tz_name)
    from analysis.forecast import despike
    all_rs = sorted((r for r in _good(readings) if t0 <= r.event_time <= t1), key=lambda r: r.event_time)
    rs = despike(all_rs)
    spikes = [r for r in all_rs if r not in set(rs)]
    if len(rs) < 2:
        return (f"INFORME DE TEMPERATURA — {cfg.camera_name}\n"
                "No hay lecturas suficientes de la termocupla en este periodo.")
    L = cfg.limit_c
    tot = ok = 0.0
    for a, b in zip(rs, rs[1:]):
        dt = min((b.event_time - a.event_time).total_seconds() / 60.0, 15.0)
        tot += dt
        if float(a.value) <= L:
            ok += dt
    vals = [float(r.value) for r in rs]
    exs = [e for e in find_excursions(readings, cfg, door, defrost, refr, loads, setpoint)
           if t0 <= e.start <= t1 and e.kind != "lectura_espuria"]
    hrs = (rs[-1].event_time - rs[0].event_time).total_seconds() / 3600.0
    pct = 100.0 * ok / tot if tot else 0.0
    lines = [
        f"INFORME DE TEMPERATURA — {cfg.camera_name}",
        f"Periodo: {f.dh(rs[0].event_time)} a {f.dh(rs[-1].event_time)} ({_n(hrs)} h).",
        f"La temperatura estuvo en ≤ {_n(L, 0)} °C el {pct:.0f}% del tiempo "
        f"(promedio {_n(sum(vals) / len(vals))} °C, mínimo {_n(min(vals))} °C, "
        f"máximo {_n(max(vals))} °C).",
    ]
    if not exs:
        lines.append(f"✅ No hubo episodios sobre el límite de {_n(L, 0)} °C.")
    else:
        lines.append(f"Hubo {len(exs)} episodio(s) sobre el límite, "
                     f"{_dur(sum(e.above_min for e in exs))} en total:")
        for k, e in enumerate(exs, 1):
            est = (f"recuperado a las {f.hm(e.end)}" if e.recovered else "SIGUE SOBRE EL LÍMITE")
            ctx = LABEL.get(e.kind or classify(e, cfg), "sin causa visible")
            if e.kind == "puerta":
                ctx = f"puerta abierta {_dur(e.door_min)}"
            lines.append(f"  {k}. {f.hm(e.start)} — máximo {_n(e.peak_c)} °C, "
                         f"{_dur(e.above_min)} sobre el límite, {est} ({ctx}).")
        sin = [e for e in exs if (e.kind or classify(e, cfg)) in ATENCION]
        if sin:
            lines.append(f"Atención: {len(sin)} episodio(s) conviene(n) revisarse (ver arriba: "
                         + ", ".join(sorted({LABEL[e.kind] for e in sin})) + ").")
        if cfg.max_recovery_min is not None:
            fuera = [e for e in exs if e.above_min > cfg.max_recovery_min]
            lines.append(("❌ " + f"{len(fuera)} episodio(s) excedieron el plazo de "
                          f"{_dur(cfg.max_recovery_min)}.") if fuera
                         else f"✅ Todos dentro del plazo de {_dur(cfg.max_recovery_min)}.")
    # ---- operación de la cámara en el periodo ----
    ops: list[str] = []
    if door:
        n, tot_m, long_m = door_activity(door, t0, t1)
        if n:
            ops.append(f"Puerta: {n} apertura(s), {_dur(tot_m)} abierta en total (la más larga, {_dur(long_m)}).")
        else:
            ops.append("Puerta: no se registraron aperturas.")
    if refr:
        d_all = _state_fraction(refr, t0, t1)
        if d_all is not None:
            txt = f"Compresor: pidió frío el {d_all:.0%} del tiempo"
            blocks = duty_blocks(refr, t0, t1)
            if len(blocks) >= 3:
                txt += " (por bloques de 4 h: " + ", ".join(f"{b:.0%}" for b in blocks) + ")"
            ops.append(txt + ".")
            if len(blocks) >= 3:
                prev_med = median(blocks[:-1])
                if blocks[-1] >= prev_med + 0.10 and blocks[-1] >= prev_med * 1.25:
                    ops.append("Atención: el compresor viene trabajando más que antes "
                               f"({blocks[-1]:.0%} frente a {prev_med:.0%}); puede ser más carga térmica "
                               "o pérdida de eficiencia. Conviene vigilarlo.")
    if setpoint:
        for tt, a, b in setpoint_changes(setpoint, t0, t1):
            ops.append(f"Setpoint: cambió de {_n(a)} a {_n(b)} °C a las {f.hm(tt)}.")
    gaps = data_gaps(readings, t0, t1)
    if gaps:
        tot_g = sum(g[2] for g in gaps)
        ops.append(f"Datos: {len(gaps)} interrupción(es) de la termocupla, {_dur(tot_g)} sin lecturas "
                   "(" + "; ".join(f"{f.hm(g[0])}–{f.hm(g[1])}" for g in gaps[:4]) + "). "
                   "Pudo ser un corte de energía o de comunicación; en ese tiempo no se sabe la temperatura.")
    if spikes:
        ops.append(f"Sensor: {len(spikes)} lectura(s) aislada(s) sospechosa(s) (picos de hasta "
                   f"{_n(max(float(r.value) for r in spikes))} °C que vuelven enseguida; posible falso contacto "
                   "de la termocupla). No se cuentan en las estadísticas.")
    if loads:
        from analysis.product_log import ICON, assess_loads, render_load_line
        for r in assess_loads(loads, readings, cfg.limit_c, t1, cfg.product_rules):
            ops.append(f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name))
    if ops:
        lines.append("Operación del periodo:")
        lines += [f"  • {o}" for o in ops]
    lines.append("Nota: es la temperatura del aire de la cámara (termocupla), no la del producto.")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Formato CONCISO (por defecto): conclusión + dato clave + acción.
# La versión larga queda disponible con detail=True ("detalle" en el chat).
# ----------------------------------------------------------------------

def causa_corta(exc: Excursion, cfg: CoolingConfig) -> str:
    k = exc.kind or classify(exc, cfg)
    duty = f"{exc.refr_duty:.0%}" if exc.refr_duty is not None else None
    f = _Fmt(cfg.tz_name)
    if k == "deshielo":
        return "deshielo normal"
    if k == "deshielo_largo":
        return "deshielo que se prolongó"
    if k == "puerta":
        return f"puerta abierta {_dur(exc.door_min)}"
    if k == "carga_con_puerta":
        return "puerta abierta y recuperación lenta (posible ingreso de producto)"
    if k == "carga_termica":
        return f"carga térmica alta (compresor al {duty})" if duty else "carga térmica alta"
    if k == "capacidad":
        return f"posible pérdida de capacidad de enfriamiento (compresor al {duty})" if duty else \
               "posible pérdida de capacidad de enfriamiento"
    if k == "sin_frio":
        return f"el controlador pidió frío solo {duty}" if duty else "el controlador no pidió frío"
    if k == "corte_datos":
        return f"tramo sin lecturas de {_dur(exc.gap_min)}"
    if k == "lectura_espuria":
        return "lectura aislada (posible falso contacto)"
    if k == "en_curso":
        return f"aún incierta (compresor al {duty})" if duty else "aún incierta"
    if k == "ingreso_producto" and exc.load is not None:
        return f"ingreso de {exc.load.label()} a las {f.hm(exc.load.t_in)}"
    if k == "setpoint" and exc.sp_now is not None:
        t = f"setpoint en {_n(exc.sp_now)} °C"
        if exc.sp_change is not None:
            t += f" (cambió de {_n(exc.sp_change[1])} a {_n(exc.sp_change[2])} °C a las {f.hm(exc.sp_change[0])})"
        return t + ": la cámara regula sobre el límite por configuración"
    return "sin causa visible"


ACCION_CORTA = {
    "deshielo": None,
    "deshielo_largo": "Revisar el fin de deshielo y el hielo en el evaporador.",
    "puerta": "Verificar el cierre de la puerta y los tiempos de apertura.",
    "carga_con_puerta": "Si no hubo ingreso de producto, revisar el cierre de la puerta.",
    "carga_termica": "Revisar la temperatura de ingreso del producto y el calor ambiente.",
    "capacidad": "Revisar hielo en el evaporador, ventilador y refrigerante.",
    "sin_frio": "Revisar el setpoint y la configuración del controlador.",
    "corte_datos": "Revisar la alimentación eléctrica y la comunicación con SITRAD.",
    "lectura_espuria": "Revisar el cableado de la termocupla.",
    "sin_causa": "Inspeccionar la cava en sitio.",
    "en_curso": "Seguir la evolución.",
    "ingreso_producto": "Ingresar las cargas grandes por tandas.",
    "setpoint": "Confirmar que el setpoint es intencional.",
}


def _risk_lines(loads, temp_readings, cfg: CoolingConfig, now, only_level=None) -> list[str]:
    if not loads or not temp_readings:
        return []
    from analysis.product_log import ICON, assess_loads, render_load_line
    return [f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name)
            for r in assess_loads(loads, temp_readings, cfg.limit_c, now, cfg.product_rules)]


def case_from_excursion(e: Excursion, cfg: CoolingConfig, reported: bool, now=None) -> dict:
    """Registro del episodio tal como lo vio el bot (hipótesis + características), para medir su acierto."""
    f = _Fmt(cfg.tz_name)
    rec = {
        "id": "ex:" + e.start.isoformat(), "type": "episodio", "start": e.start.isoformat(),
        "end": (e.end or e.last_t).isoformat(), "recovered": e.recovered, "reported": reported,
        "bot_kind": e.kind or classify(e, cfg), "bot_cause": causa_corta(e, cfg), "limit_c": cfg.limit_c,
        "features": {
            "peak_c": round(e.peak_c, 2), "prev_c": None if e.prev_c is None else round(e.prev_c, 2),
            "rise_c": None if e.rise_c is None else round(e.rise_c, 2), "above_min": round(e.above_min, 1),
            "recovery_min": None if e.recovery_min is None else round(e.recovery_min, 1),
            "refr_duty": None if e.refr_duty is None else round(e.refr_duty, 2),
            "door_min": round(e.door_min, 1), "defrost": bool(e.defrost), "gap_min": round(e.gap_min, 1),
            "n_above": e.n_above, "sp_now": e.sp_now, "sp_changed": e.sp_change is not None,
            "load": e.load.label() if e.load is not None else None,
            "hour_local": e.start.astimezone(f.tz).hour, "weekday": e.start.astimezone(f.tz).weekday()},
    }
    if reported and now is not None:
        rec["reported_at"] = now.isoformat()
    return rec


def render_alert(exc: Excursion, cfg: CoolingConfig, risk_lines: list[str] | None = None,
                 detail: bool = False) -> str:
    if detail:
        return _render_alert_detail(exc, cfg)
    f = _Fmt(cfg.tz_name)
    name = cfg.camera_name[:1].upper() + cfg.camera_name[1:]
    out = [f"🟠 {name} sobre {_n(cfg.limit_c, 0)} °C hace {_dur(exc.above_min)}",
           f"Máx. {_n(exc.peak_c)} °C (desde las {f.hm(exc.start)}). Causa probable: {causa_corta(exc, cfg)}."]
    out += (risk_lines or [])[:2]
    return "\n".join(out)


def render_episode_report(exc: Excursion, history: list[Excursion], cfg: CoolingConfig,
                          temp_readings=None, detail: bool = False) -> str:
    if detail:
        return _render_episode_detail(exc, history, cfg, temp_readings)
    f = _Fmt(cfg.tz_name)
    L = cfg.limit_c
    end = exc.end or exc.last_t
    out = [f"INFORME — {cfg.camera_name}, {f.hm(exc.start)}–{f.hm(end)}"]
    rng = f"{_n(exc.prev_c)} → {_n(exc.peak_c)}" if exc.prev_c is not None else f"máx. {_n(exc.peak_c)}"
    if exc.recovered:
        ok = cfg.max_recovery_min is None or exc.above_min <= cfg.max_recovery_min
        line = (f"Aire de la cámara: {rng} °C; {_dur(exc.above_min)} sobre {_n(L, 0)} °C, "
                f"volvió a ≤{_n(L, 0)} °C a las {f.hm(exc.end)} " + ("✅" if ok else "❌ (excede el plazo permitido)"))
        usual = _usual(history, cfg, exc)
        if usual and usual[1] > 0:
            diff = (exc.above_min / usual[1] - 1.0) * 100.0
            if abs(diff) >= 50:
                line += f" — {abs(diff):.0f}% {'más lenta' if diff > 0 else 'más rápida'} que lo habitual"
        out.append(line)
    else:
        out.append(f"Aire de la cámara: {rng} °C; sigue sobre {_n(L, 0)} °C desde hace {_dur(exc.above_min)} ⏳")
    out.append(f"Causa probable: {causa_corta(exc, cfg)}.")
    if exc.load is not None:
        out.append(f"Desde el ingreso de {exc.load.label()} ({f.hm(exc.load.t_in)}): "
                   f"{_dur((end - exc.load.t_in).total_seconds() / 60.0)} "
                   + (f"hasta volver a {_n(L, 0)} °C." if exc.recovered else "y aún no vuelve al límite."))
        out += _risk_lines([exc.load], temp_readings, cfg, end)
    if exc.gap_min >= 10.0 and (exc.kind or classify(exc, cfg)) != "corte_datos":
        out.append(f"Ojo: {_dur(exc.gap_min)} sin lecturas durante el episodio.")
    acc = ACCION_CORTA.get(exc.kind or classify(exc, cfg))
    if acc:
        out.append(acc)
    return "\n".join(out)


def render_period_report(readings, t0: datetime, t1: datetime, cfg: CoolingConfig,
                         door=None, defrost=None, refr=None, setpoint=None, loads=None,
                         detail: bool = False) -> str:
    if detail:
        return _render_period_detail(readings, t0, t1, cfg, door, defrost, refr, setpoint, loads)
    from analysis.forecast import despike
    f = _Fmt(cfg.tz_name)
    all_rs = sorted((r for r in _good(readings) if t0 <= r.event_time <= t1), key=lambda r: r.event_time)
    rs = despike(all_rs)
    spikes = len(all_rs) - len(rs)
    if len(rs) < 2:
        return f"INFORME — {cfg.camera_name}\nNo hay lecturas suficientes de la termocupla en este periodo."
    L = cfg.limit_c
    tot = ok = 0.0
    for a, b in zip(rs, rs[1:]):
        dt = min((b.event_time - a.event_time).total_seconds() / 60.0, 15.0)
        tot += dt
        if float(a.value) <= L:
            ok += dt
    vals = [float(r.value) for r in rs]
    exs = [e for e in find_excursions(readings, cfg, door, defrost, refr, loads, setpoint)
           if t0 <= e.start <= t1 and e.kind != "lectura_espuria"]
    hrs = (rs[-1].event_time - rs[0].event_time).total_seconds() / 3600.0
    out = [f"INFORME {cfg.camera_name} · {f.hm(rs[0].event_time)}–{f.hm(rs[-1].event_time)} ({_n(hrs)} h)"]
    head = f"Aire ≤{_n(L, 0)} °C el {100.0 * ok / tot if tot else 0:.0f}% del tiempo · máx. {_n(max(vals))} °C"
    if exs:
        head += f" · {len(exs)} episodio(s), {_dur(sum(e.above_min for e in exs))} sobre el límite"
    else:
        head += " · sin episodios sobre el límite ✅"
    out.append(head)
    for e in exs[:4]:
        est = "" if e.recovered else " (SIGUE)"
        out.append(f"• {f.hm(e.start)} {_dur(e.above_min)}{est} — {causa_corta(e, cfg)}")
    if len(exs) > 4:
        out.append(f"• … y {len(exs) - 4} más")
    ops = []
    if refr:
        d = _state_fraction(refr, t0, t1)
        if d is not None:
            ops.append(f"compresor {d:.0%}")
    if door:
        n, tot_m, _long = door_activity(door, t0, t1)
        ops.append(f"puerta {n} apertura(s)" + (f" ({_dur(tot_m)})" if n else ""))
    gaps = data_gaps(readings, t0, t1)
    if gaps:
        ops.append(f"{len(gaps)} corte(s) de datos ({_dur(sum(g[2] for g in gaps))})")
    if spikes:
        ops.append(f"{spikes} lectura(s) aislada(s) descartada(s)")
    if setpoint:
        for tt, a0, b0 in setpoint_changes(setpoint, t0, t1):
            ops.append(f"setpoint {_n(a0)}→{_n(b0)} °C a las {f.hm(tt)}")
    if ops:
        out.append(" · ".join(ops))
    bad = [e for e in exs if (e.kind or classify(e, cfg)) in ATENCION]
    if bad:
        acc = ACCION_CORTA.get(bad[-1].kind or classify(bad[-1], cfg))
        out.append("Atención: " + ", ".join(sorted({LABEL[e.kind] for e in bad})) + (f". {acc}" if acc else "."))
    out += _risk_lines(loads, readings, cfg, t1)
    return "\n".join(out)


# ----------------------------------------------------------------------
# Monitor en vivo (qué mensajes enviar en cada ciclo)
# ----------------------------------------------------------------------

@dataclass
class CoolingMonitor:
    cfg: CoolingConfig
    stale_min: float = 10.0
    _stale_since: datetime | None = None
    _watch: object = None
    on_case: object = None                                   # callback(dict): registra casos en el libro
    _sent: set = field(default_factory=set)                  # episodios de los que SÍ se envió mensaje
    _case_state: dict = field(default_factory=dict)
    _seen: dict = field(default_factory=dict)                # clave -> (inicio, fin) de episodios ya vistos
    _alerted: set = field(default_factory=set)
    _reported: set = field(default_factory=set)
    _primed: bool = False
    _closed_pending: list = field(default_factory=list)      # episodios cerrados aún sin entregar
    _closed_seen: set = field(default_factory=set)

    def drain_closed(self) -> list:
        """Episodios que se cerraron (recuperados) desde la última llamada; cada uno se entrega una sola vez."""
        out, self._closed_pending = self._closed_pending, []
        return out

    def step(self, temp_readings, door=None, defrost=None, refr=None,
             now: datetime | None = None, loads=None, loads_all=None,
             setpoint=None) -> list[tuple[str, str]]:
        """Devuelve [(tipo, texto)]: 'alerta' | 'informe' | 'alerta_temprana' | 'riesgo_producto'."""
        out_stale = self._check_stale(temp_readings, now)
        if now is not None and not out_stale:
            if self._watch is None:
                from analysis.rise_watch import RiseWatch
                self._watch = RiseWatch(self.cfg)
            out_stale = out_stale + self._watch.step(temp_readings, door, defrost, refr, loads, now, setpoint)
        exs = self._drop_remnants(find_excursions(temp_readings, self.cfg, door, defrost, refr, loads_all, setpoint))
        out: list[tuple[str, str]] = list(out_stale)
        for e in exs:
            if e.recovered and e.key not in self._closed_seen:
                self._closed_seen.add(e.key)
                self._closed_pending.append(e)
        if not self._primed:
            # Al arrancar no se informa el pasado:
            # recuperados y en curso se marcan como vistos.
            for e in exs:
                self._alerted.add(e.key)
                if e.recovered:
                    self._reported.add(e.key)
            self._primed = True
            return list(out_stale)
        for e in exs:
            routine = ((defrost_explains(e, self.cfg) and not self.cfg.notify_defrost)
                       or e.kind == "lectura_espuria")        # ruido del sensor: va al informe de periodo
            if routine:
                if e.recovered:
                    self._reported.add(e.key)
                    self._alerted.add(e.key)
                continue
            if not e.recovered:
                still_above = e.last_c is not None and e.last_c > self.cfg.limit_c
                if still_above and e.above_min >= self.cfg.alert_after_min and e.key not in self._alerted:
                    self._alerted.add(e.key)
                    out.append(("alerta", render_alert(e, self.cfg, _risk_lines(loads, temp_readings, self.cfg, now) if now else None)))
                    self._sent.add(e.key)
            elif e.key not in self._reported:
                self._reported.add(e.key)
                self._alerted.add(e.key)
                out.append(("informe", render_episode_report(e, exs, self.cfg, temp_readings)))
                self._sent.add(e.key)
        self._record_cases(exs, now)
        return out

    def _drop_remnants(self, exs):
        """
        El almacén solo guarda 24 h: cuando un episodio cruza ese borde, cada ciclo se ve "recortado"
        (inicio un poco más tarde, otra clave) y parecería un episodio nuevo. Un episodio cuyo inicio cae
        DENTRO de otro ya visto es el mismo recortado: se descarta para no informarlo de nuevo.
        """
        keep = []
        for e in exs:
            a, b = e.start, (e.end or e.last_t)
            if e.key in self._seen:
                self._seen[e.key] = (a, b)
            elif any(k != e.key and s0 <= a <= e0 for k, (s0, e0) in self._seen.items()):
                continue
            else:
                self._seen[e.key] = (a, b)
            keep.append(e)
        if len(self._seen) > 400:                       # no crece sin límite
            for k in sorted(self._seen, key=lambda k: self._seen[k][1])[:100]:
                self._seen.pop(k, None)
        return keep

    def _record_cases(self, exs, now) -> None:
        """Entrega al libro de casos lo que el bot propuso (episodios y avisos) para medir su acierto."""
        if self.on_case is None:
            return
        try:
            for e in exs:
                state = (e.recovered, e.key in self._sent, e.kind)
                if self._case_state.get(e.key) != state:
                    self._case_state[e.key] = state
                    self.on_case(case_from_excursion(e, self.cfg, e.key in self._sent, now))
            if self._watch is not None:
                for rec in self._watch.drain_cases():
                    self.on_case(rec)
        except Exception:
            pass

    def set_cfg(self, cfg: CoolingConfig) -> None:
        """Actualiza la configuración (p. ej. el límite aplicable cambia al entrar otro producto)."""
        if cfg.limit_c != self.cfg.limit_c:
            self._primed = False            # se vuelve a "sembrar": no se re-informa el pasado
            self._alerted.clear()
            self._reported.clear()
            self._seen.clear()
        self.cfg = cfg
        if self._watch is not None:
            self._watch.cfg = cfg

    def _check_stale(self, temp_readings, now):
        """Avisa cuando la termocupla deja de reportar y cuando vuelve."""
        good = _good(temp_readings)
        if not good or now is None:
            return []
        f = _Fmt(self.cfg.tz_name)
        last = max(r.event_time for r in good)
        gap = (now - last).total_seconds() / 60.0
        if gap >= self.stale_min and self._stale_since is None:
            self._stale_since = last
            return [("alerta",
                     f"🟠 Sin lecturas de la termocupla desde las {f.hm(last)} (hace {_dur(gap)}). "
                     "Posible corte de energía o de comunicación.")]
        if gap < self.stale_min and self._stale_since is not None:
            since, self._stale_since = self._stale_since, None
            return [("informe",
                     f"✅ Lecturas reanudadas tras ≈{_dur((last - since).total_seconds() / 60.0)} "
                     f"(desde las {f.hm(since)}). Temperatura: {_n(float(good[-1].value))} °C.")]
        return []
