"""Clientes LLM: Ollama (local) con fallback determinista por plantillas.

Cambios implementados:
    - Revisa done_reason == "length" (JSON cortado).
    - Ignora el campo 'thinking' o etiquetas <think> al parsear.
    - Limita num_thread para no asfixiar a Sitrad.
    - Baja prioridad del proceso (best-effort).
    - Auditoría de cada interacción con el modelo.
    - Tool use: generate_with_tools() con bucle de ejecución de
      herramientas y round-trip contra /api/chat de Ollama.
"""

from __future__ import annotations
# agent.llm_client
import hashlib
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger(__name__)


# ============================================================================
# BASE
# ============================================================================

class BaseLLM(ABC):
    name = "base"

    @abstractmethod
    def generate(
        self,
        system: str,
        user: str,
        schema: dict,
    ) -> Optional[dict]:
        raise NotImplementedError

    def generate_text(
        self,
        system: str,
        user: str,
    ) -> Optional[str]:
        """Genera texto libre. Opcional: los backends que no lo
        soporten deben lanzar NotImplementedError."""
        raise NotImplementedError(
            "Este backend no soporta generate_text()."
        )

    def generate_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        tool_executor,
        max_tool_rounds: int = 6,
    ) -> Optional[str]:
        """Genera una respuesta usando tool use.

        El backend debe:
          1. Enviar el prompt y las definiciones de herramientas al
             modelo.
          2. Si el modelo pide una herramienta, ejecutarla mediante
             'tool_executor(name, arguments)'.
          3. Devolver el resultado al modelo y repetir.
          4. Devolver la respuesta final en texto cuando el modelo
             deje de pedir herramientas.

        'tool_executor' debe aceptar (name: str, arguments: dict) y
        devolver un dict serializable a JSON.
        """
        raise NotImplementedError(
            "Este backend no soporta tool use."
        )


# ============================================================================
# UTILIDADES
# ============================================================================

