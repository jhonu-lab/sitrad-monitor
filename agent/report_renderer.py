from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ============================================================
# DRAFT
# ============================================================

@dataclass
class ReportDraft:
    titulo: str
    severidad: str                        # "alta" | "media" | "baja"
    hechos: list[str] = field(default_factory=list)
    hipotesis: list[str] = field(default_factory=list)
    accion: str = "Sin acción requerida."


    @classmethod
    def from_dict(cls, raw: Any) -> "ReportDraft | None":
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                titulo=str(raw.get("titulo", "")).strip(),
                severidad=str(raw.get("severidad", "baja")).strip().lower(),
                hechos=[str(x).strip() for x in raw.get("hechos", []) if str(x).strip()],
                hipotesis=[str(x).strip() for x in raw.get("hipotesis", []) if str(x).strip()],
                accion=str(raw.get("accion", "Sin acción requerida.")).strip(),
            )
        except Exception:
            return None

    def is_valid(self) -> bool:
        if not self.titulo:
            return False
        if self.severidad not in {"alta", "media", "baja"}:
            return False
        if not self.hechos:
            return False
        return True


# ============================================================
# RENDER
# ============================================================

SEV_ICON = {
    "alta": "🔴",
    "media": "🟠",
    "baja": "🟡",
}


def render(draft: ReportDraft) -> str:
    """
    Plantilla fija. El LLM solo rellena el draft; el texto
    se compone aquí.
    """
    lines: list[str] = []
    icon = SEV_ICON.get(draft.severidad, "•")

    lines.append(f"{icon} {draft.titulo}")
    lines.append(f"Severidad: {draft.severidad}")
    lines.append("")

    lines.append("Hechos:")
    for h in draft.hechos:
        lines.append(f"  • {h}")

    if draft.hipotesis:
        lines.append("")
        lines.append("Hipótesis (no confirmadas):")
        for h in draft.hipotesis:
            lines.append(f"  • {h}")

    lines.append("")
    lines.append(f"Acción: {draft.accion}")

    return "\n".join(lines)


# ============================================================
# ANCLAJE NUMÉRICO
# ============================================================

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _normalize_number(token: str) -> str:
    """Unifica separador decimal y elimina ceros a la derecha."""
    s = token.replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return s
    # 25.0 -> "25", 25.30 -> "25.3"
    s = f"{f:g}"
    return s


def extract_numbers(text: str) -> set[str]:
    return {
        _normalize_number(m.group(0))
        for m in _NUM_RE.finditer(text)
    }


def numeric_anchor_ok(
    report_text: str,
    context: str,
    *,
    allow: set[str] | None = None,
) -> tuple[bool, set[str]]:
    """
    Devuelve (ok, missing).

    'ok' es True si TODO número del informe aparece en el
    contexto, o en la lista blanca 'allow' (p.ej. severidades
    textuales, números de plantilla).
    """
    report_numbers = extract_numbers(report_text)
    context_numbers = extract_numbers(context)
    allowed = allow or set()

    missing = report_numbers - context_numbers - allowed
    return (not missing, missing)


# ============================================================
# FALLBACK DETERMINISTA
# ============================================================

def draft_from_alert(
    *,
    channel: str,
    kind: str,
    severity: float,
    since_iso: str,
    evidence: dict[str, Any],
) -> ReportDraft:
    """Plantilla determinista para ALERTA / SALUD."""
    sev_label = _severity_label(severity)

    hechos = [
        f"Canal: {channel}",
        f"Tipo de anomalía: {kind}",
        f"Severidad calculada: {severity:.2f} ({sev_label})",
        f"Desde: {since_iso}",
    ]

    # Añadir evidencia numérica de forma legible.
    for k, v in evidence.items():
        if isinstance(v, (int, float)):
            hechos.append(f"{k}: {v}")
        elif isinstance(v, (list, tuple)) and v:
            hechos.append(f"{k}: {', '.join(map(str, v))}")

    return ReportDraft(
        titulo=f"Anomalía en {channel}",
        severidad=sev_label,
        hechos=hechos,
        hipotesis=[],
        accion="Verificar el canal en el próximo turno.",
    )


def draft_from_resolution(
    *,
    channel: str,
    kind: str,
    duration_min: float,
    since_iso: str,
) -> ReportDraft:
    return ReportDraft(
        titulo=f"Recuperado: {channel}",
        severidad="baja",
        hechos=[
            f"Canal: {channel}",
            f"Tipo: {kind}",
            f"Duración: {duration_min:.1f} min",
            f"Inicio: {since_iso}",
        ],
        hipotesis=[],
        accion="Sin acción requerida.",
    )
def draft_from_summary(
    *,
    events: list[dict[str, Any]],
    window_hours: float,
) -> ReportDraft:
    """
    Resumen de turno. Si los eventos vienen anotados (`explicado`,
    `descripcion`) se separan los que son operación normal (deshielo
    programado, recuperación) de los que hay que revisar.
    """
    hechos: list[str] = [f"Ventana: últimas {window_hours:.1f} h"]

    annotated = [e for e in events if "explicado" in e]
    if annotated:
        ok = sum(1 for e in annotated if e["explicado"])
        hechos.append(
            f"Eventos térmicos: {len(events)} "
            f"({ok} explicados por operación normal, "
            f"{len(annotated) - ok} por revisar)"
        )
    else:
        hechos.append(f"Eventos: {len(events)}")

    # Primero lo que requiere atención.
    ordered = sorted(events, key=lambda e: e.get("explicado") is not False)
    for e in ordered[:5]:
        if e.get("descripcion"):
            prefix = "" if e.get("explicado") else "POR REVISAR: "
            hechos.append(prefix + e["descripcion"])
        else:
            patron = e.get("patron", "evento")
            dur = e.get("duracion_min", 0.0)
            canales = ", ".join(e.get("variables_afectadas", []))
            hechos.append(f"{patron} ({dur:.1f} min): {canales}")
    if len(events) > 5:
        hechos.append(f"... y {len(events) - 5} más")

    por_revisar = any(e.get("explicado") is False for e in events)
    return ReportDraft(
        titulo="Resumen de turno",
        severidad="media" if por_revisar else "baja",
        hechos=hechos,
        hipotesis=[],
        accion=(
            "Revisar los eventos marcados «POR REVISAR»."
            if por_revisar else "Sin acción requerida."
        ),
    )


def _severity_label(x: float) -> str:
    if x >= 0.7:
        return "alta"
    if x >= 0.35:
        return "media"
    return "baja"