"""
agent/narrative.py — párrafo de ANÁLISIS escrito por el LLM encima de un informe ya calculado.

Los informes de temperatura son código determinista: las cifras nunca las inventa
un modelo. Esta capa es OPCIONAL (cooling.llm_analysis = true) y solo agrega un
párrafo corto que interpreta lo que el informe ya dice (qué significa, qué
revisar). Reglas de seguridad:

  * el modelo solo recibe el texto del informe, no series crudas;
  * todo número del párrafo debe aparecer en el informe (anclaje numérico de
    report_renderer); si alguno no, el párrafo se descarta y se envía el informe
    tal cual;
  * si el LLM falla, no responde o devuelve algo largo, se envía el informe tal cual;
  * el párrafo se marca como redactado por IA.

Ojo: la llamada es síncrona; con un modelo local lento bloquea el ciclo mientras
responde (con reloj acelerado, se pierden muestras simuladas).
"""
from __future__ import annotations

import logging
import re

from agent.report_renderer import numeric_anchor_ok

logger = logging.getLogger(__name__)

SYSTEM = (
    "Eres un analista de cadena de frío que asesora a un ingeniero de una planta de lácteos y cárnicos. "
    "Recibes un INFORME ya calculado sobre la temperatura de una cámara de refrigeración. "
    "Escribe un ANÁLISIS de máximo 4 frases en español, claro y directo: qué significa lo ocurrido para la "
    "conservación del producto y qué conviene revisar primero. "
    "Reglas estrictas: usa SOLO hechos del informe; no escribas ninguna cifra que no esté en el informe; "
    "no afirmes que el producto se dañó ni cuánta merma hubo (di 'puede afectar'); no repitas el informe; "
    "recuerda, si es relevante, que la temperatura es la del aire y no la del producto; sin listas ni títulos."
)

_MARKUP = re.compile(r"[#*_`>]+")


def add_analysis(report_text: str, llm, max_chars: int = 700) -> str:
    if llm is None or not report_text:
        return report_text
    try:
        out = llm.generate_text(SYSTEM, "INFORME:\n" + report_text + "\n\nEscribe el ANÁLISIS.")
    except Exception:
        logger.exception("El LLM falló al redactar el análisis; se envía el informe sin él.")
        return report_text
    if not out:
        return report_text
    out = _MARKUP.sub("", str(out)).strip()
    if not out or len(out) > max_chars:
        logger.warning("Análisis del LLM descartado (vacío o demasiado largo: %d).", len(out))
        return report_text
    ok, missing = numeric_anchor_ok(out, report_text)
    if not ok:
        logger.warning("Análisis del LLM descartado: cifras que no están en el informe: %s", sorted(missing))
        return report_text
    return report_text + "\n\nAnálisis (redactado por IA a partir de los datos de arriba):\n" + out