_THINK_TAG_RE = re.compile(r"<think[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(content: str) -> str:
    if not content:
        return ""
    return _THINK_TAG_RE.sub("", content).strip()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _best_effort_lower_priority() -> None:
    """Baja la prioridad del proceso si el SO lo permite (best-effort)."""
    try:
        if hasattr(os, "nice"):
            os.nice(5)
    except Exception:
        pass

    try:
        import psutil  # type: ignore
        p = psutil.Process(os.getpid())
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


# ============================================================================
# OLLAMA
# ============================================================================

class OllamaLLM(BaseLLM):
    """Cliente local para Ollama.

    Cambios:
        - num_thread configurable.
        - done_reason == 'length' se registra como truncamiento.
        - Se elimina <think>...</think> antes de parsear JSON.
        - Auditoría vía callback opcional.
        - generate_with_tools() con bucle de tool use.
    """

    name = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://localhost:11434",
        timeout_seconds: int = 60,
        num_ctx: int = 8192,
        num_predict: int = 1024,
        num_thread: Optional[int] = None,
        keep_alive: str = "10m",
        think: Optional[bool] = False,
        circuit_failures: int = 3,
        circuit_open_seconds: int = 300,
        audit_callback=None,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout_seconds = int(timeout_seconds)
        self.num_ctx = int(num_ctx)
        self.num_predict = int(num_predict)
        self.num_thread = int(num_thread) if num_thread else None
        self.keep_alive = keep_alive
        self.think = think
        self.circuit_failures = max(1, int(circuit_failures))
        self.circuit_open_seconds = max(1, int(circuit_open_seconds))
        self.audit_callback = audit_callback

        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._startup_checked = False
        self._available = False

    # ------------------------------------------------------------------ #

    def _circuit_open(self) -> bool:
        if self._circuit_open_until <= 0:
            return False

        now = time.monotonic()
        if now >= self._circuit_open_until:
            logger.info("Circuit breaker Ollama cerrado.")
            self._circuit_open_until = 0.0
            self._failure_count = 0
            return False

        remaining = int(self._circuit_open_until - now)
        logger.warning(
            "Ollama deshabilitado por circuit breaker. Faltan %ds.",
            max(0, remaining),
        )
        return True

    def _register_failure(self, reason: str) -> None:
        self._failure_count += 1
        logger.warning(
            "Fallo Ollama (%d/%d): %s",
            self._failure_count, self.circuit_failures, reason,
        )
        if self._failure_count >= self.circuit_failures:
            self._circuit_open_until = (
                time.monotonic() + self.circuit_open_seconds
            )
            logger.error(
                "Circuit breaker Ollama ABIERTO durante %ds.",
                self.circuit_open_seconds,
            )

    def _register_success(self) -> None:
        if self._failure_count:
            logger.info(
                "Ollama recuperado tras %d fallo(s).",
                self._failure_count,
            )
        self._failure_count = 0
        self._circuit_open_until = 0.0

    # ------------------------------------------------------------------ #

    def check_startup(self) -> bool:
        if self._circuit_open():
            return False

        try:
            response = requests.get(
                f"{self.host}/api/tags",
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            models = data.get("models", [])
            installed = {
                m.get("name") for m in models if isinstance(m, dict)
            }

            if self.model not in installed:
                logger.error(
                    "Modelo '%s' NO instalado. Disponibles: %s",
                    self.model,
                    sorted(m for m in installed if m),
                )
                self._available = False
                self._register_failure(f"modelo no instalado: {self.model}")
                self._startup_checked = True
                return False

            logger.info("Ollama OK. Modelo '%s' presente.", self.model)
            self._available = True
            self._startup_checked = True
            self._register_success()
            return True

        except requests.exceptions.RequestException as e:
            self._available = False
            self._startup_checked = True
            self._register_failure(f"Ollama no responde: {e}")
            return False
        except (ValueError, json.JSONDecodeError) as e:
            self._available = False
            self._startup_checked = True
            self._register_failure(f"/api/tags inválido: {e}")
            return False

    def _ensure_startup(self) -> bool:
        if not self._startup_checked:
            return self.check_startup()
        return self._available

    # ------------------------------------------------------------------ #

    def generate(
        self,
        system: str,
        user: str,
        schema: dict,
    ) -> Optional[dict]:

        if self._circuit_open():
            return None

        if not self._ensure_startup():
            return None

        _best_effort_lower_priority()

        options: Dict[str, Any] = {
            "temperature": 0.2,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }
        if self.num_thread is not None:
            options["num_thread"] = self.num_thread

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think

        started = time.monotonic()
        try:
            response = requests.post(
                f"{self.host}/api/chat",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            self._available = False
            self._register_failure(f"Ollama no responde: {e}")
            self._audit(
                system, user, None, None, started, data_meta={},
                error=str(e),
            )
            return None
        except (json.JSONDecodeError, ValueError) as e:
            self._register_failure(f"JSON inválido de Ollama: {e}")
            self._audit(
                system, user, None, None, started, data_meta={},
                error=str(e),
            )
            return None

        content_raw = data.get("message", {}).get("content", "") or ""
        content = _strip_thinking(content_raw)

        if not content:
            self._register_failure("Ollama devolvió contenido vacío.")
            self._audit(
                system, user, None, data, started, data_meta=data,
            )
            return None

        # Revisar truncamiento
        done_reason = data.get("done_reason")
        truncated = done_reason == "length"

        if truncated:
            logger.warning(
                "Ollama: done_reason='length' (respuesta truncada). "
                "num_predict=%d podría ser insuficiente.",
                self.num_predict,
            )

        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            self._register_failure(f"JSON inválido en content: {e}")
            self._audit(
                system, user, None, data, started, data_meta=data,
                error=f"json_decode: {e}",
            )
            return None

        if not isinstance(result, dict):
            self._register_failure("Ollama no devolvió un objeto JSON.")
            self._audit(
                system, user, None, data, started, data_meta=data,
            )
            return None

        # Comparar prompt_eval_count con lo esperado (detección de truncamiento)
        prompt_eval_count = data.get("prompt_eval_count")
        data.get("eval_count")
        if prompt_eval_count is not None and prompt_eval_count < 10:
            logger.warning(
                "Ollama: prompt_eval_count muy bajo (%s). "
                "Posible truncamiento del contexto.",
                prompt_eval_count,
            )

        self._register_success()
        self._audit(
            system, user, result, data, started, data_meta=data,
        )
        return result

    # ------------------------------------------------------------------ #

    def generate_text(
        self,
        system: str,
        user: str,
    ) -> Optional[str]:
        """Genera texto libre (sin formato JSON).

        No envía 'format' al servidor.
        No intenta parsear la respuesta como JSON.
        Devuelve el texto tal cual o None.
        """

        if self._circuit_open():
            return None

        if not self._ensure_startup():
            return None

        _best_effort_lower_priority()

        options: Dict[str, Any] = {
            "temperature": 0.2,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }
        if self.num_thread is not None:
            options["num_thread"] = self.num_thread

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think

        try:
            response = requests.post(
                f"{self.host}/api/chat",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            self._register_failure(f"Ollama no responde: {e}")
            return None
        except (json.JSONDecodeError, ValueError) as e:
            self._register_failure(f"Respuesta inválida: {e}")
            return None

        content_raw = data.get("message", {}).get("content", "") or ""
        content = _strip_thinking(content_raw)

        if not content:
            self._register_failure("Ollama devolvió texto vacío.")
            return None

        if data.get("done_reason") == "length":
            logger.warning(
                "Ollama truncó la respuesta (num_predict=%d).",
                self.num_predict,
            )

        self._register_success()
        return content

    # ------------------------------------------------------------------ #
    # TOOL USE
    # ------------------------------------------------------------------ #

    def generate_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        tool_executor,
        max_tool_rounds: int = 6,
    ) -> Optional[str]:
        """Genera una respuesta usando tool use contra /api/chat.

        Ciclo:
            user  →  Ollama
                       ├── respuesta final      → devolver texto
                       └── tool_calls
                              │
                              ▼
                        ejecutar herramienta(s)
                              │
                              ▼
                        resultado JSON
                              │
                              ▼
                        volver a Ollama
                              │
                              └── ... hasta respuesta final
        """

        if self._circuit_open():
            return None

        if not self._ensure_startup():
            return None

        _best_effort_lower_priority()

        options: Dict[str, Any] = {
            "temperature": 0.2,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }

        if self.num_thread is not None:
            options["num_thread"] = self.num_thread

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": user,
            },
        ]

        started = time.monotonic()

        for _ in range(max_tool_rounds):

            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "keep_alive": self.keep_alive,
                "options": options,
            }

            if self.think is not None:
                payload["think"] = self.think

            try:

                response = requests.post(
                    f"{self.host}/api/chat",
                    json=payload,
                    timeout=self.timeout_seconds,
                )

                response.raise_for_status()

                data = response.json()

            except requests.exceptions.RequestException as exc:

                self._register_failure(
                    f"Ollama no responde: {exc}"
                )

                logger.exception(
                    "Error en Ollama tool use."
                )

                self._audit(
                    system,
                    user,
                    None,
                    None,
                    started,
                    data_meta={},
                    error=str(exc),
                )

                return None

            except (
                json.JSONDecodeError,
                ValueError,
            ) as exc:

                self._register_failure(
                    f"Respuesta inválida: {exc}"
                )

                self._audit(
                    system,
                    user,
                    None,
                    None,
                    started,
                    data_meta={},
                    error=str(exc),
                )

                return None

            message = data.get(
                "message",
                {},
            )

            if not isinstance(
                message,
                dict,
            ):
                self._register_failure(
                    "Mensaje inválido de Ollama."
                )
                return None

            tool_calls = message.get(
                "tool_calls",
                [],
            )

            # ------------------------------------------------------
            # No hay herramientas:
            # esta es la respuesta final.
            # ------------------------------------------------------

            if not tool_calls:

                content_raw = (
                    message.get(
                        "content",
                        "",
                    )
                    or ""
                )

                content = _strip_thinking(
                    content_raw
                )

                if not content:

                    self._register_failure(
                        "Ollama devolvió respuesta "
                        "vacía en tool use."
                    )

                    return None

                if data.get(
                    "done_reason"
                ) == "length":

                    logger.warning(
                        "Ollama truncó la respuesta "
                        "final de tool use."
                    )

                self._register_success()

                self._audit(
                    system,
                    user,
                    {
                        "text": content,
                    },
                    data,
                    started,
                    data_meta=data,
                )

                return content

            # ------------------------------------------------------
            # Añadir la respuesta del asistente al historial.
            # ------------------------------------------------------

            assistant_message = {
                "role": "assistant",
                "content": (
                    message.get(
                        "content",
                        "",
                    )
                    or ""
                ),
                "tool_calls": tool_calls,
            }

            messages.append(
                assistant_message
            )

            # ------------------------------------------------------
            # Ejecutar cada herramienta solicitada.
            # ------------------------------------------------------

            for tool_call in tool_calls:

                function = (
                    tool_call.get(
                        "function",
                        {}
                    )
                )

                name = function.get(
                    "name"
                )

                arguments = function.get(
                    "arguments",
                    {},
                )

                if isinstance(
                    arguments,
                    str,
                ):

                    try:
                        arguments = json.loads(
                            arguments
                        )

                    except json.JSONDecodeError:

                        result = {
                            "ok": False,
                            "error": (
                                "Argumentos JSON "
                                "inválidos."
                            ),
                        }

                        messages.append({
                            "role": "tool",
                            "content": json.dumps(
                                result,
                                ensure_ascii=False,
                            ),
                        })

                        continue

                if not isinstance(
                    arguments,
                    dict,
                ):

                    result = {
                        "ok": False,
                        "error": (
                            "Los argumentos de "
                            "la herramienta no son "
                            "un objeto JSON."
                        ),
                    }

                else:

                    logger.info(
                        "LLM solicita herramienta: "
                        "%s(%s)",
                        name,
                        arguments,
                    )

                    try:
                        result = tool_executor(
                            name,
                            arguments,
                        )

                    except Exception as exc:

                        logger.exception(
                            "Fallo ejecutando "
                            "herramienta '%s'.",
                            name,
                        )

                        result = {
                            "ok": False,
                            "error": str(exc),
                        }

                messages.append({
                    "role": "tool",
                    "content": json.dumps(
                        result,
                        ensure_ascii=False,
                    ),
                })

        logger.warning(
            "Ollama alcanzó max_tool_rounds=%d "
            "sin producir respuesta final.",
            max_tool_rounds,
        )

        return (
            "No fue posible completar la consulta "
            "dentro del número máximo de operaciones."
        )

    # ------------------------------------------------------------------ #

    def _audit(
        self,
        system: str,
        user: str,
        result: Optional[dict],
        data: Optional[dict],
        started: float,
        data_meta: Optional[dict] = None,
        error: str = "",
    ) -> None:
        if self.audit_callback is None:
            return

        try:
            latency_ms = int((time.monotonic() - started) * 1000)
            meta = data_meta or {}
            record = {
                "model": self.model,
                "latency_ms": latency_ms,
                "input_tokens": meta.get("prompt_eval_count"),
                "output_tokens": meta.get("eval_count"),
                "truncated": meta.get("done_reason") == "length",
                "prompt_hash": _hash_text(system + "\n" + user),
                "output_hash": _hash_text(
                    json.dumps(result, ensure_ascii=False)
                    if result else ""
                ),
                "redacted": {
                    "system_len": len(system),
                    "user_len": len(user),
                    "result_keys": list(result.keys()) if result else [],
                    "error": error,
                },
            }
            self.audit_callback(record)
        except Exception:
            logger.exception("Auditoría LLM falló.")


