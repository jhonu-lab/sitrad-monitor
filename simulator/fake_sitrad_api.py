"""
Simulador de la API REST de Sitrad PRO.

Replica el formato JSON exacto que Sitrad PRO devuelve en producción
(basado en la documentación oficial de Full Gauge Controls).

Endpoints:
    GET /api/instruments              → Lista de instrumentos
    GET /api/instruments/{id}/values  → Valores actuales del instrumento
"""
import threading
from flask import Flask, jsonify
from simulator.scenarios import SCENARIOS

app = Flask(__name__)
_state = {"tick": 0}
_lock = threading.Lock()


def _get_current_snapshot():
    """Devuelve el estado actual de todos los instrumentos según el tick."""
    with _lock:
        tick = _state["tick"]
    return SCENARIOS.get_snapshot(tick)


@app.route("/api/instruments", methods=["GET"])
def list_instruments():
    snapshot = _get_current_snapshot()
    return jsonify({
        "instruments": [
            {"id": inst["id"], "name": inst["name"], "model": inst["model"]}
            for inst in snapshot
        ]
    })


@app.route("/api/instruments/<int:instrument_id>/values", methods=["GET"])
def get_values(instrument_id):
    snapshot = _get_current_snapshot()
    for inst in snapshot:
        if inst["id"] == instrument_id:
            return jsonify(inst)
    return jsonify({"error": "not found"}), 404


def advance_tick():
    """Avanza el reloj interno del simulador (llamado por el runner)."""
    with _lock:
        _state["tick"] += 1


def reset_ticks():
    """Reinicia el contador de ticks (útil entre demos)."""
    with _lock:
        _state["tick"] = 0


def run_server(host="127.0.0.1", port=8001):
    """Levanta el servidor en un thread daemon (no bloquea el runner)."""
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False,
                               use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread