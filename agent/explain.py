"""
agent/explain.py — "¿por qué sube la temperatura?", "¿coincide con el consumo?",
"¿es normal?".

Es la pregunta central del ingeniero. Reúne en UNA respuesta, armada en código
(sin LLM, así ninguna cifra se inventa):

    1. qué hace la temperatura (nivel, setpoint, pendiente en la ventana);
    2. qué hacen a la vez el compresor (ciclo de trabajo) y el consumo del
       medidor (kWh y consumo frente al esperado con compresor encendido),
       comparados con el comportamiento habitual de las últimas 24 h;
    3. las hipótesis del diagnóstico determinista con su evidencia a favor y
       en contra, y los datos que faltarían para confirmarlas.

Los canales salen de DEFAULT_CFG de diagnosis (roles), no de nombres fijos.
No concluye una causa: presenta hipótesis respaldadas por evidencia.
"""
from __future__ import annotations

from typing import Any

from analysis.diagnosis import DEFAULT_CFG, diagnose

GOOD = ("VALID", "FROZEN")
RISE_C_H = 0.5          # |pendiente| por debajo de esto se considera estable
HISTORY_MIN_HOURS = 12  # historia mínima para hablar de "habitual"


def _good(rs):
    return [r for r in rs if r.value is not None and r.quality.value in GOOD]


def _mean_state(rs) -> float | None:
    v = [float(r.value) for r in _good(rs)]
    return (sum(v) / len(v)) if v else None


def _hours(rs) -> float:
    g = _good(rs)
    if len(g) < 2:
        return 0.0
    return (g[-1].event_time - g[0].event_time).total_seconds() / 3600.0