# ============================================================================
# TEMPLATE FALLBACK
# ============================================================================

class TemplateLLM(BaseLLM):
    """Fallback determinista sin modelo."""

    name = "template"

    def generate(
        self,
        system: str,
        user: str,
        schema: dict,
    ) -> Optional[dict]:

        try:
            pkg = json.loads(user)
        except (TypeError, json.JSONDecodeError):
            logger.warning("TemplateLLM recibió JSON inválido.")
            return None

        # Algunos llamadores meten el few-shot como texto plano antes del JSON.
        # Intentamos extraer el último bloque JSON válido.
        if not isinstance(pkg, dict):
            return None

        signals = pkg.get("signals") or []
        if not signals:
            return {
                "relevante": False,
                "severidad": "INFO",
                "titulo": "",
                "mensaje": "",
                "causas_probables": [],
                "accion_sugerida": "",
                "minutos_para_actuar": 0,
            }

        equipo = pkg.get("equipo") or {}
        dev_name = equipo.get("nombre") or f"Equipo {equipo.get('id', '?')}"

        ordered = sorted(
            signals,
            key=lambda s: float(s.get("interest", 0) or 0),
            reverse=True,
        )
        top = ordered[0]
        sev = _severity_from(top)

        descriptions = [
            _describe_signal(signal=s, equipo=dev_name)
            for s in ordered
        ]

        content = top.get("content") or top.get("metrics", {}).get("content")
        consequence = top.get("consequence") or top.get("metrics", {}).get("consequence")
        product = top.get("product") or top.get("metrics", {}).get("product")
        sensor = top.get("sensor", "sensor desconocido")
        detail = top.get("detail", "")

        if content:
            titulo = f"{dev_name}: {sensor} — {content}"
        else:
            titulo = f"{dev_name}: {sensor}"
        if detail:
            titulo += f" — {detail}"

        lines = [f"{dev_name}", ""]
        if product:
            lines.append(f"Producto: {product}")
        if content:
            lines.append(f"Contenido: {content}")
        lines.extend([f"Sensor: {sensor}", "", "Hallazgos:"])
        for description in descriptions:
            lines.append(f"• {description}")

        if consequence:
            lines.extend(["", f"Consecuencia: {consequence}"])

        deadline_at = top.get("deadline_at") or top.get("metrics", {}).get("deadline_at")
        if deadline_at:
            lines.extend(["", f"Hora límite: {deadline_at}"])

        action = (
            top.get("action")
            or top.get("metrics", {}).get("action")
            or "Revisar la condición indicada."
        )
        lines.extend(["", f"Acción: {action}"])

        minutes = _minutes_to_act(top)
        if minutes is not None:
            lines.extend(["", f"Tiempo para actuar: {minutes} min."])

        return {
            "relevante": True,
            "severidad": sev,
            "titulo": titulo,
            "mensaje": "\n".join(lines),
            "causas_probables": _causes_from_signals(ordered),
            "accion_sugerida": action,
            "minutos_para_actuar": int(minutes) if minutes is not None else 0,
        }

    def generate_text(
        self,
        system: str,
        user: str,
    ) -> Optional[str]:
        """Fallback de texto libre: simplemente no está soportado.

        El sistema debería usar Ollama para informes interpretativos.
        """
        return None

    def generate_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        tool_executor,
        max_tool_rounds: int = 6,
    ) -> Optional[str]:
        """TemplateLLM no soporta tool use.

        Se declara explícitamente para que CavaAgent pueda detectar
        que este backend no es válido para answer_question().
        """
        raise NotImplementedError(
            "TemplateLLM no soporta tool use. "
            "Configure provider: ollama para usar herramientas."
        )


