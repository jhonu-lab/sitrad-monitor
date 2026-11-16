from __future__ import annotations
# agent.demo_llm
import re
from datetime import datetime
from typing import Optional

from agent.llm_client import BaseLLM
from analysis.diagnosis import DEFAULT_CFG
from core.clock import local_tz

# Canales por ROL (catálogo), no por nombre fijo.
CH_TEMP = DEFAULT_CFG["ch_temp"]
CH_KW = DEFAULT_CFG["ch_kw"]


class DemoLLM(BaseLLM):
    """
    Sustituto determinista de un LLM para demos y pruebas offline.

    NO razona. Redacta a partir del texto de contexto que genera
    ContextSerializer y, para preguntas abiertas, enruta por palabras
    clave hacia las herramientas REALES de CavaAgent:

        get_stats, get_slope, get_events, correlate, energy.

    Sirve para validar todo el flujo

        detección -> memoria -> contexto -> agente -> Telegram

    sin depender de Ollama. Todas las cifras que escribe provienen del
    contexto o de los resultados de las herramientas; no inventa datos
    y no afirma causas físicas.

    Uso en config.json:

        "llm": {"provider": "demo"}
    """

    name = "demo"

    LOCAL_TZ = local_tz()

    PATRONES = {
        "ascenso_rapido_recuperacion": "ascenso rápido con recuperación",
        "descenso_rapido_recuperacion": "descenso rápido con recuperación",
        "oscilacion_periodica": "oscilación periódica",
        "escalon": "cambio de nivel (escalón)",
        "deriva_lenta": "deriva lenta",
        "cambio_transitorio": "cambio transitorio",
    }

    @classmethod
    def _patron(cls, name: str) -> str:
        return cls.PATRONES.get(name, name.replace("_", " "))

    @classmethod
    def _local_hms(cls, iso: str) -> str:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(cls.LOCAL_TZ).strftime("%H:%M:%S")

    # ------------------------------------------------------------------
    # API de BaseLLM
    # ------------------------------------------------------------------

    def generate(self, system: str, user: str, schema: dict):
        return None

    def generate_text(self, system: str, user: str) -> Optional[str]:
        if "EVENTO DETECTADO" in user:
            return self._event_report(user)
        if "CONTEXTO DEL PERÍODO" in user:
            return self._periodic_report(user)
        return None

    # ------------------------------------------------------------------
    # Informe de evento
    # ------------------------------------------------------------------

    @staticmethod
    def _grab(pattern: str, text: str, default: str = "") -> str:
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default

    def _event_report(self, user: str) -> str:
        inicio = self._grab(r"Inicio:\s*\d+/\d+/\d+\s+([\d:]+)", user, "?")
        fin = self._grab(r"Fin:\s*\d+/\d+/\d+\s+([\d:]+)", user, "?")
        dur = self._grab(r"Duración:\s*([\d.]+)\s*min", user, "?")
        patron = self._grab(
            r"Patrón detectado por el motor:\s*(\S+)", user, "desconocido"
        )
        volvio = "Volvió al comportamiento previo: sí" in user
        similares = int(
            self._grab(r"Eventos similares:\s*(\d+)", user, "0") or 0
        )
        dur_hist = self._grab(r"Duración típica:\s*([\d.]+)", user)

        resumen: dict[str, str] = {}
        for m in re.finditer(
            r"^\s{4}([^:\n]+):\s*(-?[\d.]+ → -?[\d.]+ → -?[\d.]+ \S+)",
            user,
            re.MULTILINE,
        ):
            resumen[m.group(1).strip()] = m.group(2).strip()

        cabecera = (
            f"Evento entre {inicio} y {fin} ({dur} min). "
            f"Patrón temporal detectado: {self._patron(patron)}."
        )

        variables = [
            f"{canal}: {resumen[canal]}."
            for canal in (CH_TEMP, CH_KW)
            if canal in resumen
        ]

        if len(variables) < 3 and "Estado Compresor" in resumen:
            variables.append(
                "El estado del compresor también cambió durante la ventana."
            )

        recuperacion = (
            "Las variables recuperaron su nivel previo."
            if volvio
            else "Aún no se observa recuperación hacia el nivel previo."
        )

        if similares:
            extra = f" (duración típica {dur_hist} min)" if dur_hist else ""
            cierre = (
                f"Antecedentes: {similares} evento(s) similar(es){extra}. "
                "La causa no puede determinarse con estos datos. "
                "Sin acción requerida."
            )
        else:
            cierre = (
                "Sin antecedentes similares. "
                "La causa no puede determinarse con estos datos. "
                "Sin acción requerida."
            )

        # Máximo 6 líneas: cabecera + hasta 3 variables + recuperación + cierre.
        return "\n".join([cabecera, *variables[:3], recuperacion, cierre])

    # ------------------------------------------------------------------
    # Informe periódico
    # ------------------------------------------------------------------

    def _periodic_report(self, user: str) -> str:
        n_eventos = len(re.findall(r"^EVENTO \d+", user, re.MULTILINE))
        duracion = self._grab(r"DURACIÓN DEL PERÍODO:\s*([\d.]+)", user)

        out = [
            "Informe del período"
            + (f" ({float(duracion):.0f} min)." if duracion else ".")
        ]

        if n_eventos:
            out.append(f"Eventos detectados: {n_eventos}.")
            for m in re.finditer(
                r"Inicio:\s*\d+/\d+/\d+\s+([\d:]+).*?"
                r"Patrón detectado:\s*(\S+)",
                user,
                re.DOTALL,
            ):
                out.append(
                    f"  • {m.group(1)} — {self._patron(m.group(2))}"
                )
        else:
            out.append("No se detectaron eventos en el período.")
            for m in re.finditer(
                r"^\s{2}([^\n:]+):\n\s{4}Media:\s*(-?[\d.]+)"
                r"\n\s{4}Mínimo:\s*(-?[\d.]+)"
                r"\n\s{4}Máximo:\s*(-?[\d.]+)",
                user,
                re.MULTILINE,
            ):
                canal, media, mn, mx = m.groups()
                out.append(
                    f"  • {canal}: media {float(media):.2f}, "
                    f"rango {float(mn):.2f}..{float(mx):.2f}"
                )

        return "\n".join(out)

    # ------------------------------------------------------------------
    # Preguntas con herramientas
    # ------------------------------------------------------------------

    def generate_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        tool_executor,
        max_tool_rounds: int = 6,
    ) -> Optional[str]:

        q = user.lower()

        minutes = 30.0
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(horas?|h\b|min)", q)
        if m:
            value = float(m.group(1).replace(",", "."))
            minutes = value * 60.0 if m.group(2).startswith("h") else value

        # ---- energía --------------------------------------------------
        if "energ" in q or "consum" in q:
            r = tool_executor(
                "energy",
                {"canal": CH_KW, "minutos": minutes},
            )
            res = r.get("resultado")
            if res is None:
                return r.get("mensaje") or r.get("error") or "Sin datos."
            return (
                f"Energía activa en los últimos {minutes:.0f} min: "
                f"{float(res):.3f} kWh (integral de {CH_KW})."
            )

        # ---- correlación ---------------------------------------------
        if "correl" in q or "relaci" in q:
            r = tool_executor(
                "correlate",
                {
                    "a": CH_TEMP,
                    "b": CH_KW,
                    "minutos": minutes,
                },
            )
            res = r.get("resultado")
            if res is None:
                return r.get("mensaje") or r.get("error") or "Sin datos."
            return (
                f"Correlación de Pearson entre {CH_TEMP} y {CH_KW} "
                f"en {minutes:.0f} min: {float(res):+.2f} "
                f"({r.get('puntos_alineados', 0)} puntos alineados). "
                "Una correlación no implica causalidad."
            )

        # ---- eventos --------------------------------------------------
        if "evento" in q or "pas" in q or "ocurri" in q:
            r = tool_executor(
                "get_events",
                {
                    "desde": "2000-01-01T00:00:00+00:00",
                    "hasta": "2100-01-01T00:00:00+00:00",
                },
            )
            if not r.get("ok"):
                return f"No pude consultar eventos: {r.get('error')}"
            eventos = r.get("eventos", [])
            if not eventos:
                return "No hay eventos registrados."
            out = [f"Eventos registrados: {len(eventos)}."]
            for e in eventos[-5:]:
                out.append(
                    f"  • {self._local_hms(e['inicio'])} — "
                    f"{self._patron(e['patron'])}, "
                    f"{e['duracion_min']} min, "
                    f"{'recuperó' if e['volvio_a_base'] else 'sin recuperar'}"
                )
            return "\n".join(out)

        # ---- pendiente ------------------------------------------------
        if "pendiente" in q or "tendencia" in q:
            r = tool_executor(
                "get_slope",
                {"canal": CH_TEMP, "minutos": minutes},
            )
            res = r.get("resultado")
            if res is None:
                return (
                    f"En los últimos {minutes:.0f} min no hay una pendiente "
                    f"estadísticamente significativa en {CH_TEMP}."
                )
            return (
                f"Pendiente de {CH_TEMP} en {minutes:.0f} min: "
                f"{float(res):+.3f} °C/h."
            )

        # ---- por defecto: estadísticas -------------------------------
        r = tool_executor(
            "get_stats",
            {"canal": CH_TEMP, "minutos": minutes},
        )
        res = r.get("resultado")
        if not res:
            return r.get("mensaje") or r.get("error") or "Sin datos."
        return (
            f"{CH_TEMP} en los últimos {minutes:.0f} min: "
            f"media {res['media']:.2f} °C, mín {res['min']:.2f}, "
            f"máx {res['max']:.2f}, desviación "
            f"{res['desviacion_estandar']:.2f} "
            f"({res['n']} lecturas válidas)."
        )
