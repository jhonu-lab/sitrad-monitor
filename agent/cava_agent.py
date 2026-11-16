from __future__ import annotations
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from core.channel_catalog import format_channels
from analysis.engine import AnalysisEngine
from analysis.time_ranges import parse_time_range
from core.contracts import (
    DataQuality,
)
from core.series_store import SeriesStore

from agent.report_renderer import (
    ReportDraft,
    draft_from_alert,
    draft_from_resolution,
    draft_from_summary,
    numeric_anchor_ok,
    render,
    _severity_label,
)


logger = logging.getLogger(__name__)


# ================================================================
# PROMPT BASE
# ================================================================

BASE_PROMPT = """
Eres un analista técnico de una cava de refrigeración.

Tu función es INTERPRETAR la información proporcionada por el
motor de análisis y las mediciones disponibles.

No eres el sistema de adquisición de datos.
No eres el detector matemático de eventos.
No debes sustituir los cálculos realizados por las herramientas.

REGLAS GENERALES:

1. Utiliza exclusivamente la información proporcionada.
2. No inventes valores, tiempos, variables, eventos ni causas.
3. Diferencia siempre entre:
   - hecho observado;
   - resultado matemático;
   - interpretación;
   - hipótesis que no puede confirmarse.
4. Cuando una causa física no esté demostrada, utiliza expresiones
   como "compatible con", "podría estar relacionado con" o
   "no puede determinarse con los datos disponibles".
5. No conviertas automáticamente un patrón matemático en una
   causa física.
6. No afirmes que ocurrió una causa concreta si únicamente se
   observó un comportamiento en las variables.
7. Utiliza las unidades y cifras proporcionadas.
8. Mantén un tono técnico, claro y no alarmista.
9. Prioriza cambios relevantes y relaciones entre variables.
10. Si la evidencia no permite llegar a una conclusión, dilo
    explícitamente.
"""


# ================================================================
# FORMATO DE EVENTO
# ================================================================

EVENT_FORMAT = """
FORMATO DEL INFORME DE EVENTO:

El informe se envía a Telegram y debe tener como máximo 6 líneas.

Línea 1:
Indica cuándo ocurrió y qué patrón temporal fue detectado.

Líneas 2-4:
Explica qué variables cambiaron, cuánto cambiaron y cómo
se relacionaron, utilizando únicamente los datos disponibles.

Línea 5:
Indica si el comportamiento volvió o no hacia su comportamiento
previo, cuando esa información esté disponible.

Línea 6:
Indica una acción concreta únicamente si la evidencia la justifica.
Si no se requiere ninguna acción, escribe:
"Sin acción requerida."

No enumeres lecturas individuales ni timestamps sueltos.

Utiliza el resumen:
<valor_inicial> → <valor_máximo> → <valor_final>

en lugar de recitar toda la serie temporal.
"""


# ================================================================
# PROMPT ESPECÍFICO DE EVENTO
# ================================================================

EVENT_PROMPT = """
Interpreta el EventCandidate proporcionado.

El patrón indicado por el motor de análisis es una observación
matemática del comportamiento temporal. No lo conviertas
automáticamente en una causa física.

Si el patrón detectado es, por ejemplo, una recuperación,
describe la evolución observada y solamente plantea una posible
interpretación si existe evidencia suficiente.

Redacta el informe respetando exactamente las reglas generales
y el formato de evento.
"""

INCIDENT_PROMPT = """
Recibes un PAQUETE DE DIAGNÓSTICO ya calculado por la capa
determinista. NO recalcules la hipótesis. Tu tarea es:

1. Identificar la hipótesis principal calculada. Viene en el
   contexto como la línea 'hipotesis_principal_calculada: <nombre>'.

2. Redactar el diagnóstico usando SOLO los rasgos y las evidencias
   que aparecen en el paquete. Cada cifra del informe debe existir
   literalmente en el contexto.

3. Si el paquete marca 'provisional: si', DEBES empezar el informe
   con la palabra PROVISIONAL. No afirmes certeza que la capa
   determinista no afirma.

4. Terminar SIEMPRE con una línea literal:

   hipotesis_principal: <nombre>

   Donde <nombre> es exactamente el valor de
   'hipotesis_principal_calculada'.

No inventes causas que no aparezcan en las hipótesis del paquete.
No conviertas correlación en causalidad. No repitas la severidad en
los hechos.
"""
# ================================================================
# PROMPT DE INFORME PERIÓDICO
# ================================================================

PERIODIC_PROMPT = """
Genera un informe técnico del período recibido.

El informe debe:

1. Resumir el comportamiento general de las variables.
2. Identificar los eventos relevantes detectados.
3. Explicar los patrones temporales observados.
4. Relacionar variables cuando exista evidencia.
5. Señalar comportamientos anómalos o inusuales cuando los datos
   lo justifiquen.
6. Indicar recuperaciones cuando estén demostradas.
7. Diferenciar hechos de interpretaciones.
8. No inventar causas.
9. No convertir automáticamente un patrón en una causa física.
10. Ser breve y útil para lectura en Telegram.

No impongas el límite de 6 líneas utilizado para los informes
individuales de eventos.

No hagas un listado interminable de números.
Prioriza lo que realmente describe el comportamiento del sistema.
"""


# ================================================================
# P0: SIN IMÁGENES / URLS INVENTADAS
# ================================================================
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]*\)")
_BARE_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_CHART_ASK = re.compile(
    r"\b(grafic\w*|dibuj\w*|imagen|plot|chart|diagrama|curva)\b|"
    r"\b(?:temperatura|temp)\s+vs\s+(?:el\s+)?(?:tiempo|compresor|refr|evaporador)\b")
CHART_UNAVAILABLE = (
    "No pude generar el gráfico ahora. Te doy los números: pídeme «temperatura de las últimas 6 horas» "
    "(mínimo, máximo, promedio y tendencia) o «estado actual de la cava»."
)


def strip_fake_media(text: str | None) -> str | None:
    """El modelo no tiene cómo producir imágenes ni enlaces: cualquier imagen markdown o URL en su
    respuesta es inventada. Se elimina y, si había imagen, se avisa."""
    if not text:
        return text
    had_img = bool(_MD_IMG.search(text))
    out = _MD_IMG.sub("", text)
    out = _MD_LINK.sub(r"\1", out)
    out = _BARE_URL.sub("", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if had_img:
        out = (out + "\n\n" if out else "") + "(No puedo mostrar gráficos por este chat; te doy los valores en texto.)"
    return out


# ================================================================
# PROMPT DE PREGUNTA
# ================================================================
QUESTION_PROMPT = """
Responde la pregunta del usuario utilizando exclusivamente la
información disponible y las mediciones obtenidas mediante las
herramientas de análisis.

Si la pregunta requiere un cálculo, utiliza los datos
proporcionados por las herramientas.

Si la evidencia no permite responder con certeza, dilo.

No inventes datos.

Distingue entre:

- lo que se observó;
- lo que se puede calcular;
- lo que se puede interpretar;
- lo que no puede determinarse.

No apliques el formato de 6 líneas utilizado para los eventos.

Responde directamente a la pregunta del usuario.

IMPORTANTE SOBRE LOS CANALES:

Los nombres de los canales son exactos y deben utilizarse tal
como aparecen en la lista de canales disponibles.

No inventes nombres como "temperatura", "temperatura ambiente",
"potencia", etc. si esos nombres no aparecen exactamente en la
lista.

Si no conoces el nombre exacto de un canal, utiliza primero
la herramienta list_channels.

Si una herramienta devuelve un error indicando que el canal
no existe, utiliza el nombre exacto proporcionado por la lista
de canales y vuelve a intentar la operación.

REGLAS DE DESAMBIGUACIÓN:

1. Las equivalencias entre palabras de la pregunta y nombres de
   canal se indican en "ALIAS DE CANALES" al final de este prompt.
   Si una palabra no tiene alias y hay varios canales posibles
   (por ejemplo M1 y M2), pregunta cuál se refiere o repórtalos
   por separado.

2. No mezcles canales: no uses un canal como sustituto de otro.

REGLAS SOBRE ENERGÍA Y POTENCIA:

3. Para consumo energético ("energía", "kWh", "consumo"),
   usa SIEMPRE la herramienta `energy`. NUNCA uses `get_stats`
   para energía.

4. La herramienta `energy` devuelve `resultado` y
   `unidad_energia`. Reporta AMBOS: "<resultado> <unidad_energia>".
   Si el canal era de Potencia Activa (kW), la unidad es kWh.
   Si era de Potencia Aparente (kVA), la unidad es kVAh.

5. Para "potencia" o "promedio de potencia" (kW), usa `get_stats`.

REGLAS SOBRE CANALES:

6. Si la pregunta menciona un canal que NO aparece en la lista
   de canales disponibles, NO llames a ninguna herramienta sobre
   ese canal. Responde que ese canal no existe y enumera los
   canales disponibles.

7. Antes de llamar `correlate`, verifica que AMBOS canales
   existan. Si uno no existe, no llames a `correlate`.

REGLAS SOBRE PREGUNTAS FUERA DEL DOMINIO:

8. Si la pregunta pide información que no es una variable medida
   por el sistema — costo de energía, número de empleados,
   previsión de fallos, temperatura exterior, clima, niveles de
   luminosidad, humedad, presión — responde explícitamente que
   NO tienes ese dato. No especules. No construyas la respuesta
   a partir de otras variables. No ofrezcas cálculos indirectos.

REGLAS SOBRE FORMATO (OBLIGATORIAS):

9. NUNCA generes imágenes, gráficos, enlaces ni URLs. No uses la
    sintaxis markdown ![...](...) ni [texto](http...). No puedes
    enviar archivos. Si piden un gráfico, responde con los valores
    clave en texto (mínimo, máximo, promedio, tendencia).

REGLAS SOBRE PRODUCTO E INFORMES:

10. El sistema NO mide el producto. Solo existe el producto que el operario
    registró (herramientas `load_report` y `forecast_temperature`). NUNCA
    deduzcas que hay o no hay producto a partir de la temperatura, el compresor
    o la puerta.

11. Cuando una herramienta de informe (`status_report`, `temperature_report`,
    `load_report`, `forecast_temperature`, `explain_temperature`) responda la
    pregunta, usa SU texto tal cual; no lo resumas ni lo reinterpretes.

REGLAS SOBRE EVENTOS:

12. Si la pregunta dice "hoy", calcula el rango del día actual
    en hora local (America/Bogota) usando `get_current_time` y
    llama `get_events` con `desde`/`hasta` ISO 8601. No pidas
    fechas al usuario si puedes calcularlas.

13. Si la pregunta dice "última hora", "últimos N minutos", usa
    el tiempo actual como `hasta` y réstale la ventana.

REGLAS SOBRE VENTANAS:

14. Si la ventana solicitada supera las 24 h de retención del
    sistema, indícalo y ofrece el resultado con el máximo
    disponible (24 h). No inventes datos más allá.

REGLAS SOBRE LOS INSTRUMENTOS (esta instalación):

15. Los instrumentos son los de la lista de canales disponibles. El
    CONTROLADOR tiene hasta 9 canales: Temp Cámara, Setpoint, Desvío vs
    Setpoint, Estado Refrigeración (REFR, la orden de frío), Estado
    Deshielo, Puerta Abierta, Salida HOT, Temp Evaporador y Estado
    Ventilador. MULTIPOWER (hasta 30 canales: medidores eléctricos M1 y
    M2 con tensión, corriente, potencia, factor de potencia, frecuencia,
    energía, simetrías y demanda) existe SOLO si sus canales aparecen en
    la lista. No existe temperatura de producto. "Ambiente" es la sonda
    S1 del controlador (aire de la cámara), no la temperatura del cuarto.

16. NO afirmes qué equipo mide M1 o M2. Son medidores; no se sabe cuál es
    el compresor. Di "M1 sube cuando REFR está activo" (evidencia), nunca
    "M1 es el compresor". "Compresor" en la pregunta = Estado Refrigeración
    (la orden del controlador); su efecto eléctrico se ve en M1 Potencia
    Activa.

17. Cuando el usuario pida un dato, CONSÚLTALO con la herramienta: no pidas
    permiso ni confirmación. "Temperatura de la cava" = Temp Cámara con
    get_stats a 60 minutos si no indica otra ventana.

18. Las herramientas reciben MINUTOS (o `horas`). 24 horas = 1440 minutos.
    Compara la ventana pedida con la cobertura real y dilo si hay menos
    datos de los pedidos.

19. Para la relación con el compresor usa compressor_relation (REFR contra
    M1/M2 y contra la temperatura). No correlaciones Temp Cámara contra kW:
    la temperatura reacciona con retraso y da valores bajos que NO significan
    "sin relación". Nunca uses los canales de Demanda.

20. "¿Cómo está la cava?" o "¿hay algún problema?": usa diagnose_now y
    reporta la hipótesis con su evidencia. Si responde "ninguna", di que
    no hay evidencia de falla. Separa hechos de hipótesis.

21. "Mayores variaciones, picos o cambios": usa top_variations, no
    get_slope. Indica si coinciden con deshielo (esperado) o puerta.

22. Sobre suficiencia o antigüedad de datos usa data_coverage SIN
    horas_requeridas, salvo que el usuario pida un número concreto de horas.

23. Un aumento de temperatura o de kW no es una avería por sí solo: el
    deshielo programado sube la temperatura a propósito.

24. Responde corto: 3 a 6 líneas, citando solo las cifras que sostienen la
    conclusión. No vuelques todas las cifras de la herramienta.
"""
# ================================================================
# PROMPT DE DRAFT (FASE 5)
# ================================================================

DRAFT_SCHEMA_PROMPT = """
Devuelve EXCLUSIVAMENTE un objeto JSON con esta forma:

{
  "titulo":     "<título corto, sin cifras nuevas>",
  "severidad":  "alta" | "media" | "baja",
  "hechos":     ["<hecho 1>", "<hecho 2>", ...],
  "hipotesis":  ["<hipótesis no confirmada>", ...],
  "accion":     "<acción concreta o 'Sin acción requerida.'>"
}

Reglas estrictas:

1. No inventes cifras. Cada número que aparezca en 'hechos'
   o 'hipotesis' debe existir en el CONTEXTO.
2. Separa siempre hechos (observables) de hipótesis (no
   confirmadas). Si no tienes hipótesis, deja la lista vacía.
3. Máximo 4 hechos y 2 hipótesis.
4. No repitas el encabezado ni la severidad dentro de 'hechos'.
5. Si no puedes aportar valor añadido al contexto, responde
   con el JSON mínimo del tipo de mensaje.
"""


# ================================================================
# JSON SCHEMA PARA EL LLM (FASE 5)
# ================================================================

DRAFT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "severidad": {
            "type": "string",
            "enum": ["alta", "media", "baja"],
        },
        "hechos": {
            "type": "array",
            "items": {"type": "string"},
        },
        "hipotesis": {
            "type": "array",
            "items": {"type": "string"},
        },
        "accion": {"type": "string"},
    },
    "required": [
        "titulo",
        "severidad",
        "hechos",
        "accion",
    ],
}


INCIDENT_LABELS = {
    "out_of_profile": "valor fuera de lo habitual",
    "persistent_drift": "deriva persistente",
    "cross_inconsistency": "inconsistencia entre instrumentos",
    "state_overrun": "estado prolongado más de lo programado",
    "flat_signal": "señal sin variación",
    "missing": "falta de datos",
    "offline": "instrumento sin comunicación",
    "err": "lecturas con error",
    "capacidad_reducida": "pérdida de capacidad de enfriamiento",
}


def incident_label(kind: str) -> str:
    return INCIDENT_LABELS.get(kind, str(kind).replace("_", " "))


CHART_DIR = "logs/charts"
CHART_TZ = "America/Bogota"
TEMP_LIMIT_C = 4.0          # solo si la config de enfriamiento no está disponible