# ============================================================================
# HELPERS
# ============================================================================

def _describe_signal(signal: dict, equipo: str) -> str:
    kind = signal.get("kind", "UNKNOWN")
    sensor = signal.get("sensor", "sensor desconocido")
    detail = signal.get("detail", "")
    metrics = signal.get("metrics") or {}
    unit = signal.get("unit", "")
    current = metrics.get("current_value")

    if current is not None:
        value_text = f"{current}{unit}"
    else:
        value_text = "sin valor actual"

    if kind in ("RISING_TREND", "FALLING_TREND"):
        slope = metrics.get("slope_c_per_h") or metrics.get("slope_per_hour")
        text = f"{sensor}: tendencia, valor actual {value_text}"
        if slope is not None:
            text += f", pendiente {slope}{unit}/h"
        eta = metrics.get("eta_minutes") or metrics.get("damage_in_minutes")
        if eta is not None:
            text += f", ETA {eta} min"
    elif kind == "EXPOSURE":
        content = signal.get("content") or metrics.get("content") or "contenido no especificado"
        minutes = metrics.get("minutes_over")
        limit = metrics.get("limit") or metrics.get("temp_max") or metrics.get("max_value")
        text = f"{sensor}: exposición de {content}"
        if current is not None:
            text += f", valor {value_text}"
        if limit is not None:
            text += f", límite {limit}{unit}"
        if minutes is not None:
            text += f", {minutes} min sobre el límite"
    elif kind == "BASELINE_DRIFT":
        z = metrics.get("z_score")
        text = f"{sensor}: desviación respecto a línea base, valor {value_text}"
        if z is not None:
            text += f", z-score {z}"
    else:
        text = f"{sensor}: {kind}"
        if current is not None:
            text += f", valor {value_text}"

    if detail:
        text += f" ({detail})"
    return text


