"""core/retention.py — limpieza programada de datos para no llenar el disco.

Se ejecuta una vez al día a la hora local configurada (por defecto 03:00) y,
además, una vez al arrancar si la última ejecución quedó atrás. Lo que hace:

  * Archivos JSONL que solo crecen (eventos, informes, casos, episodios,
    conversaciones, alarmas...): conserva las líneas más nuevas que N días.
    Las líneas sin fecha reconocible se conservan.
  * Archivos rotados por día (observations.YYYYMMDD.jsonl,
    lecturas.YYYYMMDD.jsonl): se borran los de días viejos.
  * Carpetas de salida (gráficos PNG, exportaciones CSV): se borran por antigüedad.
  * Tope opcional de tamaño total: si la carpeta de logs supera `max_total_mb`,
    se borran primero los archivos rotados más viejos.

Concurrencia: cada objetivo declara quién lo escribe. Los que escribe el hilo
del agente (conversaciones, feedback, gráficos, exportaciones) se limpian en
ese hilo; el resto, en el ciclo principal. Así ninguna limpieza pisa una
escritura en curso.

Config (storage.retention), todos opcionales:
    {"enabled": true, "run_at_hour_local": 3, "max_total_mb": 0,
     "days": {"observations": 7, "raw_readings": 3, "events": 90, ...},
     "extra": [{"path": "logs/otro.jsonl", "days": 30}]}
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from core.clock import local_tz

logger = logging.getLogger(__name__)

# Campos de fecha que usan los distintos logs del proyecto, por prioridad.
_TS_KEYS = ("event_time", "ts", "saved_at", "recorded_at", "collectedAt",
            "fin", "end", "inicio", "start", "when", "at", "opened_at")
_DAY_RE = re.compile(r"\.(\d{8})(?:_\d{6})?\.[A-Za-z0-9]+$")

DEFAULT_DAYS = {
    "observations": 7,       # observations.jsonl y sus rotados
    "raw_readings": 3,       # sitrad_data/lecturas.YYYYMMDD.jsonl (respuesta cruda de la API: pesa mucho)
    "events": 90,
    "reports": 30,
    "cases": 180,
    "cooling_cases": 180,
    "feedback": 365,
    "episodes": 365,
    "conversations": 30,
    "alarms": 180,
    "sim_truth": 7,
    "charts": 7,
    "exports": 30,
}


@dataclass
class Target:
    name: str
    kind: str          # "jsonl" | "daily" | "dir"
    path: str          # archivo (jsonl), archivo base (daily) o carpeta (dir)
    days: float
    owner: str = "main"  # "main" | "agent"


class RetentionManager:
    def __init__(self, targets: list[Target], tz_name: str = "America/Bogota",
                 run_at_hour_local: int = 3, max_total_mb: float = 0.0,
                 total_dirs: tuple[str, ...] = (), enabled: bool = True,
                 state_path: str | None = None) -> None:
        self.targets = [t for t in targets if t.days and t.days > 0]
        self.tz = local_tz(tz_name)
        self.run_at_hour = int(run_at_hour_local) % 24
        self.max_total_mb = float(max_total_mb or 0.0)
        self.total_dirs = tuple(d for d in total_dirs if d)
        self.enabled = bool(enabled)
        self.state_path = state_path
        self._last_run_day: str | None = self._load_state()

    # ------------------------------------------------------------------
    # Construcción desde config
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict, paths: dict[str, str], tz_name: str,
                    observations_keep_days: int | None = None) -> "RetentionManager":
        """`paths`: nombre -> ruta (los mismos nombres que DEFAULT_DAYS)."""
        cfg = dict(cfg or {})
        days = {**DEFAULT_DAYS, **(cfg.get("days") or {})}
        if observations_keep_days and "observations" not in (cfg.get("days") or {}):
            days["observations"] = observations_keep_days
        agent_owned = {"conversations", "feedback", "charts", "exports"}
        targets: list[Target] = []
        for name, path in paths.items():
            if not path:
                continue
            if name in ("charts", "exports"):
                kind = "dir"
            elif name in ("observations", "raw_readings"):
                kind = "daily"
            else:
                kind = "jsonl"
            targets.append(Target(name, kind, path, float(days.get(name, 0) or 0),
                                  "agent" if name in agent_owned else "main"))
        for extra in cfg.get("extra") or []:
            if isinstance(extra, dict) and extra.get("path"):
                targets.append(Target(str(extra.get("name") or extra["path"]),
                                      str(extra.get("kind", "jsonl")), str(extra["path"]),
                                      float(extra.get("days", 30)), str(extra.get("owner", "main"))))
        dirs = sorted({os.path.dirname(t.path) if t.kind != "dir" else t.path for t in targets} - {""})
        state = os.path.join(dirs[0], ".retention_state.json") if dirs else None
        return cls(targets, tz_name=tz_name,
                   run_at_hour_local=int(cfg.get("run_at_hour_local", 3)),
                   max_total_mb=float(cfg.get("max_total_mb", 0) or 0),
                   total_dirs=tuple(dirs), enabled=bool(cfg.get("enabled", True)),
                   state_path=state)

    # ------------------------------------------------------------------
    # Programación
    # ------------------------------------------------------------------

    def _load_state(self) -> str | None:
        if not self.state_path:
            return None
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh).get("last_run_day")
        except (OSError, ValueError):
            return None

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"last_run_day": self._last_run_day}, fh)
        except OSError:
            pass

    def due(self, now: datetime) -> bool:
        if not self.enabled or not self.targets:
            return False
        local = now.astimezone(self.tz)
        today = local.strftime("%Y%m%d")
        if self._last_run_day == today:
            return False
        # Primera vez (o se perdió la hora programada): corre ya; si no, a la hora fijada.
        if self._last_run_day is None:
            return True
        yesterday = (local - timedelta(days=1)).strftime("%Y%m%d")
        if self._last_run_day < yesterday:
            return True
        return local.hour >= self.run_at_hour

    def maybe_run(self, now: datetime, submit: Callable | None = None) -> bool:
        """Corre si toca. Los objetivos del hilo del agente se encolan con `submit` (si se da)."""
        if not self.due(now):
            return False
        self._last_run_day = now.astimezone(self.tz).strftime("%Y%m%d")
        self._save_state()
        main = [t for t in self.targets if t.owner != "agent" or submit is None]
        agent = [t for t in self.targets if t.owner == "agent" and submit is not None]
        self.run(now, main)
        if agent:
            submit(lambda: self.run(now, agent), "limpieza de datos")
        return True

    # ------------------------------------------------------------------
    # Limpieza
    # ------------------------------------------------------------------

    def run(self, now: datetime, targets: list[Target] | None = None) -> dict[str, int]:
        """Limpia ahora. Devuelve {objetivo: elementos borrados (líneas o archivos)}."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        report: dict[str, int] = {}
        for t in (self.targets if targets is None else targets):
            cutoff = now - timedelta(days=t.days)
            try:
                if t.kind == "jsonl":
                    n = self.trim_jsonl(t.path, cutoff)
                elif t.kind == "daily":
                    n = self.prune_daily(t.path, cutoff) + self.trim_jsonl(t.path, cutoff)
                elif t.kind == "dir":
                    n = self.prune_dir(t.path, cutoff)
                else:
                    n = 0
            except Exception:
                logger.exception("Limpieza de %s falló.", t.path)
                n = 0
            if n:
                report[t.name] = n
        if targets is None or any(t.owner != "agent" for t in targets):
            n = self.enforce_total_size()
            if n:
                report["tope_total"] = n
        if report:
            logger.info("Limpieza programada: %s", report)
        return report

    @staticmethod
    def _line_time(line: str) -> datetime | None:
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        if not isinstance(rec, dict):
            return None
        for k in _TS_KEYS:
            v = rec.get(k)
            if isinstance(v, str) and len(v) >= 10:
                try:
                    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
                except ValueError:
                    continue
                return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        return None

    @classmethod
    def trim_jsonl(cls, path: str, cutoff: datetime) -> int:
        """Quita las líneas con fecha < cutoff. Reescribe de forma atómica. Devuelve líneas quitadas."""
        if not os.path.isfile(path):
            return 0
        # Lectura rápida: si la primera línea con fecha ya es >= cutoff, no hay nada que hacer
        # (los logs se escriben en orden de tiempo).
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                t = cls._line_time(line)
                if t is not None:
                    if t >= cutoff:
                        return 0
                    break
            else:
                return 0
        removed = 0
        tmp = path + ".retention.tmp"
        with open(path, "r", encoding="utf-8", errors="replace") as src, \
                open(tmp, "w", encoding="utf-8", newline="\n") as dst:
            for line in src:
                if not line.strip():
                    continue
                t = cls._line_time(line)
                if t is not None and t < cutoff:
                    removed += 1
                    continue
                dst.write(line if line.endswith("\n") else line + "\n")
        if removed:
            os.replace(tmp, path)
        else:
            os.remove(tmp)
        return removed

    def prune_daily(self, base_path: str, cutoff: datetime) -> int:
        """Borra base.YYYYMMDD.ext (y base.YYYYMMDD_HHMMSS.ext) de días anteriores al corte."""
        directory = os.path.dirname(base_path) or "."
        stem, ext = os.path.splitext(os.path.basename(base_path))
        cutoff_day = cutoff.astimezone(self.tz).strftime("%Y%m%d")
        n = 0
        try:
            names = os.listdir(directory)
        except OSError:
            return 0
        for name in names:
            if not (name.startswith(stem + ".") and name.endswith(ext)):
                continue
            m = _DAY_RE.search(name)
            if m and m.group(1) < cutoff_day:
                try:
                    os.remove(os.path.join(directory, name))
                    n += 1
                except OSError:
                    pass
        return n

    @staticmethod
    def prune_dir(directory: str, cutoff: datetime) -> int:
        """Borra los archivos de la carpeta modificados antes del corte."""
        if not os.path.isdir(directory):
            return 0
        limit = cutoff.timestamp()
        n = 0
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            try:
                if os.path.isfile(full) and os.path.getmtime(full) < limit:
                    os.remove(full)
                    n += 1
            except OSError:
                pass
        return n

    def enforce_total_size(self) -> int:
        """Si las carpetas superan max_total_mb, borra archivos rotados (fechados) del más viejo al más nuevo."""
        if self.max_total_mb <= 0 or not self.total_dirs:
            return 0
        files: list[tuple[str, int, str]] = []
        total = 0
        for d in self.total_dirs:
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                full = os.path.join(d, name)
                if not os.path.isfile(full):
                    continue
                size = os.path.getsize(full)
                total += size
                m = _DAY_RE.search(name)
                if m:
                    files.append((m.group(1), size, full))
        limit = self.max_total_mb * 1024 * 1024
        n = 0
        for _, size, full in sorted(files):
            if total <= limit:
                break
            try:
                os.remove(full)
                total -= size
                n += 1
            except OSError:
                pass
        if total > limit:
            logger.warning("Los datos ocupan %.0f MB (> %.0f MB) aunque ya no quedan archivos rotados "
                           "que borrar: revisa los días de retención.", total / 1048576, self.max_total_mb)
        return n