class CavaAgent:
    """
    Agente de análisis de datos de Sitrad.

    Responsabilidades:

    - interpretar EventCandidate;
    - generar informes periódicos;
    - responder preguntas;
    - utilizar AnalysisEngine para cálculos;
    - obtener datos históricos mediante SeriesStore;
    - entregar tructurado al LLM;
    - exponer herramientas deterministas al LLM.
    - convertir datos estructurados (alertas, hallazgos, resúmenes)
      en mensajes listos para enviar (Fase 5).

    No es responsable de:

    - leer directamente Sitrad;
    - almacenar datos;
    - enviar Telegram;
    - detectar causas mediante reglas rígidas.
    """

    # ============================================================
    # HERRAMIENTAS DEL AGENTE
    # ============================================================

    # Memoria de conversación por usuario (opcional) y alias de canales.
    conversations = None
    channel_aliases: dict = {}
    _current_user: str | None = None

    TOOLS = [

        # --------------------------------------------------------
        # LIST CHANNELS
        # --------------------------------------------------------
        {
        "type": "function",
        "function": {
            "name": "list_converters",
            "description": (
                "Lista los convertidores SITRAD con sus instrumentos y los canales "
                "de cada instrumento (incluye los descartados y por qué). "
                "Úsala para 'qué hardware hay conectado', 'qué convertidores tienes', "
                "'qué mide cada equipo' o cuando el usuario pregunte por un canal "
                "que no está en la lista del instrumento activo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "refresh": {"type": "boolean",
                                "description": "Si true, vuelve a consultar SITRAD en vez de usar la caché."},
                },
                "required": [],
            },
        },
    },


            {
            "type": "function",
            "function": {
                "name": "alarm_history",
                "description": (
                    "Alarmas registradas por SITRAD entre dos fechas, opcionalmente "
                    "filtradas por instrumento o por código/texto. Úsala para "
                    "'cuántas alarmas ha tenido', 'qué alarmas saltaron ayer', "
                    "'cuándo fue la última alarma de puerta'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "desde": {"type": "string", "description": "ISO 8601 (por defecto, hace 7 días)."},
                        "hasta": {"type": "string", "description": "ISO 8601 (por defecto, ahora)."},
                        "instrumento": {"type": "string",
                                        "description": "Filtra por nombre de instrumento (substring)."},
                        "codigo": {"type": "string",
                                   "description": "Filtra por código o texto de alarma (substring)."},
                    },
                    "required": [],
                },
            },
        },


                {
            "type": "function",
            "function": {
                "name": "correlate_with_alarms",
                "description": (
                    "Cruza las variaciones de un canal con las alarmas de SITRAD en la "
                    "misma ventana. Úsala para 'las subidas coinciden con alarmas', "
                    "'la alarma de puerta explica los picos', 'hay relación entre "
                    "deshielo y alarmas de timeout'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "canal": {"type": "string", "description": "Nombre exacto del canal."},
                        "desde": {"type": "string"},
                        "hasta": {"type": "string"},
                    },
                    "required": ["canal"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_channels",
                "description": (
                    "Devuelve los nombres exactos de los canales "
                    "disponibles en el equipo. Úsala cuando no sepas "
                    "el nombre exacto de un canal."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },

        # --------------------------------------------------------
        # CURRENT TIME
        # --------------------------------------------------------

        {
            "type": "function",
            "function": {
                "name": "get_current_time",
                "description": (
                    "Devuelve la fecha y hora actuales en UTC."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },

        # --------------------------------------------------------
        # GET STATS
        # --------------------------------------------------------

        {
            "type": "function",
            "function": {
                "name": "get_stats",
                "description": (
                    "Obtiene estadísticas matemáticas de un canal "
                    "durante los últimos N minutos. "
                    "Si no conoces el nombre exacto del canal, "
                    "llama primero a list_channels."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "canal": {
                            "type": "string",
                            "description": (
                                "Nombre exacto del canal."
                            ),
                        },
                        "minutos": {
                            "type": "number",
                            "description": (
                                "Cantidad de minutos hacia atrás."
                            ),
                        },
                    },
                    "required": [
                        "canal",
                        "minutos",
                    ],
                },
            },
        },

        # --------------------------------------------------------
        # GET SLOPE
        # --------------------------------------------------------

        {
            "type": "function",
            "function": {
                "name": "get_slope",
                "description": (
                    "Calcula la pendiente temporal de un canal "
                    "durante los últimos N minutos. "
                    "Si no conoces el nombre exacto del canal, "
                    "llama primero a list_channels."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "canal": {
                            "type": "string",
                            "description": (
                                "Nombre exacto del canal."
                            ),
                        },
                        "minutos": {
                            "type": "number",
                            "description": (
                                "Cantidad de minutos hacia atrás."
                            ),
                        },
                    },
                    "required": [
                        "canal",
                        "minutos",
                    ],
                },
            },
        },

        # --------------------------------------------------------
        # GET EVENTS
        # --------------------------------------------------------

        {
            "type": "function",
            "function": {
                "name": "get_events",
                "description": (
                    "Consulta eventos históricos registrados "
                    "entre dos fechas."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "desde": {
                            "type": "string",
                            "description": (
                                "Fecha/hora ISO 8601."
                            ),
                        },
                        "hasta": {
                            "type": "string",
                            "description": (
                                "Fecha/hora ISO 8601."
                            ),
                        },
                    },
                    "required": [
                        "desde",
                        "hasta",
                    ],
                },
            },
        },

        # --------------------------------------------------------
        # CORRELATE
        # --------------------------------------------------------

        {
            "type": "function",
            "function": {
                "name": "correlate",
                "description": (
                    "Calcula la correlación de Pearson entre "
                    "dos canales durante los últimos N minutos. "
                    "Si no conoces el nombre exacto de alguno de "
                    "los canales, llama primero a list_channels."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "a": {
                            "type": "string",
                            "description": (
                                "Primer canal. Debe ser el nombre "
                                "exacto registrado."
                            ),
                        },
                        "b": {
                            "type": "string",
                            "description": (
                                "Segundo canal. Debe ser el nombre "
                                "exacto registrado."
                            ),
                        },
                        "minutos": {
                            "type": "number",
                            "description": (
                                "Cantidad de minutos hacia atrás."
                            ),
                        },
                    },
                    "required": [
                        "a",
                        "b",
                        "minutos",
                    ],
                },
            },
        },

        # --------------------------------------------------------
        # ENERGY
        # --------------------------------------------------------

        {
            "type": "function",
            "function": {
                "name": "energy",
                "description": (
                    "Calcula la energía integrada de un canal "
                    "de potencia durante los últimos N minutos. "
                    "Si no conoces el nombre exacto del canal, "
                    "llama primero a list_channels."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "canal": {
                            "type": "string",
                            "description": (
                                "Canal de potencia."
                            ),
                        },
                        "minutos": {
                            "type": "number",
                            "description": (
                                "Cantidad de minutos hacia atrás."
                            ),
                        },
                    },
                    "required": [
                        "canal",
                        "minutos",
                    ],
                },
            },
        },

        # --------------------------------------------------------
        # HISTORIAL DEL USUARIO
        # --------------------------------------------------------

        {
            "type": "function",
            "function": {
                "name": "historial_usuario",
                "description": (
                    "Busca en las conversaciones anteriores de ESTE "
                    "usuario lo que se le respondió antes. Úsala cuando "
                    "pregunte \"qué me dijiste\", \"qué te pregunté\" o "
                    "se refiera a una respuesta pasada."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "consulta": {
                            "type": "string",
                            "description": "Palabras clave del tema.",
                        },
                    },
                    "required": ["consulta"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "data_coverage",
                "description": (
                    "Resume cuánta historia hay por instrumento y qué "
                    "canales ya tienen datos suficientes para conocer su "
                    "comportamiento habitual. Úsala para preguntas sobre "
                    "disponibilidad, antigüedad o suficiencia de datos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "horas_requeridas": {
                            "type": "number",
                                "description": (
                                "OMÍTELO salvo que el usuario pida un número "
                                "concreto de horas. Por defecto se usa el "
                                "criterio de comportamiento habitual (2 h)."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "top_variations",
                "description": (
                    "Devuelve las mayores variaciones (subidas o bajadas "
                    "sostenidas) de UN canal: valor inicial, final, "
                    "diferencia y duración. Úsala para \"mayores cambios\", "
                    "\"picos\" o \"cuánto subió/bajó\"."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "canal": {"type": "string",
                                  "description": "Nombre exacto del canal."},
                        "minutos": {"type": "number",
                                    "description": "Ventana en MINUTOS (24 h = 1440)."},
                        "n": {"type": "integer",
                              "description": "Cuántas variaciones (por defecto 3)."},
                    },
                    "required": ["canal", "minutos"],
                },
            },
        },
                {
            "type": "function",
            "function": {
                "name": "compressor_relation",
                "description": (
                    "Relación entre la orden de refrigeración (REFR) y el "
                    "consumo de M1 y M2 y la temperatura de la cámara. Úsala "
                    "para \"correlación con el compresor\", \"qué medidor "
                    "corresponde al compresor\" o \"el compresor responde\"."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutos": {"type": "number",
                                    "description": "Ventana en MINUTOS (por defecto 240)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "diagnose_now",
                "description": (
                    "Ejecuta el diagnóstico determinista sobre los datos "
                    "recientes de AMBOS instrumentos (controlador y "
                    "Multipower) y devuelve hipótesis con su evidencia. "
                    "Úsala para \"cómo está la cava\", \"hay algún "
                    "problema\" o \"qué está pasando\"."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutos": {"type": "number",
                                    "description": "Ventana (por defecto 60)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "controller_summary",
                "description": (
                    "Resumen de lo que reporta el controlador: puerta (aperturas), compresor (ciclo de "
                    "trabajo), deshielo, setpoint, o producto registrado en la cámara."
                ),
                "parameters": {"type": "object", "properties": {
                    "tema": {"type": "string",
                             "description": "puerta | compresor | deshielo | setpoint | producto | catalogo"},
                    "minutos": {"type": "number", "description": "Periodo en MINUTOS (por defecto 1440)."}},
                    "required": ["tema"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "render_chart",
                "description": (
                    "Genera un gráfico REAL (imagen PNG) de uno o varios canales en los últimos N minutos "
                    "(o entre desde/hasta). Con varios canales usa dos ejes (°C y estados). "
                    "Úsala cuando pidan un gráfico, gráfica o plot. No escribas URLs ni markdown de imagen."
                ),
                "parameters": {"type": "object", "properties": {
                    "canal": {"type": "string", "description": "Un canal (por defecto Temp Cámara)."},
                    "canales": {"type": "array", "items": {"type": "string"},
                                "description": "Varios canales con su nombre exacto."},
                    "minutos": {"type": "number", "description": "Periodo en MINUTOS (por defecto 60)."},
                    "desde": {"type": "string", "description": "ISO 8601 opcional."},
                    "hasta": {"type": "string", "description": "ISO 8601 opcional."},
                    "marcar_limite": {"type": "boolean"}, "marcar_episodios": {"type": "boolean"}},
                    "required": ["minutos"]},
            },
        },
        {"type": "function", "function": {
            "name": "history_coverage",
            "description": "Desde cuándo hay datos guardados, hasta cuándo y cuántas lecturas por canal.",
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "get_episodes",
            "description": "Lista los episodios sobre el límite (con duración, pico y causa) entre dos fechas.",
            "parameters": {"type": "object", "properties": {
                "desde": {"type": "string", "description": "ISO 8601 (por defecto, hace 24 h)."},
                "hasta": {"type": "string", "description": "ISO 8601 (por defecto, ahora)."},
                "canal": {"type": "string"}, "causa": {"type": "string"},
                "min_min": {"type": "number", "description": "Duración mínima en minutos."},
                "limit": {"type": "number"}}, "required": []}}},
        {"type": "function", "function": {
            "name": "episode_summary",
            "description": "Estadística de episodios sobre el límite: por causa, duración mediana, tiempo total.",
            "parameters": {"type": "object", "properties": {
                "desde": {"type": "string"}, "hasta": {"type": "string"}}, "required": []}}},
        {"type": "function", "function": {
            "name": "data_quality",
            "description": "Calidad de los datos por canal: cobertura, huecos y antigüedad del último dato.",
            "parameters": {"type": "object", "properties": {
                "minutos": {"type": "number", "description": "Ventana en MINUTOS (por defecto 1440)."}},
                "required": []}}},
        {"type": "function", "function": {
            "name": "data_quality_canal",
            "description": "Calidad de los datos de UN canal.",
            "parameters": {"type": "object", "properties": {
                "canal": {"type": "string"}, "minutos": {"type": "number"}}, "required": ["canal"]}}},
        {"type": "function", "function": {
            "name": "aggregate",
            "description": "Agrega un canal por hora, día o semana (mean|min|max|count|sum|p95).",
            "parameters": {"type": "object", "properties": {
                "canal": {"type": "string"}, "desde": {"type": "string"}, "hasta": {"type": "string"},
                "agg": {"type": "string", "description": "mean|min|max|count|sum|p95"},
                "group_by": {"type": "string", "description": "hour|day|week|none"}},
                "required": ["canal"]}}},
        {"type": "function", "function": {
            "name": "export_csv",
            "description": "Exporta las lecturas de un canal entre dos fechas a un CSV en el equipo del bot.",
            "parameters": {"type": "object", "properties": {
                "canal": {"type": "string"}, "desde": {"type": "string"}, "hasta": {"type": "string"}},
                "required": ["canal"]}}},
        {
            "type": "function",
            "function": {
                "name": "load_report",
                "description": (
                    "Informe de ingreso de un producto registrado: cuándo entró, cuánto tardó la cámara "
                    "en volver a 4 °C, exposición sobre el límite y trabajo del compresor. Úsala para "
                    "\"informe de la ternera\" o \"cómo le fue al producto que entró\"."
                ),
                "parameters": {"type": "object", "properties": {
                    "detalle": {"type": "boolean", "description": "true solo si el usuario pide más detalle."},
                    "producto": {"type": "string", "description": "Nombre del producto (opcional)."}},
                    "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "status_report",
                "description": (
                    "Estado ACTUAL de la cava en lenguaje claro: temperatura frente al límite, "
                    "compresor, puerta, deshielo, historial, producto registrado y conclusión. "
                    "Úsala para \"estado actual\", \"cómo está la cava\", \"temperatura actual\"."
                ),
                "parameters": {"type": "object", "properties": {
                    "detalle": {"type": "boolean", "description": "true solo si el usuario pide más detalle."},
                    "breve": {"type": "boolean", "description": "Solo temperatura actual."}},
                    "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "forecast_temperature",
                "description": (
                    "Hacia dónde va la temperatura: ritmo de subida, en cuánto tiempo llegaría "
                    "a 4 °C o al nivel crítico SI SIGUE ASÍ, causas posibles y riesgo del producto "
                    "registrado. Úsala para \"llegará a X grados\", \"qué lo provoca\" o "
                    "\"hay riesgo para la ternera\"."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                    "detalle": {"type": "boolean", "description": "true solo si el usuario pide más detalle."},
                        "minutos": {"type": "number",
                                    "description": "Ventana de tendencia en MINUTOS (10 a 60)."},
                        "objetivo_c": {"type": "number",
                                       "description": "Temperatura objetivo en °C si el usuario pregunta por una."},
                        "horizonte_min": {"type": "number",
                                          "description": "Dentro de cuántos MINUTOS quiere saber qué pasará "
                                                         "(p. ej. \"en 1 hora 5 min\" = 65)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "temperature_report",
                "description": (
                    "Informe de la temperatura de la cámara: tiempo dentro del "
                    "límite, episodios sobre el límite, cuánto tardó en "
                    "recuperarse y si cumple. Con episodio=\"ultimo\" informa solo "
                    "el último episodio. Úsala para \"cómo estuvo la cava\", "
                    "\"cuánto tardó en volver a 4 grados\" o \"dame el informe\"."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                    "detalle": {"type": "boolean", "description": "true solo si el usuario pide más detalle."},
                        "minutos": {"type": "number",
                                    "description": "Periodo en MINUTOS (por defecto 1440)."},
                        "episodio": {"type": "string",
                                     "description": "\"ultimo\" para solo el último episodio."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "explain_temperature",
                "description": (
                    "Explica qué pasa con la temperatura de la cava: tendencia, "
                    "ciclo del compresor y consumo frente a lo habitual, e "
                    "hipótesis con evidencia a favor/en contra y datos que "
                    "faltan. Úsala para \"por qué sube la temperatura\", "
                    "\"coincide con el consumo\" o \"es normal\"."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutos": {"type": "number",
                                    "description": "Ventana en MINUTOS (por defecto 120)."},
                    },
                    "required": [],
                },
            },
        },
    ]

    # ============================================================
    # CONSTRUCTOR
    # ============================================================

    def __init__(
        self,
        llm,
        engine: AnalysisEngine,
        store: SeriesStore,
        context_serializer=None,
        memory=None,
        device_id: int = 0,
        clock=None,
        levels_provider=None,
    ) -> None:

        if llm is None:
            raise ValueError(
                "CavaAgent requiere un cliente LLM."
            )

        if engine is None:
            raise ValueError(
                "CavaAgent requiere un AnalysisEngine."
            )

        if store is None:
            raise ValueError(
                "CavaAgent requiere un SeriesStore."
            )

        self.llm = llm
        self.engine = engine
        self.store = store
        self.context_serializer = context_serializer
        self.memory = memory
        self.device_id = int(device_id)
        # Reloj de la simulación; sin él el agente usa la hora real.
        self._clock = clock
        # Callable -> niveles aprendidos por AnomalyDetector (diagnóstico).
        self._levels_provider = levels_provider

    # ============================================================
    # EVENTO
    # ============================================================


    # ============================================================
    # INFORME PERIÓDICO
    # ============================================================

    def report_periodic(
        self,
        context: str,
    ) -> Optional[str]:
        """
        Genera un informe general del período.
        """

        if not context or not context.strip():
            return None

        user_prompt = (
            "CONTEXTO DEL PERÍODO\n"
            "====================\n\n"
            f"{context}\n\n"
            "Genera el informe técnico del período."
        )

        return self._generate(
            system=(
                BASE_PROMPT
                + "\n"
                + PERIODIC_PROMPT
            ),
            user=user_prompt,
        )

    # ============================================================
    # FASE 5 — INFORME COMO CONVERSIÓN DE DATOS
    # ============================================================

    def report_alert(
        self,
        *,
        channel: str,
        kind: str,
        severity: float,
        since_iso: str,
        evidence: dict[str, Any],
        context: str,
    ) -> str:
        """
        Genera el mensaje de una alerta.

        Siempre devuelve texto: si el LLM falla o alucina cifras,
        cae a la plantilla determinista.
        """
        fallback = draft_from_alert(
            channel=channel,
            kind=kind,
            severity=severity,
            since_iso=since_iso,
            evidence=evidence,
        )

        user_prompt = (
            "TIPO DE MENSAJE: ALERTA\n\n"
            f"Canal: {channel}\n"
            f"Tipo de anomalía: {kind}\n"
            f"Severidad calculada: {severity:.2f}\n"
            f"Desde: {since_iso}\n\n"
            f"Evidencia: {evidence}\n\n"
            f"CONTEXTO\n========\n{context}\n\n"
            "Redacta el JSON. No repitas la severidad ni el canal "
            "en 'hechos'."
        )

        return self._render_with_llm_or_fallback(
            user_prompt,
            fallback,
            context,
        )

    def report_resolution(
        self,
        *,
        channel: str,
        kind: str,
        duration_min: float,
        since_iso: str,
        context: str,
    ) -> str:
        """
        Genera el mensaje de resolución de una anomalía.
        """
        fallback = draft_from_resolution(
            channel=channel,
            kind=kind,
            duration_min=duration_min,
            since_iso=since_iso,
        )

        user_prompt = (
            "TIPO DE MENSAJE: RESOLUCIÓN\n\n"
            f"Canal: {channel}\n"
            f"Tipo: {kind}\n"
            f"Duración: {duration_min:.1f} min\n"
            f"Inicio: {since_iso}\n\n"
            f"CONTEXTO\n========\n{context}\n\n"
            "Redacta el JSON. Si no hay acción, "
            "usa 'Sin acción requerida.'."
        )

        return self._render_with_llm_or_fallback(
            user_prompt,
            fallback,
            context,
        )
    def report_summary(
        self,
        *,
        events: list[dict[str, Any]],
        window_hours: float,
        context: str,
    ) -> str:
        """
        Resumen agregado de una ventana. Es un listado de hechos ya
        calculados: se anota cada evento con su contexto (deshielo, puerta)
        y se redacta en código, sin LLM.
        """
        try:
            events = self._annotate_events(events)
        except Exception:
            logger.exception("No se pudieron anotar los eventos del resumen.")
        return render(draft_from_summary(events=events, window_hours=window_hours))

    def _annotate_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from datetime import timedelta
        from core.clock import local_tz

        dev = self._device_id()
        tz = local_tz()
        known = set(self.store.channels(device_id=dev))
        span = float(getattr(self.store, "max_hours", 24.0)) * 60.0

        def valid(ch):
            if ch not in known:
                return []
            return [x for x in self.store.get_series(dev, ch, minutes=span)
                    if x.value is not None and x.quality.value in ("VALID", "FROZEN")]

        temp = valid("Temp Cámara")
        defr = [x.event_time for x in valid("Estado Deshielo") if float(x.value) > 0.5]
        door = [x.event_time for x in valid("Puerta Abierta") if float(x.value) > 0.5]
        grace = timedelta(minutes=45)
        names = {"escalon": "Escalón", "deriva_lenta": "Deriva lenta",
                 "oscilacion_periodica": "Oscilación periódica"}

        out = []
        for ev in events:
            e = dict(ev)
            try:
                t0 = self._parse_datetime(ev["inicio"])
                t1 = self._parse_datetime(ev["fin"])
            except Exception:
                out.append(e)
                continue
            pts = [x for x in temp if t0 <= x.event_time <= t1]
            if len(pts) < 2:
                out.append(e)
                continue
            v0, v1 = float(pts[0].value), float(pts[-1].value)
            subida = v1 > v0
            if any(t0 - grace <= x <= t1 for x in defr):
                ctx = ("coincide con un deshielo programado (esperado)" if subida
                       else "recuperación de la cámara tras un deshielo (esperado)")
                e["explicado"] = True
            elif any(t0 <= x <= t1 for x in door):
                ctx = "con la puerta abierta"
                e["explicado"] = False
            else:
                ctx = "sin deshielo ni puerta asociados"
                e["explicado"] = False
            tipo = names.get(ev.get("patron"), str(ev.get("patron", "Evento")).capitalize())
            e["descripcion"] = (
                f"{tipo} de {'subida' if subida else 'bajada'}: {v0:.1f} → {v1:.1f} °C "
                f"({v1 - v0:+.1f} °C) en {(t1 - t0).total_seconds() / 60.0:.0f} min, "
                f"{t0.astimezone(tz):%H:%M}–{t1.astimezone(tz):%H:%M} hora local: {ctx}"
            )
            out.append(e)
        return out


    # ------------------------------------------------------------
    # NÚCLEO DE FASE 5
    # ------------------------------------------------------------

    def _render_with_llm_or_fallback(
        self,
        user_prompt: str,
        fallback: ReportDraft,
        context: str,
    ) -> str:
        """
        Intenta LLM con schema JSON. Si:

          - el LLM no implementa generate_text o devuelve None
          - la respuesta no es JSON válido
          - el draft es inválido
          - el anclaje numérico falla

        → cae a la plantilla determinista.

        Nunca propaga excepciones: siempre devuelve texto.
        """
        draft = self._try_llm_draft(user_prompt)

        if draft is None or not draft.is_valid():
            return render(fallback)

        report_text = render(draft)

        ok, missing = numeric_anchor_ok(
            report_text,
            context,
        )

        if not ok:
            logger.warning(
                "Anclaje numérico falló. Números no presentes en "
                "contexto: %s. Se usa plantilla.",
                missing,
            )
            return render(fallback)

        return report_text

    def _try_llm_draft(
        self,
        user_prompt: str,
    ) -> ReportDraft | None:
        """
        Pide al LLM un JSON compatible con ReportDraft.

        Devuelve None si el LLM no está disponible, la respuesta
        no es JSON parseable, o el JSON no puede convertirse en
        un ReportDraft.
        """
        if not hasattr(self.llm, "generate_text"):
            return None

        system = BASE_PROMPT + "\n" + DRAFT_SCHEMA_PROMPT

        try:
            response = self.llm.generate_text(
                system=system,
                user=user_prompt,
                schema=DRAFT_JSON_SCHEMA,
            )
        except TypeError:
            # LLM sin soporte de schema: reintenta sin schema.
            try:
                response = self.llm.generate_text(
                    system=system,
                    user=user_prompt,
                )
            except Exception:
                logger.exception("Fallo generando draft.")
                return None
        except Exception:
            logger.exception("Fallo generando draft.")
            return None

        if response is None:
            return None

        raw = self._extract_json(response)

        if raw is None:
            return None

        try:
            return ReportDraft.from_dict(raw)
        except Exception:
            logger.exception(
                "ReportDraft.from_dict falló con: %r",
                raw,
            )
            return None

    @staticmethod
    def _extract_json(text: Any) -> Any:
        """
        Extrae un objeto JSON del texto devuelto por el LLM.

        Soporta bloques ```json ... ``` además de JSON puro.
        Devuelve None si no puede parsear.
        """
        import json

        s = str(text).strip()

        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:].strip()

        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------
    # DISPATCHER OPCIONAL
    # ------------------------------------------------------------

    def render_policy_message(self, msg, context: str) -> str:
        payload = getattr(msg, "payload", {}) or {}
        kind = getattr(msg.kind, "value", str(msg.kind))

        if "incident_kind" in payload:
            bundle = payload.get("diagnosis")
            key = str(getattr(msg, "key", ""))
            stage = ("update" if key.startswith("incident_update")
                     else "escalate" if key.startswith("incident_escalate") else "open")
            if bundle is not None:
                return self.report_incident(
                    incident_kind=payload["incident_kind"],
                    channels=payload.get("channels", []),
                    severity=payload.get("severity_max", 0.0),
                    opened_at=payload.get("opened_at", ""),
                    bundle=bundle,
                    context=context,
                    stage=stage,
                    previous=payload.get("previous_principal"),
                )
            if kind == "RESOLUCION" and "duration_min" in payload:
                return (
                    f"✅ Resuelto: {incident_label(payload['incident_kind'])}\n"
                    f"Canales: {format_channels(payload.get('channels', []))}\n"
                    f"Duración: {float(payload['duration_min']):.0f} min"
                )
            return str(msg.text)


        if kind in ("ALERTA", "SALUD"):
            return self.report_alert(
                channel=payload["channel"],
                kind=payload["anomaly_kind"],
                severity=payload["severity"],
                since_iso=payload["since_iso"],
                evidence=payload.get("evidence", {}),
                context=context,
            )

        if kind == "RESOLUCION":
            return self.report_resolution(
                channel=payload["channel"],
                kind=payload["anomaly_kind"],
                duration_min=payload["duration_min"],
                since_iso=payload["since_iso"],
                context=context,
            )

        if kind == "RESUMEN":
            try:
                r = self._tool_temperature_report(
                    {"minutos": float(payload.get("window_hours", 8.0) or 8.0) * 60.0})
                if r.get("ok") and r.get("respuesta_base"):
                    return r["respuesta_base"]
            except Exception:
                logger.exception("Informe de temperatura falló; uso el resumen clásico.")
            return self.report_summary(
                events=payload.get("events", []),
                window_hours=payload.get("window_hours", 1.0),
                context=context,
            )

        # default: informe periódico legado.
        return self.report_periodic(context) or ""

    # ============================================================
    # PREGUNTA
    # ============================================================
    def report_incident(
        self,
        *,
        incident_kind: str,
        channels: list[str],
        severity: float,
        opened_at: str,
        bundle,                       # DiagnosisBundle
        context: str,
        stage: str = "open",          # open | escalate | update
        previous: str | None = None,  # diagnóstico anterior (stage=update)
    ) -> str:
        """
        Redacta el diagnóstico de un incidente a partir del bundle
        determinista. No recalcula: sólo redacta sobre lo que ya
        está calculado.
        """
        from agent.report_renderer import ReportDraft

        label = incident_label(incident_kind)
        if stage == "update":
            titulo = (f"Actualización del diagnóstico: {label}"
                      if previous else f"Incidente: {label} (con diagnóstico)")
        elif stage == "escalate":
            titulo = f"Incidente que empeora: {label}"
        else:
            titulo = f"Incidente: {label}"
        extra = ([f"Diagnóstico anterior: {previous}"]
                 if stage == "update" and previous else [])
        fallback = ReportDraft(
            titulo=titulo,
            severidad=_severity_label(severity),
            hechos=[
                f"Canales: {format_channels(channels)}",
                f"Tipo de incidente: {label}",
                f"Severidad máxima: {severity:.2f}",
                f"Desde: {opened_at}",
                *extra,
                f"Hipótesis principal: {bundle.principal}",
                f"Confianza: {bundle.confianza:.2f}",
            ],
            hipotesis=[
                h.nombre + ": " + ("; ".join(h.a_favor[:2]) or "sin evidencia")
                for h in bundle.hipotesis[:2]
            ] + (
                [f"Datos que faltarían: {bundle.hipotesis[0].faltan[0]}"]
                if bundle.hipotesis and bundle.hipotesis[0].faltan else []
            ),
            accion=(
                "Verificar el canal en el próximo turno."
                if bundle.provisional
                else "Revisar la causa indicada por la hipótesis principal."
            ),
        )

        user_prompt = (
            "TIPO DE MENSAJE: DIAGNÓSTICO DE INCIDENTE\n\n"
            f"incident_kind: {incident_kind}\n"
            f"etapa: {stage}\n"
            + (f"diagnostico_anterior: {previous}\n" if previous else "")
            + f"channels: {', '.join(channels)}\n"
            f"severity_max: {severity:.2f}\n"
            f"opened_at: {opened_at}\n\n"
            "PAQUETE DE DIAGNÓSTICO (ya calculado, no recalcular):\n"
            f"{bundle.to_context()}\n\n"
            f"CONTEXTO OPERATIVO\n==================\n{context}\n\n"
            "Redacta el JSON. Cierra con la línea literal "
            "'hipotesis_principal: <nombre>'."
        )

        return self._render_with_llm_or_fallback(
            user_prompt, fallback, context,
        )
    def forget_user(self, user_key: str) -> int:
        """Borra la memoria de conversación de un usuario."""
        if self.conversations is None or not user_key:
            return 0
        return self.conversations.forget(user_key)

    def _alias_block(self, known: list[str]) -> str:
        aliases = self.channel_aliases or {
            "temperatura, producto, cámara": "Temp Producto",
            "evaporador": "Temp Evaporador",
        }
        lines = [
            f'- "{words}" -> usa "{chan}"'
            for words, chan in aliases.items()
            if chan in known
        ]
        if not lines:
            return ""
        return "ALIAS DE CANALES:\n" + "\n".join(lines)

    def answer_question(
        self,
        question: str,
        user_key: str | None = None,
        user_name: str = "",
    ) -> "Optional[str | dict]":
        """
        Responde una pregunta utilizando las herramientas
        de análisis disponibles.

        Los canales disponibles se inyectan dinámicamente
        en el prompt para reducir alucinaciones de nombres.
        """

        if not question or not question.strip():
            return None

        question = question.strip()
        routed = self._route_question(question)
        if routed is not None:
            if self.conversations is not None and user_key:
                self.conversations.record(
                    user_key, question,
                    routed["caption"] if isinstance(routed, dict) else routed, user_name=user_name
                )
            return routed
        try:
            # ----------------------------------------------------
            # Obtener canales reales del SeriesStore
            # ----------------------------------------------------

            known = self.store.channels(
                device_id=self._device_id()
            )

            if known:
                channels_line = (
                    "Canales disponibles: "
                    + ", ".join(
                        f"'{channel}'"
                        for channel in known
                    )
                    + "."
                )
            else:
                channels_line = (
                    "No hay canales registrados todavía."
                )

            # ----------------------------------------------------
            # Construir prompt dinámico
            # ----------------------------------------------------

            system = (
                BASE_PROMPT
                + "\n"
                + QUESTION_PROMPT
                + "\n\n"
                + channels_line
            )

            if not self._has_multipower():
                system += ("\n\nHARDWARE: Esta instalación SOLO tiene controlador (TC-900E). No hay Multipower, "
                           "ni potencia, ni tensión, ni energía. No menciones M1/M2.")

            alias = self._alias_block(known)
            if alias:
                system += "\n\n" + alias

            # ----------------------------------------------------
            # Memoria de conversación del usuario
            # ----------------------------------------------------

            self._current_user = user_key
            if self.conversations is not None and user_key:
                ctx = self.conversations.context_block(user_key)
                if ctx:
                    system += "\n\n" + ctx

            # ----------------------------------------------------
            # Entregar control de tools al LLM
            # ----------------------------------------------------

            # Si el modelo usa una herramienta de INFORME, el texto del informe se entrega
            # tal cual (determinista): el modelo no vuelve a redactar ni reinterpreta cifras.
            _REPORT_TOOLS = {"status_report", "temperature_report", "load_report",
                             "forecast_temperature", "explain_temperature", "controller_summary",
                             "history_coverage", "get_episodes", "episode_summary", "data_quality",
                             "data_quality_canal", "aggregate", "export_csv"}
            used: list[str] = []
            photo_result: list = [None]          # si el LLM llama render_chart, la foto se entrega tal cual

            def _exec(name, args):
                res = self._execute_tool(name, args)
                if name == "render_chart" and isinstance(res, dict) and res.get("ok"):
                    photo_result[0] = {"__photo__": res["photo_path"], "caption": res["caption"]}
                if (name in _REPORT_TOOLS and isinstance(res, dict) and res.get("ok")
                        and res.get("respuesta_base")):
                    used.append(str(res["respuesta_base"]))
                return res

            answer = self.llm.generate_with_tools(
                system=system,
                user=question,
                tools=self.TOOLS,
                tool_executor=_exec,
            )
            if photo_result[0] is not None:
                return photo_result[0]
            if used:
                answer = "\n\n".join(dict.fromkeys(used))
            answer = strip_fake_media(answer)
            if answer and ('"arguments"' in answer or "CallCheck" in answer):
                answer = (
                    "No logré consultar los datos para esa pregunta. Prueba con: "
                    "«estado actual de la cava», «correlación con el compresor», "
                    "«mayores variaciones de temperatura» o «qué canales tienen "
                    "datos suficientes»."
                )

            if answer and self.conversations is not None and user_key:
                self.conversations.record(
                    user_key, question, answer, user_name=user_name
                )
            return answer

        except Exception:

            logger.exception(
                "Error procesando pregunta "
                "con herramientas."
            )

            return None

    # ============================================================
    # ENRUTADOR DETERMINISTA (preguntas típicas)
    # ============================================================
    #
    # Las preguntas frecuentes se resuelven con la herramienta adecuada y
    # un texto armado en código: mismo resultado siempre, sin depender de que
    # el modelo elija bien la herramienta. Lo demás pasa al LLM.

    use_router = True

    @staticmethod
    def _norm(text: str) -> str:
        import unicodedata
        t = unicodedata.normalize("NFD", text.lower())
        return "".join(c for c in t if unicodedata.category(c) != "Mn")

    def _window_minutes(self, q: str, default: float) -> float:
        m = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(horas?|hs?|minutos?|mins?|dias?)\b", q)
        if not m:
            return default
        v = float(m.group(1).replace(",", "."))
        u = m.group(2)
        if u.startswith("h"):
            return v * 60.0
        if u.startswith("d"):
            return v * 1440.0
        return v
    def _route_question(self, question: str) -> str | None:
        if not self.use_router:
            return None
        q = self._norm(question)
        parts: list[str] = []

        # Hora y rango relativos ("ayer", "esta semana"): los usan el bloque de
        # alarmas, el de gráficos y el de episodios. Se calculan aquí una vez.
        now_utc = self._now()
        rng = parse_time_range(q, now_utc, self._tz_name())

        def has(*words):
            return any(w in q for w in words)

        DETAIL_TOOLS = {"status_report", "temperature_report", "load_report", "forecast_temperature"}
        want_detail = has("detalle", "detallado", "detallada", "completo", "completa", "a fondo",
                          "mas informacion", "explicame")

        def _ex(name, args):
            if want_detail and name in DETAIL_TOOLS:
                args = {**args, "detalle": True}
            return self._execute_tool(name, args)

        try:
            fb_reply = self._handle_feedback_message(question)
            if fb_reply is not None:
                return fb_reply
                        # Alarmas de SITRAD: histórico filtrable
            if has("alarma", "alarmas", "ha saltado", "se disparó", "se disparo",
                   "cuántas alarmas", "cuantas alarmas"):
                a0, a1 = (rng[0], rng[1]) if rng else (now_utc - timedelta(days=7), now_utc)
                args: dict = {"desde": a0.isoformat(), "hasta": a1.isoformat()}
                if has("puerta"):
                    args["codigo"] = "door"
                elif has("temperatura", "temp"):
                    args["codigo"] = "temp"
                elif has("presión", "presion"):
                    args["codigo"] = "pressure"
                r = _ex("alarm_history", args)
                parts.append(r.get("respuesta_base") or r.get("error")
                             or "No pude leer el histórico de alarmas.")

            # Correlación de variaciones con alarmas (independiente del anterior)
            if has("coincide", "coinciden", "correlacion con alarmas",
                   "relacion con alarmas", "subidas con alarmas"):
                chs = self._channels_in(q) or ["Temp Cámara"]
                a0, a1 = (rng[0], rng[1]) if rng else (now_utc - timedelta(days=7), now_utc)
                r = _ex("correlate_with_alarms",
                        {"canal": chs[0], "desde": a0.isoformat(), "hasta": a1.isoformat()})
                parts.append(r.get("respuesta_base") or r.get("error")
                             or "No pude cruzar las variaciones con alarmas.")
            if _CHART_ASK.search(q) and not has("tabla"):
                chs = self._channels_in(q) or ["Temp Cámara"]
                args = {"canales": chs, "minutos": self._window_minutes(q, 60.0),
                        "marcar_episodios": has("episodio")}
                if rng and not re.search(r"\d+\s*(horas?|hs?|minutos?|mins?)", q):
                    args.update(desde=rng[0].isoformat(), hasta=rng[1].isoformat())
                res = self._execute_tool("render_chart", args)
                if isinstance(res, dict) and res.get("ok"):
                    return {"__photo__": res["photo_path"], "caption": res["caption"]}
                return CHART_UNAVAILABLE
            if has("que historia tienes", "desde cuando tienes datos", "cuantos datos tienes", "cuanta historia",
                   "desde cuando hay datos"):
                return self._execute_tool("history_coverage", {}).get("respuesta_base")
            if has("calidad de los datos", "calidad de datos", "cobertura", "faltan datos", "hay huecos",
                   "como estan los datos"):
                chs = self._channels_in(q)
                args = {"minutos": self._window_minutes(q, 1440.0)}
                if len(chs) == 1 and has("canal", "del canal", "de la puerta", "del evaporador", "del compresor"):
                    args["canal"] = chs[0]
                r = self._execute_tool("data_quality", args)
                return r.get("respuesta_base") or r.get("error")
            if has("exporta", "exportar", "saca a csv", "dame un csv", " csv"):
                chs = self._channels_in(q) or ["Temp Cámara"]
                a0, a1 = (rng[0], rng[1]) if rng else (now_utc - timedelta(hours=24), now_utc)
                r = self._execute_tool("export_csv", {"canal": chs[0], "desde": a0.isoformat(), "hasta": a1.isoformat()})
                return r.get("respuesta_base") or r.get("error")
            if has("por dia", "por hora", "por semana", "promedio por", "maximo por", "minimo por", "cuantas lecturas"):
                chs = self._channels_in(q) or ["Temp Cámara"]
                agg = ("max" if has("maxim") else "min" if has("minim") else "count" if has("cuantas lecturas")
                       else "sum" if has("suma", "total de") else "p95" if has("p95", "percentil") else "mean")
                grp = "hour" if has("por hora") else "week" if has("por semana") else "day"
                a0, a1 = (rng[0], rng[1]) if rng else (now_utc - timedelta(days=7), now_utc)
                r = self._execute_tool("aggregate", {"canal": chs[0], "desde": a0.isoformat(), "hasta": a1.isoformat(),
                                                     "agg": agg, "group_by": grp})
                return r.get("respuesta_base") or r.get("error")
            if has("episodios"):
                a0, a1 = (rng[0], rng[1]) if rng else (
                    (now_utc - timedelta(days=7), now_utc) if has("semana") else (now_utc - timedelta(hours=24), now_utc))
                tool = "episode_summary" if has("resumen", "estadistica", "por causa") else "get_episodes"
                r = self._execute_tool(tool, {"desde": a0.isoformat(), "hasta": a1.isoformat()})
                return r.get("respuesta_base") or r.get("error")
            if rng and has("como estuvo", "como estuvieron", "como le fue", "como anduvo") and not has("episodio"):
                r = self._execute_tool("temperature_report", {"desde": rng[0].isoformat(), "hasta": rng[1].isoformat()})
                return r.get("respuesta_base") or r.get("error")
            if has("que tan bien aciertas", "cuanto aciertas", "tu acierto", "aciertos", "casos etiquetados",
                   "precision del bot", "como vas acertando", "retroalimentacion", "tu desempeno"):
                book = getattr(self, "case_book", None)
                return book.accuracy_text() if book is not None else "No tengo registro de casos."
            loaded = self._handle_load_message(question)
            if loaded is not None:
                return loaded
            import re as _re0
            _hor = [(float(v.replace(",", ".")), u) for v, u in
                    _re0.findall(r"(\d+(?:[.,]\d+)?)\s*(horas?|h\b|min(?:utos?)?)", q)]
            horizon = sum(v * (60.0 if u.startswith("h") else 1.0) for v, u in _hor) or (
                60.0 if "una hora" in q else 30.0 if "media hora" in q else None)
            if horizon and has("que sucedera", "que pasara", "como estara", "como va a estar",
                               "que va a pasar", "dentro de", "en una hora", "en media hora", "va a estar") \
                    or (horizon and _re0.search(r"\ben\s+\d", q) and has("sucedera", "pasara", "estara")):
                r = _ex("forecast_temperature", {"horizonte_min": horizon})
                parts.append(r.get("respuesta_base") or r.get("error") or "No pude estimar la tendencia.")
            if not parts and has("llegara", "va a llegar", "cuando llega", "proyeccion", "pronostico",
                   "va a seguir subiendo", "hasta cuanto sube", "hasta cuanto va a subir",
                   "que lo provoca", "que lo esta provocando", "que puede estar provocando",
                   "hay riesgo", "esta en riesgo", "se puede danar", "se va a danar",
                   ):
                import re as _re
                mt = _re.search(r"(\d+(?:[.,]\d+)?)\s*(?:grados|°|c\b)", q)
                args = {"objetivo_c": float(mt.group(1).replace(",", "."))} if mt else {}
                r = _ex("forecast_temperature", args)
                parts.append(r.get("respuesta_base") or r.get("error")
                             or "No pude estimar la tendencia.")
            from analysis.product_log import KNOWN_PRODUCTS as _KP
            import re as _re_prod
            _prod = next(
                (p for p in sorted(_KP, key=len, reverse=True)
                if len(p) >= 4 and _re_prod.search(rf"\b{_re_prod.escape(p)}\b", q)),
                None,
            )
            load_q = (has("informe del producto", "informe de ingreso", "informe de carga",
                          "como le fue") or (has("informe") and _prod is not None))
            if load_q:
                r = _ex("load_report", {"producto": _prod} if _prod else {})
                parts.append(r.get("respuesta_base") or r.get("error") or "No pude generar el informe.")
            elif has("cuanto tardo", "cuanto demoro", "cuanto se demoro", "ultimo episodio",
                   "ultima vez que subio", "volvio a 4", "volvio al limite"):
                r = _ex("temperature_report", {"episodio": "ultimo"})
                parts.append(r.get("respuesta_base") or r.get("error")
                             or "No pude generar el informe.")
            elif has("informe", "como estuvo", "como va la cava", "como esta la cava",
                     "cumpl", "resumen del dia", "resumen de hoy", "resumen del turno",
                     "tiempo dentro", "tiempo fuera"):
                r = _ex("temperature_report",
                                       {"minutos": self._window_minutes(q, 1440.0)})
                parts.append(r.get("respuesta_base") or r.get("error")
                             or "No pude generar el informe.")
            if has("por que sube", "por que esta subiendo", "por que subio",
                   "por que aumenta", "por que aumento", "por que se calienta",
                   "por que esta alta", "por que esta caliente", "por que baja",
                   "coincide con el consumo", "coincide con la energia",
                   "coincide con el consumo electrico", "es normal", "es habitual",
                   "se desvia", "causa de la subida", "por que la temperatura"):
                r = _ex(
                    "explain_temperature",
                    {"minutos": self._window_minutes(q, 120.0)})
                parts.append(r.get("respuesta_base") or r.get("error")
                             or "No pude evaluar la temperatura.")
            if has("variacion", "mayores cambios", "picos", "cuanto subio", "cuanto bajo"):
                n = 3
                m = re.search(r"\b(\d+)\s+(?:mayores|principales)", q)
                if m:
                    n = int(m.group(1))
                elif "cinco" in q:
                    n = 5
                elif "dos" in q:
                    n = 2
                r = _ex("top_variations", {
                    "canal": "Temp Cámara", "n": n,
                    "minutos": self._window_minutes(q, 1440.0)})
                parts.append(self._render_variations(r))
            if has("correlacion", "relacion con el compresor", "compresor responde",
                   "corresponde al compresor", "con el compresor"):
                r = _ex("compressor_relation", {"minutos": 240})
                txt = self._render_relation(r)
                if getattr(self, "scope_controller_only", False):
                    keep = [l for l in txt.splitlines() if l.lstrip("• ").startswith("Temp")]
                    txt = ("Relación del compresor (orden REFR) con la temperatura "
                           "(últimos 240 min, sin deshielo):\n" + "\n".join(keep) +
                           "\nEs evidencia de comportamiento, no una causa.") if keep else \
                          "Aún no hay datos suficientes para relacionar el compresor con la temperatura."
                parts.append(txt)
            if has("datos suficientes", "datos historicos", "historia suficiente",
                   "suficientes datos", "comportamiento habitual", "cuanta historia",
                   "antiguedad de los datos"):
                r = _ex("data_coverage", {})
                txt = self._render_coverage(r)
                if getattr(self, "scope_controller_only", False):
                    txt = "\n".join(l for l in txt.splitlines() if "Multipower" not in l)
                parts.append(txt)
            if has("estado de los instrumentos", "instrumentos funcionando",
                   "instrumentos conectados", "comunicacion de los instrumentos",
                   "estan conectados", "estado de la comunicacion"):
                r = _ex("diagnose_now", {"minutos": 360})
                parts.append(self._render_instrument_status(r))
            if has("convertidor", "convertidores", "que hardware", "que equipos",
                "que hay conectado", "que instrumentos", "que canales hay",
                "que canales tiene", "que variables", "cuales instrumentos",
                "instrumentos tiene", "que mide cada"):
                r = _ex("list_converters", {"refresh": has("ahora", "actualiza", "refresh", "vuelve a")})
                txt = r.get("respuesta_base") if r.get("ok") else (r.get("error") or "")
                if not txt:
                    txt = self._render_instruments()   # fallback al método actual
                parts.append(txt)
            if (has("temperatura actual", "temperatura de la cava", "temperatura de la camara")
                    and not parts):
                r = _ex("status_report", {"breve": True})
                parts.append(r.get("respuesta_base") or r.get("error") or "Sin lecturas.")
            elif has("temperatura actual") and parts:
                r = _ex("status_report", {"breve": True})
                parts.insert(0, r.get("respuesta_base") or "")
            if not parts and has("estado actual", "como esta la cava", "como esta todo",
                                 "hay algun problema", "que esta pasando", "hay fallas",
                                 "esta todo bien", "como va la cava", "estado de la cava",
                                 "estado general", "como esta la camara", "estado de la camara"):
                r = _ex("status_report", {})
                parts.append(r.get("respuesta_base") or r.get("error") or "No pude evaluar el estado.")
            if not parts and has("que productos", "productos conoces", "regulaciones", "regulacion de",
                                 "reglas de producto", "que limites", "limite del", "limite de la",
                                 "cual es el limite", "que regulacion"):
                r = _ex("controller_summary", {"tema": "catalogo", "producto": _prod})
                parts.append(r.get("respuesta_base") or r.get("error") or "Sin datos.")
            if not parts and has("alarma"):
                from core.clock import local_tz
                if not getattr(self, "sitrad_live", False):
                    parts.append("Ahora no estoy leyendo la API de SITRAD (modo demostración o archivo), "
                                 "así que no tengo sus alarmas.")
                else:
                    al = list(getattr(self, "sitrad_alarms", []))[-5:]
                    tz = local_tz(self._tz_name())
                    if not al:
                        parts.append("SITRAD no ha registrado alarmas desde que estoy conectado.")
                    else:
                        parts.append("Últimas alarmas de SITRAD:\n" + "\n".join(
                            "• " + (x["when"].astimezone(tz).strftime("%d/%m %H:%M") + " — " if x.get("when") else "")
                            + x["text"] + (f" ({x['instrument']})" if x.get("instrument") else "") for x in al))
            if not parts and has("hay algun producto", "hay producto", "que producto", "cuanto producto",
                                 "que hay en la camara", "que hay en la cava", "que tengo", "tengo producto",
                                 "hay carga", "que carga", "producto en la cava", "producto en la camara",
                                 "hay carne", "hay ternera", "hay pollo"):
                r = _ex("controller_summary", {"tema": "producto"})
                parts.append(r.get("respuesta_base") or r.get("error") or "No pude consultar el producto.")
            if not parts and has("estado del producto", "como esta el producto", "el producto",
                                 "la carne", "la ternera"):
                r = _ex("forecast_temperature", {})
                parts.append(r.get("respuesta_base") or r.get("error") or "No pude evaluar el producto.")
            for _words, _tema in ((("puerta",), "puerta"), (("compresor",), "compresor"),
                                  (("deshielo", "desescarche"), "deshielo"),
                                  (("setpoint", "punto de ajuste", "consigna"), "setpoint")):
                if not parts and has(*_words):
                    r = _ex("controller_summary",
                                           {"tema": _tema, "minutos": self._window_minutes(q, 1440.0)})
                    parts.append(r.get("respuesta_base") or r.get("error") or "Sin datos.")
            if not parts and (has("producto", "la carga") or _prod is not None):
                r = _ex("controller_summary", {"tema": "producto"})
                parts.append(r.get("respuesta_base") or r.get("error") or "No pude consultar el producto.")
            if not parts and (getattr(self, "scope_controller_only", False) or not self._has_multipower()) and has(
                    "energia", "consumo", "kwh", "kilovatio", "potencia", "electric", "tension", "voltaje",
                    "multipower", "medidor"):
                parts.append("No tengo medidor de energía en esta instalación: solo el controlador de la cámara "
                             "(temperatura, compresor, deshielo, puerta). No puedo informar consumo, potencia ni tensión.")
        except Exception:
            logger.exception("Falla en el enrutador; se delega al LLM.")
            return None
        parts = [p for p in parts if p]
        return "\n\n".join(parts) if parts else None

    # ---------------- plantillas de respuesta ----------------

    @staticmethod
    def _render_temperature(r: dict) -> str:
        """Reservado: hoy nadie lo invoca (status_report cubre «temperatura actual»)."""
        d = r.get("datos_clave") or {}
        if "temp_actual_c" not in d:
            return "No tengo lecturas recientes de Temp Cámara."
        return (f"Temp Cámara: {d['temp_actual_c']} °C (setpoint {d.get('setpoint_c')} °C, "
                f"tendencia {d.get('tendencia_c_h')} °C/h).")

    @staticmethod
    def _render_relation(r: dict) -> str:
        if not r.get("ok"):
            return r.get("error", "No pude calcular la relación con REFR.")
        lines = ["Relación de la orden de refrigeración (REFR) con los medidores "
                 f"(últimos {r.get('minutos_analizados', 240):.0f} min, sin deshielo):"]
        lines += [f"• {x}" for x in r.get("interpretacion", [])]
        if not r.get("interpretacion"):
            lines.append("• Datos insuficientes con REFR activo e inactivo.")
        lines.append(r.get("nota", ""))
        return "\n".join(l for l in lines if l)

    @staticmethod
    def _render_coverage(r: dict) -> str:
        if not r.get("ok"):
            return "No pude evaluar la cobertura de datos."
        lines = [f"{r['criterio']} Historia disponible: "
                 f"{r['historia_disponible_horas']:.1f} h."]
        for inst, g in r["por_instrumento"].items():
            line = (f"• {inst.capitalize()}: {g['suficientes']} de {g['canales']} canales "
                    f"con datos suficientes (entre {g['horas_min']:.1f} y "
                    f"{g['horas_max']:.1f} h).")
            if g["insuficientes"]:
                line += " Aún sin historia: " + ", ".join(g["insuficientes"][:6]) + "."
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _render_variations(r: dict) -> str:
        if not r.get("ok"):
            return r.get("error", "No pude calcular las variaciones.")
        lines = [f"Mayores variaciones de {r['canal']} "
                 f"(hay {r['cobertura_horas']:.1f} h de datos, hora local):"]
        if r.get("advertencia"):
            lines.append(f"Aviso: {r['advertencia']}")
        for i, v in enumerate(r["variaciones"], 1):
            lines.append(
                f"{i}. {v['tipo'].capitalize()} de {v['valor_inicial']} a {v['valor_final']} °C "
                f"({v['diferencia']:+.1f} °C) en {v['duracion_min']:.0f} min, "
                f"{v['inicio_local']} → {v['fin_local']}: {v['contexto']}.")
        return "\n".join(lines)

    def _render_instruments(self) -> str:
        from core.channel_catalog import CONTROLLER_SPECS
        if not self._has_multipower():
            try:
                present = set(self.store.channels(device_id=self._device_id()))
            except Exception:
                present = set()
            names = [sp.name for sp in CONTROLLER_SPECS if not present or sp.name in present]
            return (f"Tengo un solo instrumento: el controlador de la cámara ({len(names)} canales): "
                    f"{', '.join(names)}.\n"
                    "No hay medidor eléctrico (Multipower): sin potencia, tensión ni energía.")
        ctrl = ", ".join(sp.name for sp in CONTROLLER_SPECS)
        return (
            "Tengo dos instrumentos:\n"
            f"• Controlador (7 canales): {ctrl}.\n"
            "• Multipower (30 canales): dos medidores, M1 y M2, con 15 variables cada uno "
            "(tensión y corriente por fase, potencia activa, reactiva y aparente, factor de "
            "potencia, frecuencia, simetrías de tensión y corriente, demanda activa y energía "
            "activa).\n"
            "No hay temperatura de producto."
        )
    @staticmethod
    def _render_instrument_status(r: dict) -> str:
        d = r.get("datos_clave") or {}
        lines = []
        if d.get("equipos_con_datos"):
            lines.append(f"Reportando: {d['equipos_con_datos']}.")
        if d.get("equipos_sin_datos"):
            lines.append(f"Sin datos: {d['equipos_sin_datos']}.")
        elif d.get("canales_sin_datos"):
            lines.append(f"Canales sin datos: {d['canales_sin_datos']}.")
        if not lines:
            lines.append("Sin lecturas recientes.")
        return "Estado de los instrumentos: " + " ".join(lines)
    # ============================================================
    # CONSTRUCCIÓN DE PROMPT DE EVENTO
    # ============================================================


    # ============================================================
    # LLM
    # ============================================================

    def _generate(
        self,
        system: str,
        user: str,
    ) -> Optional[str]:

        if not hasattr(
            self.llm,
            "generate_text",
        ):

            logger.error(
                "El cliente LLM no implementa "
                "generate_text()."
            )

            return None

        try:

            response = self.llm.generate_text(
                system=system,
                user=user,
            )

        except Exception:

            logger.exception(
                "Fallo al generar respuesta con el LLM."
            )

            return None

        if response is None:
            return None

        response = str(
            response
        ).strip()

        return response or None

    # ============================================================
    # EJECUTOR DE HERRAMIENTAS
    # ============================================================
    def _execute_tool(self, name, arguments):
        arguments = self._normalize_arguments(name, arguments)
        result = self._execute_tool_raw(name, arguments)
        try:
            if isinstance(result, dict) and result.get("ok"):
                chans = []
                if name in {"get_stats", "get_slope", "energy"}:
                    chans = [arguments.get("canal")]
                elif name == "correlate":
                    chans = [arguments.get("a"), arguments.get("b")]
                result.update(self._freshness([c for c in chans if c]))
        except Exception:
            logger.exception("No se pudo calcular la antigüedad de datos.")
        return result

    # ============================================================
    # NORMALIZACIÓN DE ARGUMENTOS
    # ============================================================

    MIN_CORRELATE_MINUTES = 30.0
    PROFILE_LEARNING_HOURS = 2.0
    def _normalize_arguments(self, name: str, arguments: dict) -> dict:
        """
        - Acepta `horas` como alternativa a `minutos` (el modelo confunde
          "24 horas" con 24 minutos).
        - La correlación necesita muchos puntos: ventana mínima de 30 min.
        """
        if not isinstance(arguments, dict):
            return arguments
        args = dict(arguments)
        if "horas" in args and "minutos" not in args:
            try:
                args["minutos"] = float(args.pop("horas")) * 60.0
            except (TypeError, ValueError):
                args.pop("horas", None)
        if name == "correlate" and "minutos" in args:
            try:
                if float(args["minutos"]) < self.MIN_CORRELATE_MINUTES:
                    args["minutos"] = self.MIN_CORRELATE_MINUTES
                    args["_ventana_ajustada"] = True
            except (TypeError, ValueError):
                pass
        return args

    # ============================================================
    # HERRAMIENTA: data_coverage
    # ============================================================
    # ------------------------------------------------------------------
    # Producto en la cámara y proyección
    # ------------------------------------------------------------------

    def _tz_name(self) -> str:
        c = getattr(self, "cooling_cfg", None)
        return getattr(c, "tz_name", "America/Bogota")

    def _active_loads(self, now):
        log = getattr(self, "product_log", None)
        return log.active(now) if log is not None else []

    def _cfg_now(self):
        """Configuración vigente: límite del producto más estricto presente + reglas del catálogo."""
        from dataclasses import replace
        from analysis.cooling_report import CoolingConfig
        base = getattr(self, "cooling_cfg", None) or CoolingConfig()
        log = getattr(self, "product_log", None)
        if log is None:
            return base
        lim, _ = log.effective_limit(self._now_utc(), base.limit_c)
        return replace(base, limit_c=lim, product_rules={**base.product_rules, **log.rules_map()})

    def _all_loads(self):
        log = getattr(self, "product_log", None)
        return list(log.loads) if log is not None else []

    def _now_utc(self):
        return self._clock.now()

    def _now(self) -> datetime:
        """Hora actual del agente: el reloj de la simulación si existe; si no, la real (UTC)."""
        return self._clock.now() if self._clock is not None else datetime.now(timezone.utc)

    def _handle_feedback_message(self, question: str):
        """El ingeniero corrige una hipótesis del bot ("el de las 06:17 fue la puerta"). None = no es una corrección."""
        from analysis.feedback_log import REAL_LABEL, bot_label, parse_feedback
        book = getattr(self, "case_book", None)
        if book is None:
            return None
        log = getattr(self, "product_log", None)
        fb = parse_feedback(question, log.known_names() if log else ())
        if fb is None:
            return None
        now = self._now_utc()
        case = None
        if fb["hhmm"]:
            case = book.find(fb["hhmm"], now)
            if case is None:
                h, m = fb["hhmm"]
                rec = book.recent(4)
                lst = "; ".join(f"{book._local(c['start']).strftime('%H:%M')} ({bot_label(c['bot_kind'])})"
                                for c in rec)
                return (f"No encuentro un episodio ni un aviso cerca de las {h:02d}:{m:02d}."
                        + (f" Los últimos que tengo: {lst}." if lst else " Todavía no he registrado ninguno."))
        else:
            ref = getattr(self, "_fb_ref", None)
            if ref and (now - ref[1]).total_seconds() <= 15 * 60 and not fb["last"]:
                case = book.get(ref[0])
            case = case or book.last(now)
            if case is None:
                return "Todavía no he registrado ningún episodio ni aviso que corregir."
        self._fb_ref = (case["id"], now)
        user = getattr(question, "user_name", "") or ""
        book.add_feedback(case["id"], fb["real"], fb["rejected"], fb["verdict"], str(question), user)
        v = book.verdict(case["id"])
        hm = book._local(case["start"]).strftime("%H:%M")
        tipo = "episodio" if case.get("type") == "episodio" else "aviso"
        real = fb["real"]
        bot = bot_label(case["bot_kind"])
        head = f"Anotado — {tipo} de las {hm}"
        if real:
            head += f": fue {REAL_LABEL[real]}."
        elif fb["verdict"] == "right":
            head += ": confirmo mi hipótesis."
        else:
            head += "."
        tail = {"acerto": f"Yo había dicho: {bot} ✅",
                "fallo": f"Yo había dicho: {bot} ❌",
                "falsa_alarma": f"Yo había dicho: {bot} → era falsa alarma ⚠️",
                "sin_hipotesis": f"No tenía una hipótesis clara (decía: {bot}) ℹ️",
                "sin_causa_real": f"No acerté (dije: {bot}). ¿Cuál fue la causa real? Dime, por ejemplo, "
                                  "\"fue la puerta\"."}[v]
        out = [head, tail]
        if real == "ingreso_producto" and log is not None:
            t0 = datetime.fromisoformat(case["start"])
            if not any(abs((l.t_in - t0).total_seconds()) <= 90 * 60 for l in log.loads):
                out.append(f"Si quieres que siga ese producto, dime por ejemplo: \"hay 500 kg de ternera desde "
                           f"las {hm}\".")
        n = book.accuracy()["etiquetados"]
        out.append(f"({n} caso(s) etiquetado(s); meta ~20)")
        return "\n".join(out)

    def _handle_load_message(self, question: str):
        """Registra producto y/o su regulación, o lo retira. None = el mensaje no es de esto."""
        from analysis.product_log import (
            ICON, assess_loads, parse_message, parse_removal, render_load_line)
        from core.clock import local_tz
        log = getattr(self, "product_log", None)
        if log is None:
            return None
        now = self._now_utc()
        known = log.known_names()
        is_out, prod = parse_removal(question, known)
        if is_out:
            gone = log.remove(now, prod)
            if not gone:
                return "No tengo producto registrado en la cámara" + (f" de {prod}." if prod else ".")
            return "Listo, retiré: " + ", ".join(l.label() for l in gone) + ".\n\n" + "\n\n".join(
                self._tool_load_report({"producto": l.product})["respuesta_base"] for l in gone)
        data = parse_message(question, now, self._tz_name(), known)
        if data is None:
            return None
        tz = local_tz(self._tz_name())
        out = []
        if data["max_temp_c"] is not None:
            rule = log.set_rule(data["product"], max_temp_c=data["max_temp_c"],
                                max_minutes_above=data["max_minutes_above"])
            tol = ("sin tolerancia" if (rule.get("max_minutes_above") or 0) <= 0
                   else f"tolerancia {int(rule['max_minutes_above'])} min")
            out.append(f"Anotado: {log.canonical(data['product'])} no debe pasar de "
                       f"{str(data['max_temp_c']).rstrip('0').rstrip('.').replace('.', ',')} °C ({tol}).")
        if data["kind"] in ("load", "both"):
            load = log.add(data["product"], data["kg"], data["t_in"], data["entry_temp_c"])
            hm = load.t_in.astimezone(tz).strftime("%d/%m %H:%M")
            out.append(f"Registrado: {load.label()}, entró {hm}"
                       + (f" a {str(load.entry_temp_c).replace('.', ',')} °C." if load.entry_temp_c is not None else "."))
            cfg = self._cfg_now()
            lim, who = log.effective_limit(now, cfg.limit_c)
            if load.t_in > now:
                out.append("⚠️ Esa hora de entrada es posterior a ahora: empezaré a contar cuando llegue.")
            out.append(f"Límite que vigilo ahora: {str(lim).rstrip('0').rstrip('.').replace('.', ',')} °C"
                       + (f" (el más estricto: {who})." if who else " (el de la cámara)."))
            dev = self._device_id()
            from analysis.diagnosis import DEFAULT_CFG
            temp = self.store.get_series(dev, DEFAULT_CFG["ch_temp"], minutes=24 * 60.0)
            for r in assess_loads([load], temp, cfg.limit_c, now, cfg.product_rules):
                out.append(f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name))
        else:
            out.append("Aplica a cualquier ingreso de ese producto.")
        return "\n".join(out)

    def _tool_controller_summary(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from analysis.controller_summary import (
            compressor_summary, defrost_summary, door_summary, loads_summary, setpoint_summary)
        from analysis.diagnosis import DEFAULT_CFG

        cfg = self._cfg_now()
        dev = self._device_id()
        now = self._now_utc()
        tema = str(arguments.get("tema", "")).lower()
        minutes = float(arguments.get("minutos", 1440.0) or 1440.0)
        g = lambda k: self.store.get_series(dev, DEFAULT_CFG[k], minutes=max(minutes, 180.0))
        if tema.startswith("puerta"):
            txt = door_summary(g("ch_door"), now, minutes, cfg)
        elif tema.startswith("compres"):
            txt = compressor_summary(g("ch_refr"), now, minutes, cfg)
        elif tema.startswith("deshielo"):
            txt = defrost_summary(g("ch_defr"), now, minutes, cfg)
        elif tema.startswith("setpoint"):
            txt = setpoint_summary(g("ch_setpoint"), g("ch_temp"), now, minutes, cfg)
        elif tema.startswith("producto"):
            txt = loads_summary(self._active_loads(now), g("ch_temp"), now, cfg)
        elif tema.startswith("catalogo"):
            from analysis.product_log import catalog_summary, rule_for, _dur as _pd
            log = getattr(self, "product_log", None)
            prod = arguments.get("producto")
            if log is None:
                txt = "No tengo catálogo de productos."
            elif prod:
                rule = rule_for(prod, log.rules_map())
                if not rule or rule.get("max_temp_c") is None:
                    txt = (f"No tengo regulación para {prod}. Dime, por ejemplo: "
                           f"\"el {prod} no debe subir de 12 grados\".")
                else:
                    m = rule.get("max_minutes_above")
                    tol = ("sin plazo configurado" if m is None else "sin tolerancia" if m <= 0
                           else f"tolerancia {_pd(m)}")
                    txt = f"{prod}: no debe pasar de {str(rule['max_temp_c']).rstrip('0').rstrip('.').replace('.', ',')} °C ({tol})."
            else:
                txt = catalog_summary(log)
        else:
            return {"ok": False, "error": "tema debe ser puerta, compresor, deshielo, setpoint, producto o catalogo."}
        return {"ok": True, "respuesta_base": txt}

    def _loads_for_report(self, product: str | None, now):
        """Cargas activas, o retiradas en las últimas 24 h; filtradas por producto si se pide."""
        from analysis.product_log import _norm
        out = []
        for l in self._all_loads():
            if l.t_out is not None and (now - l.t_out).total_seconds() > 24 * 3600:
                continue
            if product and not (_norm(product) in _norm(l.product) or _norm(l.product) in _norm(product)):
                continue
            out.append(l)
        return out

    def _tool_load_report(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from analysis.diagnosis import DEFAULT_CFG
        from analysis.load_report import render_load_report

        cfg = self._cfg_now()
        dev = self._device_id()
        now = self._now_utc()
        loads = self._loads_for_report(arguments.get("producto"), now)
        if not loads:
            return {"ok": True, "respuesta_base": "No tengo producto registrado para hacer ese informe. "
                    "Dime, por ejemplo: \"hay 500 kg de ternera desde hace 20 minutos\"."}
        g = lambda k: self.store.get_series(dev, DEFAULT_CFG[k], minutes=24 * 60.0)
        temp, refr, door, dfr = g("ch_temp"), g("ch_refr"), g("ch_door"), g("ch_defr")
        txt = "\n\n".join(render_load_report(l, temp, refr, cfg, now, door, dfr, detail=bool(arguments.get("detalle"))) for l in loads)
        return {"ok": True, "respuesta_base": txt}

    def _tool_status_report(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from analysis.diagnosis import DEFAULT_CFG
        from analysis.status_report import render_status

        cfg = self._cfg_now()
        dev = self._device_id()
        now = self._now_utc()
        if DEFAULT_CFG["ch_temp"] not in set(self.store.channels(device_id=dev)):
            return {"ok": False, "error": "No hay datos de la termocupla."}
        g = lambda ch: self.store.get_series(dev, DEFAULT_CFG[ch], minutes=24 * 60.0)
        log = getattr(self, "product_log", None)
        who = log.effective_limit(now, (getattr(self, "cooling_cfg", None) or cfg).limit_c)[1] if log else None
        text = render_status(g("ch_temp"), g("ch_door"), g("ch_defr"), g("ch_refr"),
                             g("ch_setpoint"), cfg, now, self._active_loads(now),
                             brief=bool(arguments.get("breve")), detail=bool(arguments.get("detalle")),
                             limit_who=who)
        return {"ok": True, "respuesta_base": text}

    def _tool_forecast_temperature(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Hacia dónde va la temperatura, causa probable y riesgo del producto. Conciso por defecto."""
        from datetime import timedelta
        from analysis.cooling_report import _dur, _n
        from analysis.diagnosis import DEFAULT_CFG
        from analysis.forecast import despike, fit_trend
        from analysis.product_log import ICON, assess_loads, render_load_line
        from analysis.rise_watch import possible_causes, usual_max

        cfg = self._cfg_now()
        detail = bool(arguments.get("detalle"))
        dev = self._device_id()
        now = self._now_utc()
        ch_t = DEFAULT_CFG["ch_temp"]
        if ch_t not in set(self.store.channels(device_id=dev)):
            return {"ok": False, "error": f"No hay datos del canal de temperatura ({ch_t})."}
        g = lambda ch: self.store.get_series(dev, ch, minutes=24 * 60.0)
        temp, door, dfr, refr = g(ch_t), g(DEFAULT_CFG["ch_door"]), g(DEFAULT_CFG["ch_defr"]), g(DEFAULT_CFG["ch_refr"])
        window = float(arguments.get("minutos", cfg.trend_window_min) or cfg.trend_window_min)
        tr = fit_trend(temp, window_min=max(10.0, min(window, 60.0)), now=now)
        loads = self._active_loads(now)
        if tr is None:
            return {"ok": False, "error": "No hay lecturas recientes suficientes para estimar la tendencia."}
        h = arguments.get("horizonte_min")
        h = float(h) if h else None
        crit = cfg.critical_c if cfg.critical_c is not None else cfg.limit_c + 4.0
        usual = usual_max(temp, cfg.limit_c)
        rising = tr.slope_c_h >= 0.5 and tr.r2 >= 0.5
        swing = rising and tr.temp_now < cfg.limit_c and usual is not None and tr.temp_now < usual + 0.3
        falling = tr.slope_c_h <= -0.5 and tr.r2 >= 0.5
        if rising and not swing:
            tend = f"subiendo +{_n(tr.slope_c_h)} °C/h"
        elif swing:
            tend = f"sube entre ciclos del compresor (suele llegar a ~{_n(usual)} °C): normal"
        elif falling:
            tend = f"bajando {_n(abs(tr.slope_c_h))} °C/h"
        else:
            tend = "estable"
        lines = [f"{_n(tr.temp_now)} °C ahora ({'límite ' + _n(cfg.limit_c, 0)} °C) — {tend}."]
        if rising and not swing:
            ask = arguments.get("objetivo_c")
            proj, passed = [], []
            for target in ([float(ask)] if ask is not None else [cfg.limit_c, crit]):
                m = tr.minutes_to(target)
                if m is not None:
                    proj.append(f"{_n(target, 0)} °C en ~{_dur(m)}")
                elif tr.temp_now >= target:
                    passed.append(_n(target, 0))
            if passed:
                lines.append(f"Ya superó {' y '.join(passed)} °C.")
            if proj:
                lines.append("Si sigue así: " + "; ".join(proj) + " (proyección lineal).")
            if h:
                lines.append(f"En {_dur(h)}, si siguiera igual: ~{_n(tr.temp_now + tr.slope_c_h * h / 60.0)} °C.")
            causes = possible_causes(tr, door, dfr, refr, loads, now, cfg.tz_name, g(DEFAULT_CFG["ch_setpoint"]), cfg.limit_c)
            lines.append("Causa probable: " + "; ".join(causes if detail else causes[:2]) + ".")
        elif h:
            vals = sorted(float(r.value) for r in despike(temp)
                          if r.event_time >= now - timedelta(hours=6) and float(r.value) <= cfg.limit_c)
            if len(vals) >= 20:
                lo, hi = vals[int(0.05 * (len(vals) - 1))], vals[int(0.95 * (len(vals) - 1))]
                lines.append(f"En {_dur(h)}: seguiría oscilando entre {_n(lo)} y {_n(hi)} °C si nada cambia.")
        for r in assess_loads(loads, temp, cfg.limit_c, now, cfg.product_rules):
            lines.append(f"{ICON[r.level]} " + render_load_line(r, cfg.limit_c, cfg.tz_name))
            if h and r.max_minutes is not None and r.max_minutes > 0:
                worst = r.minutes_above + h
                lines.append(f"   Peor caso en {_dur(h)}: {_dur(worst)} sobre su límite → "
                             + ("superaría el máximo" if worst > r.max_minutes else "dentro del máximo")
                             + f" de {_dur(r.max_minutes)}.")
        if not loads:
            lines.append("Sin producto registrado.")
        return {"ok": True, "respuesta_base": "\n".join(lines),
                "datos_clave": {"slope_c_h": round(tr.slope_c_h, 2), "temp_c": round(tr.temp_now, 1)}}

    def _tool_temperature_report(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Informe de temperatura (solo termocupla): periodo o último episodio."""
        from datetime import timedelta
        from analysis.cooling_report import (
            find_excursions, render_episode_report, render_period_report)
        from analysis.diagnosis import DEFAULT_CFG

        cfg = self._cfg_now()
        dev = self._device_id()
        ch_t = DEFAULT_CFG["ch_temp"]
        if ch_t not in set(self.store.channels(device_id=dev)):
            return {"ok": False, "error": f"No hay datos del canal de temperatura ({ch_t})."}
        retention = float(getattr(self.store, "max_hours", 24.0)) * 60.0
        temp = self.store.get_series(dev, ch_t, minutes=retention)
        door = self.store.get_series(dev, DEFAULT_CFG["ch_door"], minutes=retention)
        dfr = self.store.get_series(dev, DEFAULT_CFG["ch_defr"], minutes=retention)
        refr = self.store.get_series(dev, DEFAULT_CFG["ch_refr"], minutes=retention)
        spt = self.store.get_series(dev, DEFAULT_CFG["ch_setpoint"], minutes=retention)
        good = [r for r in temp if r.value is not None]
        if len(good) < 2:
            return {"ok": False, "error": "Sin lecturas suficientes de la termocupla."}
        if str(arguments.get("episodio", "")).lower().startswith("ult"):
            exs = find_excursions(temp, cfg, door, dfr, refr, self._all_loads(), spt)
            if not exs:
                return {"ok": True, "respuesta_base": (
                    f"No hubo episodios sobre {cfg.limit_c:g} °C en los datos disponibles "
                    f"({retention / 60:.0f} h).")}
            return {"ok": True, "respuesta_base": render_episode_report(exs[-1], exs, cfg, temp, detail=bool(arguments.get("detalle")))}
        minutes = float(arguments.get("minutos", 1440.0) or 1440.0)
        d0, d1 = self._parse_dt(arguments.get("desde")), self._parse_dt(arguments.get("hasta"))
        if d0 is not None:
            msg = self._history_guard(desde=d0, hasta=d1)
            if msg:
                return {"ok": True, "respuesta_base": msg}
            t0, t1 = d0, min(d1 or good[-1].event_time, good[-1].event_time)
        else:
            if "minutos" in arguments:
                msg = self._history_guard(minutes=minutes)
                if msg:
                    return {"ok": True, "respuesta_base": msg}
            t1 = good[-1].event_time
            t0 = t1 - timedelta(minutes=minutes)
        return {"ok": True,
                "respuesta_base": render_period_report(temp, t0, t1, cfg, door, dfr, refr, spt,
                                     self._active_loads(t1), detail=bool(arguments.get("detalle")))}

    # ============================================================
    # HERRAMIENTA: render_chart (imagen real, determinista)
    # ============================================================
    def _tool_render_chart(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dibuja uno o varios canales como PNG (matplotlib, backend Agg).

        arguments: {"canales": [..] | "canal": str, "minutos": ventana, "desde"/"hasta": ISO opcionales,
        "marcar_limite": bool=True, "marcar_episodios": bool=False}. Con varios canales: eje izquierdo para
        °C y derecho para estados 0/1. Devuelve {"ok", "photo_path", "caption"} o {"ok": False, "error"}.
        """
        import os
        from zoneinfo import ZoneInfo
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
        except ImportError:
            return {"ok": False, "error": "Falta matplotlib (pip install matplotlib)."}

        dev = self._device_id()
        canales = arguments.get("canales")
        if isinstance(canales, str):
            canales = [canales]
        if not canales:
            canales = [arguments.get("canal") or "Temp Cámara"]
        canales = list(dict.fromkeys(str(c).strip() for c in canales if str(c).strip()))
        try:
            requested = float(arguments.get("minutos") or 60.0)
        except (TypeError, ValueError):
            requested = 60.0
        retention = float(getattr(self.store, "max_hours", 24.0)) * 60.0
        minutes = max(5.0, min(requested, retention))
        ajuste = (f" (pediste {requested:g}, mínimo 5)" if requested < 5.0 else
                  f" (pediste {requested:.0f}, máximo {retention:.0f})" if requested > retention else "")
        known = set(self.store.channels(device_id=dev))
        bad = [c for c in canales if c not in known]
        if bad:
            return {"ok": False, "error": f"No existe el canal '{bad[0]}'. Canales: {', '.join(sorted(known))}."}

        d0, d1 = self._parse_dt(arguments.get("desde")), self._parse_dt(arguments.get("hasta"))
        data: dict[str, list] = {}
        for ch in canales:
            if d0 is not None:
                rs = self.store.get_series(dev, ch, start=d0, end=d1)
            else:
                rs = self.store.get_series(dev, ch, minutes=minutes)
            rs = sorted((r for r in rs if r.value is not None and r.quality.value in ("VALID", "FROZEN")),
                        key=lambda r: r.event_time)
            if len(rs) >= 2:
                data[ch] = rs
        if not data:
            return {"ok": False, "error": f"No hay lecturas suficientes de {', '.join(canales)} en ese periodo."}

        tz = ZoneInfo(CHART_TZ)
        t_first = min(rs[0].event_time for rs in data.values())
        t_last = max(rs[-1].event_time for rs in data.values())
        left = [c for c in data if (data[c][-1].unit or "") != "estado" and (data[c][-1].unit or "") == "°C"]
        right = [c for c in data if c not in left]
        fig, ax = plt.subplots(figsize=(8, 3.8), dpi=110)
        ax2 = ax.twinx() if (left and right) else None
        cycle = plt.rcParams["axes.prop_cycle"].by_key()["color"]
        handles = []
        for i, ch in enumerate(data):
            rs = data[ch]
            xs, ys = [r.event_time.astimezone(tz) for r in rs], [float(r.value) for r in rs]
            on_right = ch in right and ax2 is not None
            target = ax2 if on_right else ax
            kw = {"lw": 1.6, "color": cycle[i % len(cycle)], "label": ch}
            if (rs[-1].unit or "") == "estado":
                h = target.step(xs, ys, where="post", alpha=0.8, **kw)
            else:
                h = target.plot(xs, ys, **kw)
            handles += h
        if not (left or right):
            pass
        ax.set_ylabel("°C" if left else (data[right[0]][-1].unit or ""))
        if ax2 is not None:
            ax2.set_ylabel("estado (0/1)")
            ax2.set_ylim(-0.1, 1.2)
        elif right and (data[right[0]][-1].unit or "") == "estado":
            ax.set_ylim(-0.1, 1.2)
        if arguments.get("marcar_limite", True) and any(c.startswith("Temp") for c in left):
            try:
                limit = float(self._cfg_now().limit_c)
            except Exception:
                limit = TEMP_LIMIT_C
            handles.append(ax.axhline(limit, color="red", ls="--", lw=1, label=f"Límite {limit:g} °C"))
        if arguments.get("marcar_episodios", False):
            hist = getattr(self, "episode_history", None)
            if hist is not None:
                for e in hist.query(t_first, t_last, limit=50):
                    for key, ha in (("start", "left"), ("end", "right")):
                        t = self._parse_dt(e[key]).astimezone(tz)
                        ax.axvline(t, color="gray", ls=":", lw=1)
                    ax.text(self._parse_dt(e["start"]).astimezone(tz), ax.get_ylim()[1],
                            str(e.get("kind_label") or e.get("kind"))[:18], rotation=90, va="top", fontsize=7)
        ax.set_title((", ".join(data) if len(data) > 1 else next(iter(data))) +
                     (f" · últimos {minutes:.0f} min" if d0 is None else ""))
        ax.legend(handles=handles, loc="upper left", fontsize=7)
        ax.grid(alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
        fig.autofmt_xdate()
        fig.tight_layout()
        os.makedirs(CHART_DIR, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in "_".join(data))[:60]
        path = os.path.join(CHART_DIR, f"{safe}_{t_last.astimezone(tz).strftime('%Y%m%d_%H%M%S')}.png")
        fig.savefig(path)
        plt.close(fig)

        def _desc(ch):
            rs = data[ch]
            ys = [float(r.value) for r in rs]
            unit = rs[-1].unit or ""
            if unit == "estado":
                tot = on = 0.0
                for a, b in zip(rs, rs[1:]):
                    d = min((b.event_time - a.event_time).total_seconds(), 900.0)
                    tot += d
                    on += d if float(a.value) >= 0.5 else 0.0
                return f"{ch}: ON {on / tot:.0%} del tiempo" if tot else f"{ch}: sin cobertura"
            max((rs[-1].event_time - rs[0].event_time).total_seconds() / 3600.0, 1e-6)
            xs = [(r.event_time - rs[0].event_time).total_seconds() / 3600.0 for r in rs]
            mx_ = sum(xs) / len(xs)
            den = sum((x - mx_) ** 2 for x in xs) or 1e-9
            slope = sum((x - mx_) * (y - sum(ys) / len(ys)) for x, y in zip(xs, ys)) / den
            trend = "sube" if slope > 0.3 else "baja" if slope < -0.3 else "estable"
            return f"{ch}: {min(ys):.1f} → {max(ys):.1f} {unit} ({trend}, media {sum(ys) / len(ys):.1f})"

        if len(data) == 1:
            ch = next(iter(data))
            ys = [float(r.value) for r in data[ch]]
            unit = data[ch][-1].unit or ""
            caption = (f"{ch} · {minutes:.0f} min{ajuste} · mín {min(ys):.1f} / máx {max(ys):.1f} / "
                       f"media {sum(ys) / len(ys):.1f} {unit}").strip()
        else:
            caption = f"{minutes:.0f} min{ajuste} · " + " · ".join(_desc(c) for c in data)
        return {"ok": True, "photo_path": path, "caption": caption[:1024]}

    # ============================================================
    # HISTORIA DISPONIBLE, EPISODIOS, CALIDAD, AGREGACIÓN, CSV (deterministas)
    # ============================================================
    _CH_WORDS = (("evaporador", "Temp Evaporador"), ("ventilador", "Estado Ventilador"),
                 ("compresor", "Estado Refrigeración"), ("refrigeracion", "Estado Refrigeración"),
                 ("deshielo", "Estado Deshielo"), ("puerta", "Puerta Abierta"),
                 ("setpoint", "Setpoint"), ("temperatura", "Temp Cámara"), ("camara", "Temp Cámara"))

    def _channels_in(self, q: str) -> list[str]:
        """Canales mencionados en una pregunta normalizada, en orden de aparición (sin repetir)."""
        found: list[tuple[int, str]] = []
        for word, ch in self._CH_WORDS:
            i = q.find(word)
            if i >= 0 and all(ch != c for _, c in found):
                found.append((i, ch))
        return [c for _, c in sorted(found)]

    @staticmethod
    def _parse_dt(v):
        """ISO (con o sin zona) o 'AAAA-MM-DD' -> datetime UTC; None si no se puede."""
        if v in (None, ""):
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        try:
            t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    def _local_fmt(self, t: datetime, fmt: str = "%d/%m %H:%M") -> str:
        from core.clock import local_tz
        return t.astimezone(local_tz(self._tz_name())).strftime(fmt)

    def _history_span(self):
        """(desde, hasta, horas, lecturas_por_canal) de lo que hay en el almacén, o None si está vacío."""
        dev = self._device_id()
        retention = float(getattr(self.store, "max_hours", 24.0)) * 60.0
        first = last = None
        per: dict[str, int] = {}
        for ch in self.store.channels(device_id=dev):
            good = [r for r in self.store.get_series(dev, ch, minutes=retention)
                    if r.value is not None and r.quality.value in ("VALID", "FROZEN")]
            per[ch] = len(good)
            if good:
                first = good[0].event_time if first is None else min(first, good[0].event_time)
                last = good[-1].event_time if last is None else max(last, good[-1].event_time)
        if first is None:
            return None
        return first, last, (last - first).total_seconds() / 3600.0, per

    def _history_guard(self, *, minutes=None, desde=None, hasta=None) -> str | None:
        """Texto «solo tengo datos desde…» si la ventana pedida empieza antes de la historia; si no, None."""
        try:
            span = self._history_span()
        except Exception:
            return None
        if span is None:
            return None
        s0, s1, hours, _ = span
        if desde is not None:
            req0, req1 = desde, (hasta or s1)
        elif minutes is not None:
            req1, req0 = s1, s1 - timedelta(minutes=float(minutes))
        else:
            return None
        window = max((req1 - req0).total_seconds() / 60.0, 1.0)
        if req0 < s0 - timedelta(minutes=max(10.0, 0.1 * window)) or req1 < s0:
            return (f"Solo tengo datos desde el {self._local_fmt(s0)} ({hours:.1f} h). "
                    "Lo que pidas antes de eso no puedo dártelo.")
        return None

    def _tool_history_coverage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Desde cuándo hay datos en el almacén y cuántas lecturas válidas por canal."""
        span = self._history_span()
        if span is None:
            return {"ok": True, "respuesta_base": "Todavía no tengo lecturas guardadas."}
        s0, s1, hours, per = span
        top = ", ".join(f"{c}: {n}" for c, n in sorted(per.items(), key=lambda kv: -kv[1])[:6])
        return {"ok": True, "desde": s0.isoformat(), "hasta": s1.isoformat(), "horas": round(hours, 1),
                "por_canal": per,
                "respuesta_base": (f"Tengo datos desde el {self._local_fmt(s0)} hasta el {self._local_fmt(s1)} "
                                   f"({hours:.1f} h). Lecturas válidas por canal: {top}.")}

    def _range_args(self, arguments: dict[str, Any], default_hours: float = 24.0):
        now = self._now()
        d1 = self._parse_dt(arguments.get("hasta")) or now
        d0 = self._parse_dt(arguments.get("desde")) or (d1 - timedelta(hours=default_hours))
        return d0, d1

    def _tool_get_episodes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        hist = getattr(self, "episode_history", None)
        if hist is None:
            return {"ok": False, "error": "No hay historial de episodios disponible."}
        d0, d1 = self._range_args(arguments)
        try:
            limit = int(arguments.get("limit") or 20)
            min_min = arguments.get("min_min")
            min_min = None if min_min in (None, "") else float(min_min)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Parámetros inválidos (limit/min_min)."}
        eps = hist.query(d0, d1, canal=arguments.get("canal") or None, causa=arguments.get("causa") or None,
                         min_min=min_min, limit=limit)
        rango = f"{self._local_fmt(d0)} – {self._local_fmt(d1)}"
        if not eps:
            return {"ok": True, "episodios": [], "resumen": {"n": 0},
                    "respuesta_base": f"No hay episodios sobre el límite registrados entre {rango}."}
        lines = [f"Episodios sobre el límite entre {rango}: {len(eps)}."]
        for e in eps:
            s, f = self._parse_dt(e["start"]), self._parse_dt(e["end"])
            lines.append(f"• {self._local_fmt(s)}–{self._local_fmt(f, '%H:%M')} ({e['above_min']:.0f} min), "
                         f"pico {e['peak_c']:.1f} °C — {e.get('cause') or e.get('kind_label')}")
        summ = hist.summary(d0, d1)
        return {"ok": True, "episodios": eps,
                "resumen": {"n": summ["n"], "por_causa": summ["por_causa"],
                            "duracion_mediana": summ["duracion_mediana"]},
                "respuesta_base": "\n".join(lines)}

    def _tool_episode_summary(self, arguments: dict[str, Any]) -> dict[str, Any]:
        hist = getattr(self, "episode_history", None)
        if hist is None:
            return {"ok": False, "error": "No hay historial de episodios disponible."}
        d0, d1 = self._range_args(arguments, default_hours=168.0)
        s = hist.summary(d0, d1)
        rango = f"{self._local_fmt(d0)} – {self._local_fmt(d1)}"
        if s["n"] == 0:
            return {"ok": True, "resumen": s, "respuesta_base": f"No hubo episodios sobre el límite entre {rango}."}
        causas = ", ".join(f"{k}: {v}" for k, v in sorted(s["por_causa"].items(), key=lambda kv: -kv[1]))
        w = s["peor_episodio"]
        txt = (f"Episodios entre {rango}: {s['n']}. Por causa: {causas}. Duración mediana "
               f"{s['duracion_mediana']:.0f} min; tiempo total sobre el límite {s['tiempo_total_sobre_limite']:.0f} min. "
               f"El peor llegó a {w['peak_c']:.1f} °C el {self._local_fmt(self._parse_dt(w['start']))}.")
        return {"ok": True, "resumen": s, "respuesta_base": txt}

    def _tool_data_quality(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Cobertura, huecos y antigüedad por canal. Con `canal` solo ese canal."""
        from analysis.quality import channel_quality
        dev = self._device_id()
        try:
            minutes = float(arguments.get("minutos") or 1440.0)
        except (TypeError, ValueError):
            minutes = 1440.0
        span = self._history_span()
        if span is None:
            return {"ok": True, "respuesta_base": "Todavía no tengo lecturas guardadas."}
        s0, s1, _, _ = span
        t1 = s1
        t0 = max(t1 - timedelta(minutes=minutes), s0)                # solo cuenta la ventana con historia
        only = str(arguments.get("canal") or "").strip()
        names = [only] if only else self.store.channels(device_id=dev)
        if only and only not in self.store.channels(device_id=dev):
            return {"ok": False, "error": f"No existe el canal '{only}'."}
        retention = float(getattr(self.store, "max_hours", 24.0)) * 60.0
        por_canal = {c: channel_quality(self.store.get_series(dev, c, minutes=retention), t0, t1, now=t1)
                     for c in names}
        have = [q["cobertura_pct"] for q in por_canal.values()]
        glob = {"cobertura_global_pct": round(sum(have) / len(have), 1) if have else 0.0,
                "canales_sin_datos": sorted(c for c, q in por_canal.items() if q["n_valid"] + q["n_frozen"] == 0),
                "canales_con_huecos": sorted(c for c, q in por_canal.items()
                                             if q["n_valid"] + q["n_frozen"] and q["cobertura_pct"] < 80)}
        lines = [f"Calidad de datos ({(t1 - t0).total_seconds() / 3600:.1f} h, hasta {self._local_fmt(t1)}):"]
        for c, q in sorted(por_canal.items()):
            if q["estado"].startswith("sin datos") or q["estado"] == "sin lecturas":
                lines.append(f"• {c}: 0% ({'deshabilitado' if q['n_missing'] else 'sin lecturas'})")
            else:
                lines.append(f"• {c}: {q['cobertura_pct']:.0f}% · último dato hace {q['antiguedad_min']:.0f} min · "
                             f"hueco máx {q['gap_max_min']:.0f} min ({q['n_gaps_mayores_10min']} > 10 min)")
        lines.append(f"Cobertura media: {glob['cobertura_global_pct']:.0f}%.")
        return {"ok": True, "por_canal": por_canal, "global": glob, "respuesta_base": "\n".join(lines)}

    def _tool_data_quality_canal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tool_data_quality(arguments)

    def _series_for(self, ch: str, d0: datetime, d1: datetime):
        retention = float(getattr(self.store, "max_hours", 24.0)) * 60.0
        return self.store.get_series(self._device_id(), ch, minutes=retention)

    def _tool_aggregate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from analysis.aggregations import AGGS, GROUPS, aggregate
        ch = str(arguments.get("canal") or "Temp Cámara").strip()
        if ch not in self.store.channels(device_id=self._device_id()):
            return {"ok": False, "error": f"No existe el canal '{ch}'."}
        agg = str(arguments.get("agg") or "mean").lower()
        grp = str(arguments.get("group_by") or "day").lower()
        if agg not in AGGS or grp not in GROUPS:
            return {"ok": False, "error": f"agg ∈ {AGGS}; group_by ∈ {GROUPS}."}
        d0, d1 = self._range_args(arguments, default_hours=168.0)
        msg = self._history_guard(desde=d0, hasta=d1)
        if msg:
            return {"ok": True, "filas": [], "respuesta_base": msg}
        rows = aggregate(self._series_for(ch, d0, d1), d0, d1, agg, grp, tz_name=self._tz_name())
        if not rows:
            return {"ok": True, "filas": [], "respuesta_base": f"No hay lecturas de {ch} en ese periodo."}
        unit = next((r.unit for r in self._series_for(ch, d0, d1)[-1:]), "")
        lines = [f"{ch} — {agg} por {grp} ({self._local_fmt(d0)} – {self._local_fmt(d1)}):"]
        lines += [f"• {r['grupo']}: {r['valor']:g} {unit} ({r['n']} lecturas)" for r in rows[:40]]
        return {"ok": True, "filas": rows, "respuesta_base": "\n".join(lines)}

    def _tool_export_csv(self, arguments: dict[str, Any]) -> dict[str, Any]:
        import os
        from analysis.aggregations import export_csv
        ch = str(arguments.get("canal") or "Temp Cámara").strip()
        if ch not in self.store.channels(device_id=self._device_id()):
            return {"ok": False, "error": f"No existe el canal '{ch}'."}
        d0, d1 = self._range_args(arguments, default_hours=24.0)
        msg = self._history_guard(desde=d0, hasta=d1)
        if msg:
            return {"ok": True, "respuesta_base": msg}
        safe = "".join(c if c.isalnum() else "_" for c in ch)
        path = os.path.join("logs", "exports", f"{safe}_{d0:%Y%m%d%H%M}_{d1:%Y%m%d%H%M}.csv")
        n = export_csv(self._series_for(ch, d0, d1), d0, d1, path, tz_name=self._tz_name())
        return {"ok": True, "path": path, "filas": n, "bytes": os.path.getsize(path),
                "respuesta_base": (f"Exporté {n} filas de {ch} a {path} (en el equipo del bot; "
                                   "todavía no sé enviar archivos por Telegram).")}

    # ---------------- ¿hay Multipower? ----------------
    multipower_present: bool | None = None     # run_reporter puede fijarlo; si es None se deduce del store

    def _has_multipower(self) -> bool:
        """True si hay medidores M1/M2: el flag `multipower_present` manda; si es None se deduce del store."""
        if self.multipower_present is not None:
            return bool(self.multipower_present)
        try:
            return any(c.startswith(("M1 ", "M2 ")) for c in self.store.channels(device_id=self._device_id()))
        except Exception:
            return False

    def _tool_data_coverage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from core.channel_catalog import SPEC_BY_NAME

        learn_h = self.PROFILE_LEARNING_HOURS
        asked = arguments.get("horas_requeridas")
        try:
            asked = float(asked) if asked is not None else None
        except (TypeError, ValueError):
            asked = None
        dev = self._device_id()
        span_min = float(getattr(self.store, "max_hours", 24.0)) * 60.0
        groups: dict[str, dict[str, Any]] = {}
        for ch in self.store.channels(device_id=dev):
            rs = [
                r for r in self.store.get_series(dev, ch, minutes=span_min)
                if r.value is not None and r.quality.value == "VALID"
            ]
            hours = (
                (rs[-1].event_time - rs[0].event_time).total_seconds() / 3600.0
                if len(rs) >= 2 else 0.0
            )
            spec = SPEC_BY_NAME.get(ch)
            inst = spec.instrument if spec else "otros"
            g = groups.setdefault(
                inst, {"canales": 0, "horas_min": None, "horas_max": 0.0,
                       "suficientes": 0, "insuficientes": [],
                       "cumplen_umbral_pedido": 0},
            )
            g["canales"] += 1
            g["horas_max"] = max(g["horas_max"], hours)
            g["horas_min"] = hours if g["horas_min"] is None else min(g["horas_min"], hours)
            if hours >= learn_h:
                g["suficientes"] += 1
            else:
                g["insuficientes"].append(ch)
            if asked is not None and hours >= asked:
                g["cumplen_umbral_pedido"] += 1
        for g in groups.values():
            g["horas_min"] = round(g["horas_min"] or 0.0, 2)
            g["horas_max"] = round(g["horas_max"], 2)
            g["insuficientes"] = g["insuficientes"][:8]
            if asked is None:
                g.pop("cumplen_umbral_pedido")
        best = max((g["horas_max"] for g in groups.values()), default=0.0)
        res = {
            "ok": True,
            "criterio": (f"Un canal tiene datos suficientes para su comportamiento "
                         f"habitual con al menos {learn_h:g} h de historia."),
            "horas_para_comportamiento_habitual": learn_h,
            "historia_disponible_horas": best,
            "retencion_horas": round(span_min / 60.0, 1),
            "por_instrumento": groups,
        }
        if asked is not None:
            res["umbral_pedido_horas"] = asked
        return res
    # ============================================================
    # HERRAMIENTA: top_variations
    # ============================================================
    def _tool_top_variations(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from datetime import timedelta
        from core.clock import local_tz

        ch = str(arguments["canal"]).strip()
        minutes = float(arguments["minutos"])
        n = max(1, min(int(arguments.get("n", 3) or 3), 10))
        if minutes <= 0:
            return {"ok": False, "error": "'minutos' debe ser mayor que cero."}
        dev = self._device_id()
        rs = [r for r in self.store.get_series(dev, ch, minutes=minutes)
              if r.value is not None and r.quality.value == "VALID"]
        if len(rs) < 5:
            return {"ok": False, "error": f"Pocos datos válidos en '{ch}' ({len(rs)})."}
        vals = [float(r.value) for r in rs]
        thr = max(0.5, 0.1 * (max(vals) - min(vals)))

        # ZigZag: tramos monótonos separados por una reversión >= thr.
        piv, up, ext = [], None, 0
        for i in range(1, len(vals)):
            v = vals[i]
            if up is None:
                if v - vals[0] >= thr:
                    piv.append(0); up, ext = True, i
                elif vals[0] - v >= thr:
                    piv.append(0); up, ext = False, i
            elif up:
                if v > vals[ext]:
                    ext = i
                elif vals[ext] - v >= thr:
                    piv.append(ext); up, ext = False, i
            else:
                if v < vals[ext]:
                    ext = i
                elif v - vals[ext] >= thr:
                    piv.append(ext); up, ext = True, i
        if up is not None:
            piv.append(ext)
        segs = [(piv[k], piv[k + 1]) for k in range(len(piv) - 1)]
        segs.sort(key=lambda ab: -abs(vals[ab[1]] - vals[ab[0]]))

        known = set(self.store.channels(device_id=dev))

        def on_times(name):
            if name not in known:
                return []
            return [x.event_time for x in self.store.get_series(dev, name, minutes=minutes)
                    if x.value is not None and float(x.value) > 0.5]

        defr, door = on_times("Estado Deshielo"), on_times("Puerta Abierta")
        from analysis.cooling_report import _state_fraction
        from analysis.diagnosis import DEFAULT_CFG as _DC
        refr_all = (self.store.get_series(dev, _DC["ch_refr"], minutes=minutes)
                    if _DC["ch_refr"] in known else [])
        loads_all = self._all_loads()
        from analysis.cooling_report import setpoint_changes as _spc
        sp_all = (self.store.get_series(dev, _DC["ch_setpoint"], minutes=minutes)
                  if _DC["ch_setpoint"] in known else [])
        tz = local_tz()
        fmt = lambda t: t.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        grace = timedelta(minutes=45)
        out = []
        for a, b in segs[:n]:
            t0, t1 = rs[a].event_time, rs[b].event_time
            subida = vals[b] > vals[a]
            if any(t0 <= x <= t1 for x in defr):
                ctx = "durante un deshielo programado (esperado)"
            elif not subida and any(t0 - grace <= x <= t0 for x in defr):
                ctx = "recuperación de la cámara tras un deshielo (esperado)"
            elif any(t0 <= x <= t1 for x in door):
                ctx = "con la puerta abierta"
            elif any(t0 - timedelta(minutes=60) <= l.t_in <= t1 for l in loads_all):
                ctx = "ingreso de producto registrado"
            elif sp_all and _spc(sp_all, t0 - timedelta(minutes=90), t1):
                ctx = "cambio de setpoint"
            else:
                fr = _state_fraction(refr_all, t0, t1) if refr_all else None
                if not subida and fr is not None and fr >= 0.6:
                    ctx = "enfriamiento normal del compresor"
                elif subida and fr is not None and fr <= 0.3 and abs(vals[b] - vals[a]) <= 3.0:
                    ctx = "subida normal con el compresor en reposo (entre ciclos)"
                else:
                    ctx = "sin deshielo, puerta ni producto que lo expliquen (revisar)"
            out.append({
                "tipo": "subida" if subida else "bajada",
                "inicio_local": fmt(t0), "fin_local": fmt(t1),
                "valor_inicial": round(vals[a], 2),
                "valor_final": round(vals[b], 2),
                "diferencia": round(vals[b] - vals[a], 2),
                "duracion_min": round((t1 - t0).total_seconds() / 60.0, 1),
                "contexto": ctx,
            })
        covered_h = (rs[-1].event_time - rs[0].event_time).total_seconds() / 3600.0
        res = {
            "ok": True, "canal": ch, "variaciones": out,
            "cobertura_horas": round(covered_h, 2),
            "zona_horaria": "hora local (America/Bogota)",
        }
        if covered_h * 60.0 < minutes * 0.9:
            res["advertencia"] = (
                f"Solo hay {covered_h:.1f} h de datos; se pidieron "
                f"{minutes / 60.0:.1f} h."
            )
        return res
    # ============================================================
    # HERRAMIENTA: diagnose_now
    # ============================================================
    def _tool_diagnose_now(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from analysis.diagnosis import diagnose

        minutes = float(arguments.get("minutos", 360.0) or 360.0)
        dev = self._device_id()
        series = {
            ch: self.store.get_series(dev, ch, minutes=minutes)
            for ch in self.store.channels(device_id=dev)
        }
        levels = None
        if self._levels_provider is not None:
            try:
                levels = self._levels_provider()
            except Exception:
                logger.exception("No se pudieron leer los niveles aprendidos.")
        b = diagnose(series, levels=levels)
        F = b.rasgos

        def f(k, nd=1):
            v = F.get(k)
            return None if v is None else round(float(v), nd)

        partes = []
        if f("temp_actual") is not None:
            partes.append(f"Temp Cámara {f('temp_actual')} °C (setpoint {f('setpoint')} °C, "
                          f"tendencia {f('pendiente_c_h')} °C/h)")
        if F.get("refr_estado"):
            partes.append(f"REFR {F['refr_estado']} ({f('refr_racha_min', 0)} min)")
        if F.get("deshielo_activo"):
            partes.append("deshielo activo")
        if F.get("puerta_abierta"):
            partes.append("puerta abierta")
        if f("kw_actual") is not None and self._has_multipower():
            partes.append(f"M1 {f('kw_actual')} kW")
        clave = {
            k: v for k, v in {
                "temp_actual_c": f("temp_actual"), "setpoint_c": f("setpoint"),
                "tendencia_c_h": f("pendiente_c_h"), "refr": F.get("refr_estado"),
                "refr_racha_min": f("refr_racha_min", 0),
                "m1_kw": f("kw_actual"),
                "m1_kw_esperado_con_refr": f("kw_esperado_on"),
                "m1_kw_esperado_sin_refr": f("kw_esperado_off"),
                "tension_m1_v": f("tension_propia_v", 0),
                "canales_sin_datos": F.get("canales_sin_datos"),
                "equipos_sin_datos": F.get("equipos_sin_datos"),
                "equipos_con_datos": F.get("equipos_con_datos"),
            }.items() if v is not None
        }
        if not self._has_multipower():
            for k in [k for k in clave if k.startswith(("m1_", "tension_m1"))]:
                clave.pop(k)
            partes.append("sin Multipower: diagnóstico parcial")
        base = {"ok": True, "resumen": "; ".join(partes), "datos_clave": clave}

        # `top` se define antes de usarlo (también en la rama "indeterminado").
        top = b.hipotesis[0] if b.hipotesis else None

        if b.principal == "indeterminado":
            base["respuesta_base"] = (
                f"{base['resumen']}. No hay evidencia de falla con los datos actuales."
                + (f" Datos que faltarían para confirmar: {top.faltan[0]}."
                   if top and top.faltan else "")
            )
            return base

        evid = " ".join(x.rstrip(".") + "." for x in top.a_favor[:3])
        otras = ", ".join(h.nombre for h in b.hipotesis[1:3])
        base["respuesta_base"] = (
            f"{base['resumen']}. Hipótesis principal: {b.principal} "
            f"(confianza {b.confianza:.0%}{', provisional' if b.provisional else ''}). "
            f"{evid}" + (f" Otras hipótesis: {otras}." if otras else "")
            + " Es una hipótesis, no una causa confirmada."
            + (f" Datos que faltarían para confirmar: {top.faltan[0]}."
               if top and top.faltan else "")
        )
        base.update(
            hipotesis_principal=b.principal,
            confianza=round(b.confianza, 2),
            provisional=b.provisional,
            hipotesis=[
                {"nombre": h.nombre, "score": round(h.score, 2),
                 "a_favor": list(h.a_favor), "en_contra": list(h.en_contra),
                 "faltan": list(h.faltan)}
                for h in b.hipotesis[:3]
            ],
        )
        return base

    def _tool_compressor_relation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Relación entre la orden de refrigeración (REFR) y lo que miden M1/M2
        y la temperatura. Es evidencia de comportamiento, no identificación
        de equipos.
        """
        from bisect import bisect_left
        from statistics import mean

        minutes = float(arguments.get("minutos", 240.0) or 240.0)
        dev = self._device_id()
        known = set(self.store.channels(device_id=dev))
        R, D, T = "Estado Refrigeración", "Estado Deshielo", "Temp Cámara"
        if R not in known:
            return {"ok": False, "error": f"No existe el canal '{R}'."}

        def valid(ch):
            return [x for x in self.store.get_series(dev, ch, minutes=minutes)
                    if x.value is not None and x.quality.value in ("VALID", "FROZEN")]

        refr = valid(R)
        defr = valid(D) if D in known else []
        tol = 90.0

        def nearest(ts, vs, t):
            if not ts:
                return None
            i = bisect_left(ts, t)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(ts):
                    d = abs((ts[j] - t).total_seconds())
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, vs[j])
            return None if best is None else best[1]

        dts, dvs = [x.event_time for x in defr], [float(x.value) for x in defr]

        def pearson(xs, ys):
            mx, my = mean(xs), mean(ys)
            sx = sum((x - mx) ** 2 for x in xs) ** 0.5
            sy = sum((y - my) ** 2 for y in ys) ** 0.5
            if sx < 1e-12 or sy < 1e-12:
                return None
            return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)

        res: dict[str, Any] = {"ok": True, "minutos_analizados": minutes,
                               "excluye": "muestras durante deshielo", "medidores": {},
                               "interpretacion": []}
        for m in ("M1", "M2"):
            ch = f"{m} Potencia Activa"
            if ch not in known:
                continue
            kw = valid(ch)
            kts, kvs = [x.event_time for x in kw], [float(x.value) for x in kw]
            bits, vals = [], []
            for r in refr:
                if (nearest(dts, dvs, r.event_time) or 0) > 0.5:
                    continue
                v = nearest(kts, kvs, r.event_time)
                if v is not None:
                    bits.append(1.0 if float(r.value) > 0.5 else 0.0)
                    vals.append(v)
            on = [v for b, v in zip(bits, vals) if b > 0.5]
            off = [v for b, v in zip(bits, vals) if b <= 0.5]
            if len(on) < 6 or len(off) < 6:
                res["medidores"][m] = {"error": "Pocas muestras con REFR activo/inactivo."}
                continue
            corr = pearson(bits, vals)
            delta = mean(on) - mean(off)
            res["medidores"][m] = {
                "kw_medio_con_refr_activo": round(mean(on), 2),
                "kw_medio_con_refr_inactivo": round(mean(off), 2),
                "diferencia_kw": round(delta, 2),
                "correlacion_con_refr": None if corr is None else round(corr, 2),
                "muestras": len(vals),
            }
            if corr is not None and corr >= 0.6 and delta >= 3.0:
                res["interpretacion"].append(
                    f"{m} sube {delta:.1f} kW cuando REFR está activo "
                    f"(correlación {corr:.2f}): su consumo sigue la orden de refrigeración.")
            else:
                res["interpretacion"].append(
                    f"{m} no responde a REFR (diferencia {delta:+.1f} kW, "
                    f"correlación {0.0 if corr is None else corr:.2f}).")

        # Efecto sobre la temperatura (excluye deshielo)
        tmp = valid(T) if T in known else []
        if len(tmp) >= 10:
            rts = [x.event_time for x in refr]
            rvs = [float(x.value) for x in refr]
            on_r, off_r = [], []
            for a, b in zip(tmp, tmp[1:]):
                dt_h = (b.event_time - a.event_time).total_seconds() / 3600.0
                if dt_h <= 0 or dt_h > 0.1:
                    continue
                if (nearest(dts, dvs, a.event_time) or 0) > 0.5:
                    continue
                st = nearest(rts, rvs, a.event_time)
                if st is None:
                    continue
                rate = (float(b.value) - float(a.value)) / dt_h
                (on_r if st > 0.5 else off_r).append(rate)
            if len(on_r) >= 6 and len(off_r) >= 6:
                res["temperatura"] = {
                    "tasa_c_h_con_refr_activo": round(mean(on_r), 2),
                    "tasa_c_h_con_refr_inactivo": round(mean(off_r), 2),
                }
                res["interpretacion"].append(
                    f"Temp Cámara cambia {mean(on_r):+.1f} °C/h con REFR activo y "
                    f"{mean(off_r):+.1f} °C/h con REFR inactivo.")
        res["nota"] = ("Es evidencia de comportamiento; no identifica qué equipo "
                       "físico mide cada medidor.")
        return res    
    
    def _freshness(self, channels: list[str]) -> dict[str, Any]:
        dev = self._device_id()
        latest_all = [
            r.event_time
            for ch in self.store.channels(device_id=dev)
            if (r := self.store.latest(dev, ch)) is not None
        ]
        if not latest_all:
            return {}
        now = max(latest_all)
        out: dict[str, Any] = {}
        for ch in channels:
            last = self.store.latest(dev, ch)
            if last is None:
                continue
            age = (now - last.event_time).total_seconds() / 60.0
            out.setdefault("ultima_lectura", {})[ch] = last.event_time.isoformat()
            out.setdefault("antiguedad_min", {})[ch] = round(age, 1)
        if any(v > 10 for v in out.get("antiguedad_min", {}).values()):
            out["advertencia"] = (
                "Algún canal lleva más de 10 min sin reportar: la ventana "
                "pedida termina en su última lectura, no en este momento."
            )
        return out

    def _execute_tool_raw(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Ejecuta una herramienta solicitada por el LLM.

        El LLM nunca accede directamente a SeriesStore,
        AnalysisEngine o HistoricalMemory.
        """

        try:

            # ----------------------------------------------------
            # LIST CHANNELS
            # ----------------------------------------------------

            if name == "list_channels":
                return {
                    "ok": True,
                    "device_id": self._device_id(),
                    "canales": self.store.channels(
                        device_id=self._device_id()
                    ),
                }

            if name == "list_converters":
                return self._tool_list_converters(arguments)

            if name == "alarm_history":
                return self._tool_alarm_history(arguments)

            if name == "correlate_with_alarms":
                return self._tool_correlate_with_alarms(arguments)
            # ----------------------------------------------------
            # CURRENT TIME
            # ----------------------------------------------------

            if name == "get_current_time":
                return {
                    "ok": True,
                    "utc": (
                        self._clock.now() if self._clock is not None
                        else datetime.now(timezone.utc)
                    ).isoformat(),
                }

            # ----------------------------------------------------
            # GET STATS
            # ----------------------------------------------------

            if name == "get_stats":
                return self._tool_get_stats(
                    arguments
                )

            # ----------------------------------------------------
            # GET SLOPE
            # ----------------------------------------------------

            if name == "get_slope":
                return self._tool_get_slope(
                    arguments
                )

            # ----------------------------------------------------
            # GET EVENTS
            # ----------------------------------------------------

            if name == "get_events":
                return self._tool_get_events(
                    arguments
                )

            # ----------------------------------------------------
            # CORRELATE
            # ----------------------------------------------------

            if name == "correlate":
                return self._tool_correlate(
                    arguments
                )

            # ----------------------------------------------------
            # ENERGY
            # ----------------------------------------------------

            if name == "energy":
                return self._tool_energy(
                    arguments
                )

            if name == "data_coverage":
                return self._tool_data_coverage(arguments)

            if name == "render_chart":
                return self._tool_render_chart(arguments)

            if name in ("history_coverage", "get_episodes", "episode_summary", "data_quality",
                        "data_quality_canal", "aggregate", "export_csv"):
                return getattr(self, "_tool_" + name)(arguments)

            if name == "top_variations":
                return self._tool_top_variations(arguments)

            if name == "diagnose_now":
                return self._tool_diagnose_now(arguments)

            if name == "temperature_report":
                return self._tool_temperature_report(arguments)

            if name == "controller_summary":
                return self._tool_controller_summary(arguments)

            if name == "load_report":
                return self._tool_load_report(arguments)

            if name == "status_report":
                return self._tool_status_report(arguments)

            if name == "forecast_temperature":
                return self._tool_forecast_temperature(arguments)

            if name == "explain_temperature":
                from agent.explain import explain_temperature
                return explain_temperature(
                    self, float(arguments.get("minutos", 120.0) or 120.0))
            
            if name == "compressor_relation":
                return self._tool_compressor_relation(arguments)
            
            if name == "historial_usuario":
                if self.conversations is None or not self._current_user:
                    return {"ok": False, "error": "Sin historial para este usuario."}
                hits = self.conversations.search(
                    self._current_user, str(arguments.get("consulta", ""))
                )
                return {"ok": True, "coincidencias": hits, "cantidad": len(hits)}

            # ----------------------------------------------------
            # UNKNOWN TOOL
            # ----------------------------------------------------

            return {
                "ok": False,
                "error": (
                    f"Herramienta desconocida: {name}"
                ),
            }

        except Exception as exc:

            logger.exception(
                "Error ejecutando herramienta '%s'.",
                name,
            )

            return {
                "ok": False,
                "error": str(exc),
            }

    # ============================================================
    # HERRAMIENTA: get_stats
    # ============================================================

    def _tool_get_stats(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:

        channel = str(
            arguments["canal"]
        ).strip()

        minutes = float(
            arguments["minutos"]
        )

        if minutes <= 0:
            raise ValueError(
                "'minutos' debe ser mayor que cero."
            )

        msg = self._history_guard(minutes=minutes)
        if msg:
            return {"ok": False, "error": msg}

        readings = self.store.get_series(
            device_id=self._device_id(),
            channel=channel,
            minutes=minutes,
        )

        # --------------------------------------------------------
        # VALIDAR QUE EL CANAL EXISTA
        # --------------------------------------------------------

        known = self.store.channels(
            device_id=self._device_id()
        )

        if not readings and channel not in known:
            return {
                "ok": False,
                "error": (
                    f"El canal '{channel}' no existe. "
                    f"Canales disponibles: {known}. "
                    "Usa el nombre exacto."
                ),
            }

        valid = self._valid_readings(
            readings
        )

        if not valid:
            return {
                "ok": True,
                "canal": channel,
                "minutos": minutes,
                "lecturas_validas": 0,
                "resultado": None,
                "mensaje": (
                    "El canal existe, pero no existen "
                    "lecturas válidas en el intervalo solicitado."
                ),
            }

        result = self.engine.stats(
            readings
        )

        out = {
            "ok": True,
            "canal": channel,
            "minutos": minutes,
            "resultado": self._json_safe(
                result
            ),
        }
        try:                                   # advertencia si la cobertura es baja (resultado posiblemente sesgado)
            from analysis.quality import channel_quality
            span = self._history_span()
            if span is not None:
                t1 = span[1]
                t0 = max(t1 - timedelta(minutes=minutes), span[0])
                cov = channel_quality(readings, t0, t1, now=t1)["cobertura_pct"]
                if cov < 80.0:
                    out["advertencia"] = (f"Cobertura del {cov:.0f}% en la ventana; "
                                          "el resultado puede estar sesgado")
        except Exception:
            logger.exception("No pude calcular la cobertura de get_stats.")
        return out

    # ============================================================
    # HERRAMIENTA: get_slope
    # ============================================================

    def _tool_get_slope(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:

        channel = str(
            arguments["canal"]
        ).strip()

        minutes = float(
            arguments["minutos"]
        )

        if minutes <= 0:
            raise ValueError(
                "'minutos' debe ser mayor que cero."
            )

        readings = self.store.get_series(
            device_id=self._device_id(),
            channel=channel,
            minutes=minutes,
        )

        # --------------------------------------------------------
        # VALIDAR QUE EL CANAL EXISTA
        # --------------------------------------------------------

        known = self.store.channels(
            device_id=self._device_id()
        )

        if not readings and channel not in known:
            return {
                "ok": False,
                "error": (
                    f"El canal '{channel}' no existe. "
                    f"Canales disponibles: {known}. "
                    "Usa el nombre exacto."
                ),
            }

        valid = self._valid_readings(
            readings
        )

        if len(valid) < 2:
            return {
                "ok": True,
                "canal": channel,
                "minutos": minutes,
                "resultado": None,
                "mensaje": (
                    "El canal existe, pero no hay suficientes "
                    "lecturas válidas para calcular la pendiente."
                ),
            }

        result = self.engine.slope(
            valid
        )

        return {
            "ok": True,
            "canal": channel,
            "minutos": minutes,
            "resultado": self._json_safe(
                result
            ),
        }

    # ============================================================
    # HERRAMIENTA: get_events
    # ============================================================

    def _tool_get_events(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:

        if self.memory is None:
            return {
                "ok": False,
                "error": (
                    "La memoria histórica no está disponible."
                ),
            }

        start = self._parse_datetime(
            arguments["desde"]
        )

        end = self._parse_datetime(
            arguments["hasta"]
        )

        if end < start:
            raise ValueError(
                "'hasta' no puede ser anterior a 'desde'."
            )

        events = self.memory.get_events(
            start=start,
            end=end,
        )

        serialized = []

        for event in events:

            serialized.append({
                "inicio": event.inicio.isoformat(),
                "fin": event.fin.isoformat(),
                "duracion_min": event.duracion_min,
                "variables_afectadas": list(
                    event.variables_afectadas
                ),
                "magnitud": dict(
                    event.magnitud
                ),
                "patron": event.patron,
                "volvio_a_base": (
                    event.volvio_a_base
                ),
            })

        return {
            "ok": True,
            "desde": start.isoformat(),
            "hasta": end.isoformat(),
            "cantidad": len(serialized),
            "eventos": serialized,
        }

    # ============================================================
    # HERRAMIENTA: correlate
    # ============================================================

    def _tool_correlate(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:

        channel_a = str(
            arguments["a"]
        ).strip()

        channel_b = str(
            arguments["b"]
        ).strip()

        minutes = float(
            arguments["minutos"]
        )

        if minutes <= 0:
            raise ValueError(
                "'minutos' debe ser mayor que cero."
            )

        # --------------------------------------------------------
        # OBTENER SERIES
        # --------------------------------------------------------

        series_a = self.store.get_series(
            device_id=self._device_id(),
            channel=channel_a,
            minutes=minutes,
        )

        series_b = self.store.get_series(
            device_id=self._device_id(),
            channel=channel_b,
            minutes=minutes,
        )

        # --------------------------------------------------------
        # VALIDAR CANAL A
        # --------------------------------------------------------

        known = self.store.channels(
            device_id=self._device_id()
        )

        if not series_a and channel_a not in known:
            return {
                "ok": False,
                "error": (
                    f"El canal '{channel_a}' no existe. "
                    f"Canales disponibles: {known}. "
                    "Usa el nombre exacto."
                ),
            }

        # --------------------------------------------------------
        # VALIDAR CANAL B
        # --------------------------------------------------------

        if not series_b and channel_b not in known:
            return {
                "ok": False,
                "error": (
                    f"El canal '{channel_b}' no existe. "
                    f"Canales disponibles: {known}. "
                    "Usa el nombre exacto."
                ),
            }

        # --------------------------------------------------------
        # FILTRAR LECTURAS VÁLIDAS
        # --------------------------------------------------------

        # El motor solo usa VALID; si aquí se dejara pasar FROZEN, el
        # emparejamiento por posición del motor se desfasaría en silencio.
        valid_a = [
            r for r in self._valid_readings(series_a)
            if r.quality == DataQuality.VALID
        ]

        valid_b = [
            r for r in self._valid_readings(series_b)
            if r.quality == DataQuality.VALID
        ]

        if len(valid_a) < 2 or len(valid_b) < 2:
            return {
                "ok": True,
                "a": channel_a,
                "b": channel_b,
                "minutos": minutes,
                "resultado": None,
                "mensaje": (
                    "Los canales existen, pero no existen "
                    "suficientes datos válidos para calcular "
                    "la correlación."
                ),
            }

        # --------------------------------------------------------
        # ALINEAR TEMPORALMENTE
        # --------------------------------------------------------

        aligned_a, aligned_b = (
            self._align_series(
                valid_a,
                valid_b,
            )
        )

        if len(aligned_a) < 2:
            return {
                "ok": True,
                "a": channel_a,
                "b": channel_b,
                "minutos": minutes,
                "resultado": None,
                "mensaje": (
                    "Los canales existen, pero no existen "
                    "suficientes puntos temporalmente alineados."
                ),
            }

        # --------------------------------------------------------
        # SERIE CONSTANTE: la correlación no está definida (el motor
        # devolvería 0.0, que se lee como "sin relación").
        # --------------------------------------------------------

        for label, series in ((channel_a, aligned_a), (channel_b, aligned_b)):
            if len({round(float(r.value), 9) for r in series}) < 2:
                return {
                    "ok": True,
                    "a": channel_a,
                    "b": channel_b,
                    "minutos": minutes,
                    "puntos_alineados": len(aligned_a),
                    "resultado": None,
                    "mensaje": (
                        f"'{label}' es constante en el intervalo; la "
                        "correlación no está definida. No significa que "
                        "no exista relación."
                    ),
                }

        # --------------------------------------------------------
        # CALCULAR CORRELACIÓN
        # --------------------------------------------------------

        result = self.engine.correlate(
            aligned_a,
            aligned_b,
        )

        out = {
            "ok": True,
            "a": channel_a,
            "b": channel_b,
            "minutos": minutes,
            "puntos_alineados": len(
                aligned_a
            ),
            "resultado": self._json_safe(
                result
            ),
        }
        if len(aligned_a) < 20:
            out["advertencia_muestra"] = (
                f"Solo {len(aligned_a)} pares independientes: la correlación "
                "es poco fiable. Amplía la ventana o confirma con más datos."
            )
        return out

    # ============================================================
    # HERRAMIENTA: energy
    # ============================================================

    def _tool_energy(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:

        channel = str(
            arguments["canal"]
        ).strip()

        minutes = float(
            arguments["minutos"]
        )

        if minutes <= 0:
            raise ValueError(
                "'minutos' debe ser mayor que cero."
            )

        readings = self.store.get_series(
            device_id=self._device_id(),
            channel=channel,
            minutes=minutes,
        )

        # --------------------------------------------------------
        # VALIDAR QUE EL CANAL EXISTA
        # --------------------------------------------------------

        known = self.store.channels(
            device_id=self._device_id()
        )

        if not readings and channel not in known:
            return {
                "ok": False,
                "error": (
                    f"El canal '{channel}' no existe. "
                    f"Canales disponibles: {known}. "
                    "Usa el nombre exacto."
                ),
            }

        valid = self._valid_readings(
            readings
        )

        if len(valid) < 2:
            return {
                "ok": True,
                "canal": channel,
                "minutos": minutes,
                "resultado": None,
                "mensaje": (
                    "El canal existe, pero no hay suficientes "
                    "lecturas válidas para calcular energía."
                ),
            }

        # --------------------------------------------------------
        # UNIDAD DEL CANAL Y UNIDAD DE ENERGÍA
        #
        # El campo `unidad_potencia` original inducía al LLM a
        # reportar "kW" en un contexto de energía. Se exponen dos
        # campos separados y sin ambigüedad:
        #
        #   unidad_canal   -> "kW" | "kVA" | ...
        #   unidad_energia -> "kWh" | "kVAh" | ...
        #
        # El prompt (regla 4) instruye reportar `resultado` +
        # `unidad_energia`.
        # --------------------------------------------------------

        unit = valid[0].unit

        # Contador acumulado (kWh, kVArh, kVAh): la energía es fin - inicio.
        # Es exacta y no depende de huecos ni de la cadencia de muestreo.
        if str(unit).strip().lower() in {"kwh", "kvarh", "kvah"}:
            ordered = sorted(valid, key=lambda r: r.event_time)
            total, resets = 0.0, 0
            for prev, cur in zip(ordered, ordered[1:]):
                diff = float(cur.value) - float(prev.value)
                if diff >= 0:
                    total += diff
                else:
                    resets += 1   # reinicio del contador: se descarta ese tramo
            return {
                "ok": True,
                "canal": channel,
                "minutos": minutes,
                "unidad_canal": unit,
                "unidad_energia": unit,
                "metodo": "diferencia_de_contador",
                "reinicios_contador": resets,
                "resultado": round(total, 4),
            }

        result = self.engine.energy(
            valid,
            unit=unit,
        )

        unit_norm = str(unit).strip().lower()

        if unit_norm == "kw":
            unit_energy = "kWh"
        elif unit_norm == "kva":
            unit_energy = "kVAh"
        else:
            unit_energy = "unidad_desconocida"

        # --------------------------------------------------------
        # COBERTURA: el motor ignora los intervalos mayores que
        # max_change_gap_seconds. Si los datos son escasos, el
        # resultado es 0.0 o muy bajo, y eso NO es "no se consumió
        # energía": es "no hay datos para integrar".
        # --------------------------------------------------------

        ordered = sorted(valid, key=lambda r: r.event_time)
        span = (ordered[-1].event_time - ordered[0].event_time).total_seconds()
        max_gap = float(getattr(self.engine, "max_change_gap_seconds", 60.0))
        covered = sum(
            dt for dt in (
                (b.event_time - a.event_time).total_seconds()
                for a, b in zip(ordered, ordered[1:])
            )
            if 0 < dt <= max_gap
        )
        coverage = (covered / span * 100.0) if span > 0 else 0.0

        out = {
            "ok": True,
            "canal": channel,
            "minutos": minutes,
            "unidad_canal": unit,
            "unidad_energia": unit_energy,
            "cobertura_pct": round(coverage, 1),
            "resultado": self._json_safe(
                result
            ),
        }

        if coverage < 95.0:
            # La integración solo suma los intervalos con datos, así que
            # SUBESTIMA aproximadamente (1 - cobertura). Si existe el contador
            # acumulado del mismo equipo, la energía exacta es fin - inicio.
            dev = self._device_id()
            counters = [
                ch for ch in self.store.channels(device_id=dev)
                if (r := self.store.latest(dev, ch)) is not None
                and str(r.unit).strip().lower() == "kwh"
            ]
            prefix = channel.split()[0] if channel.split() else ""
            same = [c for c in counters if c.startswith(prefix)]
            counter = same[0] if same else None

            if unit_energy == "kWh" and counter is not None:
                cser = [
                    r for r in self.store.get_series(dev, counter, minutes=minutes)
                    if r.value is not None and r.quality == DataQuality.VALID
                ]
                if len(cser) >= 2:
                    cser.sort(key=lambda r: r.event_time)
                    total = sum(
                        max(0.0, float(b.value) - float(a.value))
                        for a, b in zip(cser, cser[1:])
                    )
                    out["resultado_integracion"] = out["resultado"]
                    out["resultado"] = round(total, 4)
                    out["metodo"] = "diferencia_de_contador"
                    out["contador_usado"] = counter
                    out["nota"] = (
                        f"Los datos de '{channel}' cubren solo {coverage:.0f} % "
                        f"de la ventana; se usó el contador '{counter}', que es "
                        "exacto aunque falten lecturas."
                    )
                    return out

            if coverage < 50.0:
                out["resultado"] = None
                out["mensaje"] = (
                    f"Los datos de '{channel}' cubren solo {coverage:.0f} % de la "
                    "ventana (huecos mayores al máximo permitido): no se puede "
                    "integrar la energía de forma fiable."
                )
            else:
                out["advertencia"] = (
                    f"Cobertura de {coverage:.0f} %: el valor SUBESTIMA el consumo "
                    "real (aprox. en esa proporción)."
                )
        return out

    # ============================================================
    # UTILIDADES INTERNAS
    # ============================================================

    def _device_id(self) -> int:
        """
        Devuelve el device_id configurado para el agente.
        """

        return self.device_id

    @staticmethod
    def _valid_readings(
        readings: list,
    ) -> list:

        return [
            reading
            for reading in readings
            if (
                reading.value is not None
                and reading.quality
                in {
                    DataQuality.VALID,
                    DataQuality.FROZEN,
                }
            )
        ]

    @staticmethod
    def _parse_datetime(
        value: str,
    ) -> datetime:

        if not value:
            raise ValueError(
                "Fecha/hora vacía."
            )

        dt = datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(
            timezone.utc
        )

    @staticmethod
    def _json_safe(
        value: Any,
    ) -> Any:

        if value is None:
            return None

        if isinstance(
            value,
            (str, int, float, bool),
        ):
            return value

        if isinstance(
            value,
            datetime,
        ):
            return value.isoformat()

        if isinstance(
            value,
            dict,
        ):
            return {
                str(key): CavaAgent._json_safe(
                    item
                )
                for key, item in value.items()
            }

        if isinstance(
            value,
            (list, tuple),
        ):
            return [
                CavaAgent._json_safe(
                    item
                )
                for item in value
            ]

        if hasattr(
            value,
            "__dict__",
        ):
            return CavaAgent._json_safe(
                value.__dict__
            )

        return str(value)

    @staticmethod
    def _align_series(
        a: list,
        b: list,
        tolerance_seconds: float | None = None,
    ) -> tuple[list, list]:
        """Empareja lecturas de dos series por cercanía temporal, UNO A UNO.

        Se empareja desde la serie más escasa y cada lectura de la otra se
        usa a lo sumo una vez. Si no, un instrumento lento (p. ej. 1 dato
        cada 20 min) se contaría ~10 veces contra uno rápido (cada 2 min) y
        n aparentaría 10 veces más información independiente de la real.
        """

        def _dt(rs: list) -> float:
            gaps = sorted(
                (y.event_time - x.event_time).total_seconds()
                for x, y in zip(rs, rs[1:])
            )
            return gaps[len(gaps) // 2] if gaps else 0.0

        dta, dtb = _dt(a), _dt(b)
        if tolerance_seconds is None:
            # Dos instrumentos con la misma cadencia suelen muestrear
            # desfasados ~medio período.
            tolerance_seconds = max(10.0, 0.6 * max(dta, dtb))

        swap = dta < dtb                       # `a` es la más densa
        base, other = (b, a) if swap else (a, b)

        pairs = []
        used: set[int] = set()
        j = 0
        for rb in base:
            while j < len(other) and other[j].event_time < rb.event_time:
                j += 1
            cands = [k for k in (j, j - 1) if 0 <= k < len(other)]
            if not cands:
                continue
            k = min(
                cands,
                key=lambda k: abs(
                    (other[k].event_time - rb.event_time).total_seconds()
                ),
            )
            dist = abs((other[k].event_time - rb.event_time).total_seconds())
            if k in used or dist > tolerance_seconds:
                continue
            used.add(k)
            pairs.append((rb, other[k]))

        if swap:
            return [o for _, o in pairs], [rb for rb, _ in pairs]
        return [rb for rb, _ in pairs], [o for _, o in pairs]

    def _render_tree(self, tree: dict) -> str:
        """Texto legible del árbol descubierto."""
        lines: list[str] = []
        convs = tree.get("converters") or []
        unassigned = tree.get("unassigned_instruments") or []
        n_conv = tree.get("n_converters", 0)
        n_inst = tree.get("n_instruments", 0)
        n_eff = tree.get("n_channels_effective", 0)
        n_tot = tree.get("n_channels_total", 0)
        if n_conv or n_inst:
            lines.append(
                f"En SITRAD hay {n_conv} convertidor(es), {n_inst} instrumento(s) y "
                f"{n_eff}/{n_tot} canales efectivos."
            )
        else:
            lines.append("No encontré convertidores ni instrumentos en SITRAD.")

        def _dump_instrument(ins: dict, indent: str = "  ") -> None:
            lines.append(
                f"{indent}• {ins.get('name')} (id {ins.get('id')}, "
                f"{ins.get('n_channels_effective', 0)}/{ins.get('n_channels_total', 0)} canales efectivos)"
            )
            for ch in ins.get("channels", []):
                if ch.get("accepted"):
                    lines.append(f"{indent}    ✓ {ch['canal']}  ({ch['variable']}) = {ch.get('value')!r} {ch.get('unit') or ''}")
                else:
                    lines.append(f"{indent}    ⚠ {ch['canal']}  ({ch['variable']}) — {ch.get('reason') or 'descartado'}")

        for c in convs:
            lines.append(f"Convertidor {c.get('name')} (id {c.get('id')}, {c.get('status') or '?'}):")
            if not c.get("instruments"):
                lines.append("  (sin instrumentos)")
            for ins in c["instruments"]:
                _dump_instrument(ins)
        if unassigned:
            lines.append("Sin convertidor asignado:")
            for ins in unassigned:
                _dump_instrument(ins)
        return "\n".join(lines)


    def _tool_list_converters(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Árbol SITRAD: convertidores → instrumentos → canales. Con caché en memoria."""
        fn = getattr(self, "discovery_provider", None)
        if fn is None:
            return {"ok": False,
                    "error": "El descubrimiento de hardware no está disponible "
                            "(solo en modo sitrad con la fuente SITRAD activa)."}
        refresh = bool(arguments.get("refresh"))
        if not refresh:
            cached = getattr(self, "_sitrad_tree", None)
            if cached is not None:
                return {"ok": True, "arbol": cached, "respuesta_base": self._render_tree(cached)}
        try:
            tree = fn()
        except Exception as exc:
            logger.exception("discover_all falló.")
            return {"ok": False, "error": f"El descubrimiento en SITRAD falló: {exc}"}
        self._sitrad_tree = tree
        return {"ok": True, "arbol": tree, "respuesta_base": self._render_tree(tree)}



    def _tool_alarm_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Alarmas de SITRAD persistidas en logs/sitrad_alarms.jsonl."""
        from pathlib import Path
        import json
        path = Path("logs") / "sitrad_alarms.jsonl"
        if not path.exists():
            return {"ok": True, "alarmas": [], "por_codigo": {},
                    "respuesta_base": ("No hay alarmas de SITRAD guardadas todavía. "
                                       "Las alarmas se guardan a partir del próximo evento.")}
        d0 = self._parse_dt(arguments.get("desde")) or (self._now() - timedelta(days=7))
        d1 = self._parse_dt(arguments.get("hasta")) or self._now()
        inst = (arguments.get("instrumento") or "").lower().strip()
        codigo = (arguments.get("codigo") or "").lower().strip()
        out: list[dict] = []
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    t = self._parse_dt(rec.get("when")) or self._parse_dt(rec.get("saved_at"))
                    if t is None or not (d0 <= t <= d1):
                        continue
                    if inst and inst not in (rec.get("instrument") or "").lower():
                        continue
                    if codigo and codigo not in json.dumps(rec.get("raw", {}), default=str).lower() \
                            and codigo not in (rec.get("text") or "").lower():
                        continue
                    out.append({
                        "when": t.isoformat(),
                        "instrument": rec.get("instrument"),
                        "text": rec.get("text"),
                        "code": (rec.get("raw") or {}).get("code"),
                    })
        except Exception as exc:
            return {"ok": False, "error": f"No pude leer logs/sitrad_alarms.jsonl: {exc}"}
        out.sort(key=lambda r: r["when"])
        por_codigo: dict[str, int] = {}
        for a in out:
            k = a.get("code") or a.get("text") or "?"
            por_codigo[k] = por_codigo.get(k, 0) + 1
        lines = [f"Alarmas de SITRAD entre {self._local_fmt(d0)} y {self._local_fmt(d1)}: {len(out)}."]
        for k, n in sorted(por_codigo.items(), key=lambda kv: -kv[1]):
            lines.append(f"  • {k}: {n}")
        for a in out[-10:]:
            lines.append(f"  · {self._local_fmt(self._parse_dt(a['when']))} "
                         f"{a['instrument'] or '?'} — {a['text']}")
        return {"ok": True, "alarmas": out, "por_codigo": por_codigo,
                "respuesta_base": "\n".join(lines)}



    def _tool_correlate_with_alarms(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Coincidencia temporal entre variaciones de un canal y alarmas de SITRAD."""
        ch = str(arguments.get("canal") or "Temp Cámara").strip()
        if ch not in self.store.channels(device_id=self._device_id()):
            return {"ok": False, "error": f"No existe el canal '{ch}'."}
        d0 = self._parse_dt(arguments.get("desde")) or (self._now() - timedelta(days=7))
        d1 = self._parse_dt(arguments.get("hasta")) or self._now()
        minutes = max((d1 - d0).total_seconds() / 60.0, 30.0)
        hist = self._tool_alarm_history({
            "desde": d0.isoformat(), "hasta": d1.isoformat()})
        alarmas = hist.get("alarmas", [])
        var = self._tool_top_variations({"canal": ch, "minutos": minutes, "n": 20})
        variaciones = var.get("variaciones", [])

        def _parse_local(s: str | None):
            if not s:
                return None
            try:
                return datetime.fromisoformat(str(s).replace(" ", "T")).replace(tzinfo=None)
            except Exception:
                return None

        coincidencias = 0
        detalles: list[str] = []
        for v in variaciones:
            t0 = _parse_local(v.get("inicio_local"))
            t1 = _parse_local(v.get("fin_local"))
            if t0 is None or t1 is None:
                continue
            hits = []
            for a in alarmas:
                ta_iso = a.get("when")
                if not ta_iso:
                    continue
                try:
                    ta = datetime.fromisoformat(ta_iso)
                    from core.clock import local_tz
                    ta_local = ta.astimezone(local_tz(self._tz_name())).replace(tzinfo=None)
                except Exception:
                    continue
                if t0 <= ta_local <= t1:
                    hits.append(a)
            if hits:
                coincidencias += 1
                detalles.append(
                    f"  · {v['inicio_local']} → {v['fin_local']} "
                    f"({v['tipo']} {v['diferencia']:+.1f}) — alarmas: "
                    + ", ".join((h.get("text") or "?") for h in hits[:2]))
        lines = [f"{ch}: {len(variaciones)} variaciones, {len(alarmas)} alarmas, "
                 f"{coincidencias} coincidencias."]
        if detalles:
            lines.append("Coincidencias:")
            lines += detalles
        elif not alarmas:
            lines.append("(no hay alarmas guardadas todavía en esa ventana)")
        return {"ok": True, "canal": ch, "variaciones": len(variaciones),
                "alarmas": len(alarmas), "coincidencias": coincidencias,
                "respuesta_base": "\n".join(lines)}