def _minutes_to_act(signal: dict) -> Optional[float]:
    metrics = signal.get("metrics") or {}
    value = (
        signal.get("minutes_to_act")
        if signal.get("minutes_to_act") is not None
        else metrics.get("minutes_to_act")
    )
    if value is None:
        value = metrics.get("eta_minutes")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _causes_from_signals(signals: list) -> list:
    causes = []
    for signal in signals:
        signal_causes = (
            signal.get("causes_probables")
            or signal.get("possible_causes")
            or signal.get("metrics", {}).get("causes_probables")
        )
        if not signal_causes:
            continue
        if isinstance(signal_causes, str):
            signal_causes = [signal_causes]
        for cause in signal_causes:
            if cause and cause not in causes:
                causes.append(cause)
    return causes


def _severity_from(sig: dict) -> str:
    interest = float(sig.get("interest", 0) or 0)
    metrics = sig.get("metrics") or {}
    kind = sig.get("kind")

    if kind == "EXPOSURE":
        minutes_over = _number(metrics.get("minutes_over"))
        tolerance = _number(metrics.get("max_minutos_sobre_max"))
        if (
            tolerance is not None and minutes_over is not None
            and minutes_over >= tolerance
        ):
            return "CRITICO"
        return "ATENCION"

    if kind in ("RISING_TREND", "FALLING_TREND"):
        eta = _number(metrics.get("eta_minutes"))
        tolerance = _number(metrics.get("max_minutos_sobre_max"))
        if (
            eta is not None and tolerance is not None
            and eta >= 0 and tolerance > 0 and eta < tolerance
        ):
            return "ATENCION"

    if interest >= 0.85:
        return "ATENCION"
    return "INFO"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ============================================================================