def explain_temperature(agent, minutes: float = 120.0) -> dict[str, Any]:
    minutes = max(30.0, float(minutes or 120.0))
    dev = agent._device_id()
    ch_t, ch_r, ch_k = DEFAULT_CFG["ch_temp"], DEFAULT_CFG["ch_refr"], DEFAULT_CFG["ch_kw"]
    store = agent.store
    have = set(store.channels(device_id=dev))
    if ch_t not in have:
        return {"ok": False, "error": f"No hay datos del canal de temperatura ({ch_t})."}

    series = {ch: store.get_series(dev, ch, minutes=360.0) for ch in have}
    levels = None
    if getattr(agent, "_levels_provider", None) is not None:
        try:
            levels = agent._levels_provider()
        except Exception:
            levels = None
    b = diagnose(series, levels=levels)
    F = b.rasgos

    temp_now = F.get("temp_actual")
    if temp_now is None:
        return {"ok": False, "error": f"Sin lecturas recientes de {ch_t}."}
    sp = F.get("setpoint")

    # ---- 1. temperatura ------------------------------------------------
    sl = agent._tool_get_slope({"canal": ch_t, "minutos": minutes})
    slope = sl.get("resultado")
    if slope is None:
        verbo, tendencia = "sin tendencia significativa", "estable"
    elif slope >= RISE_C_H:
        verbo, tendencia = f"sube {slope:+.2f} °C/h", "sube"
    elif slope <= -RISE_C_H:
        verbo, tendencia = f"baja {slope:+.2f} °C/h", "baja"
    else:
        verbo, tendencia = f"estable ({slope:+.2f} °C/h)", "estable"
    linea_t = f"{ch_t}: {temp_now:.1f} °C"
    if sp is not None:
        linea_t += f" (setpoint {sp:.1f} °C, desvío {temp_now - sp:+.1f} °C)"
    linea_t += f"; en {minutes:.0f} min {verbo}."

    # ---- 2. compresor y consumo frente a lo habitual ---------------------
    hechos: list[str] = []
    datos: dict[str, Any] = {"temp_c": round(temp_now, 1), "tendencia": tendencia,
                             "pendiente_c_h": None if slope is None else round(float(slope), 2)}
    if ch_r in have:
        win = store.get_series(dev, ch_r, minutes=minutes)
        day = store.get_series(dev, ch_r, minutes=1440.0)
        d_w, d_d = _mean_state(win), _mean_state(day)
        if d_w is not None:
            datos["refr_duty_ventana"] = round(d_w, 2)
            txt = f"El compresor (REFR) estuvo encendido {d_w:.0%} del tiempo en la ventana"
            if d_d is not None and _hours(day) >= HISTORY_MIN_HOURS:
                txt += f" (habitual en 24 h: {d_d:.0%})"
                datos["refr_duty_24h"] = round(d_d, 2)
            hechos.append(txt + ".")
    if ch_k in have:
        ratio = F.get("kw_ratio_refr")
        if ratio is not None and F.get("refr_estado") == "ON":
            r_show = max(0.0, min(float(ratio), 1.5))
            hechos.append(
                f"Con el compresor pedido, {ch_k} marca {F['kw_actual']:.1f} kW: el aumento "
                f"de consumo que produce el compresor es {r_show:.0%} del normal "
                f"(0% = no se nota compresor, 100% = trabaja como siempre).")
            datos["consumo_vs_esperado_con_compresor"] = round(float(ratio), 2)
        # energía: contador del medidor si existe, si no integración de potencia
        kwh_ch = next((c for c in have if c.endswith("Energía Activa") and c.startswith(ch_k.split(" ")[0])), ch_k)
        e_w = agent._tool_energy({"canal": kwh_ch, "minutos": minutes})
        e_d = agent._tool_energy({"canal": kwh_ch, "minutos": 1440.0})
        kw_w = e_w.get("resultado") if isinstance(e_w, dict) else None
        kw_d = e_d.get("resultado") if isinstance(e_d, dict) else None
        if kw_w is not None:
            txt = f"{ch_k.split(' ')[0]} consumió {float(kw_w):.1f} kWh en {minutes:.0f} min"
            hist_h = _hours(store.get_series(dev, ch_k, minutes=1440.0))
            if kw_d and hist_h >= HISTORY_MIN_HOURS:
                esperado = float(kw_d) * (minutes / 60.0) / hist_h
                if esperado > 0:
                    pct = (float(kw_w) / esperado - 1.0) * 100.0
                    txt += f" ({pct:+.0f}% frente a su ritmo de las últimas {hist_h:.0f} h)"
                    datos["energia_vs_habitual_pct"] = round(pct, 0)
            hechos.append(txt + ".")
            datos["energia_kwh_ventana"] = round(float(kw_w), 2)
    if F.get("puerta_min_abierta_ventana"):
        hechos.append(f"La puerta estuvo abierta {F['puerta_min_abierta_ventana']:.0f} min en la ventana.")
    if F.get("deshielo_activo"):
        hechos.append("Hay un deshielo activo: es normal que la temperatura suba.")
    elif F.get("min_desde_deshielo") is not None and F["min_desde_deshielo"] <= DEFAULT_CFG["defrost_grace_min"]:
        hechos.append(f"El último deshielo terminó hace {F['min_desde_deshielo']:.0f} min: "
                      "la cámara aún puede estar recuperándose.")

    # ---- veredicto de coincidencia temperatura <-> consumo ------------------
    r_c = datos.get("consumo_vs_esperado_con_compresor")
    e_p = datos.get("energia_vs_habitual_pct")
    coincide = None
    if tendencia == "sube":
        if (r_c is not None and r_c < 0.8) or (e_p is not None and e_p <= -10):
            coincide = ("Sí coincide: mientras la temperatura sube, el consumo eléctrico "
                        "es menor de lo que el compresor produciría trabajando con normalidad.")
        elif r_c is not None or e_p is not None:
            coincide = ("No se ve una caída de consumo: mientras la temperatura sube, el consumo "
                        "es el habitual (el compresor responde); eso apunta más a carga térmica "
                        "que a un problema eléctrico.")
        if coincide:
            datos["coincide_con_consumo"] = coincide.startswith("Sí")

    # ---- 3. hipótesis ----------------------------------------------------
    hip = list(b.hipotesis[:3])
    partes = [linea_t]
    partes += hechos
    if coincide:
        partes.append(coincide)
    hip_out = [{"nombre": h.nombre, "score": round(h.score, 2), "a_favor": list(h.a_favor),
                "en_contra": list(h.en_contra), "faltan": list(h.faltan)} for h in hip]
    if hip and b.principal != "indeterminado":
        top = hip[0]
        a_favor = " ".join(x.rstrip(".") + "." for x in top.a_favor[:3])
        cierre = (f"Hipótesis principal: {b.principal} (confianza {b.confianza:.0%}"
                  f"{', provisional' if b.provisional else ''}). {a_favor}")
        if top.en_contra:
            cierre += " En contra: " + "; ".join(x.rstrip(".") for x in top.en_contra[:2]) + "."
        otras = ", ".join(h.nombre for h in hip[1:])
        if otras:
            cierre += f" Otras hipótesis: {otras}."
        cierre += " Es una hipótesis respaldada por esos datos, no una causa confirmada."
        if top.faltan:
            cierre += f" Para confirmarla faltaría: {top.faltan[0]}."
        partes.append(cierre)
        datos["hipotesis_principal"] = b.principal
        datos["confianza"] = round(float(b.confianza), 2)
    else:
        if tendencia == "sube":
            partes.append("La temperatura sube, pero ninguna regla de diagnóstico alcanza "
                          "evidencia suficiente para proponer una causa con estos datos.")
        else:
            partes.append("No hay evidencia de falla: el comportamiento no se desvía de lo esperado.")
        if hip and hip[0].faltan:
            partes.append(f"Datos que ayudarían: {hip[0].faltan[0]}.")
    return {"ok": True, "respuesta_base": "\n".join(partes), "datos_clave": datos,
            "hipotesis": hip_out}