# FACTORY
# ============================================================================

def build_llm(config: dict, audit_callback=None) -> BaseLLM:
    provider = str(config.get("provider", "")).strip().lower()
    timeout_seconds = int(config.get("timeout_seconds", 60))

    if provider == "ollama":
        model = config.get("model")
        if not model:
            raise ValueError("provider=ollama requiere 'model'.")

        return OllamaLLM(
            model=model,
            host=config.get("ollama_host", "http://localhost:11434"),
            timeout_seconds=timeout_seconds,
            num_ctx=int(config.get("num_ctx", 8192)),
            num_predict=int(config.get("num_predict", 1024)),
            num_thread=config.get("num_thread"),
            keep_alive=config.get("keep_alive", "10m"),
            think=config.get("think", False),
            circuit_failures=int(config.get("circuit_failures", 3)),
            circuit_open_seconds=int(config.get("circuit_open_seconds", 300)),
            audit_callback=audit_callback,
        )

    if provider == "template":
        return TemplateLLM()

    if provider == "demo":
        # Import perezoso: demo_llm importa BaseLLM de este módulo.
        from agent.demo_llm import DemoLLM

        return DemoLLM()

    raise ValueError(
        f"Backend LLM desconocido: '{provider}'. "
        "Use provider: ollama, template o demo."
    )