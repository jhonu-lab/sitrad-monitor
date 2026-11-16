"""
tools/mock_sitrad_server.py — un SITRAD falso para probar el bot por la ruta de la API real.

    python tools/mock_sitrad_server.py --excel historial.xlsx [--port 8004] [--speed 24] [--advance wall|request]
                                       [--shape a|b]

Sirve (sin TLS, usuario y contraseña cualquiera) las mismas URLs que la API local de SITRAD PRO:
    GET /api/v1/instruments
    GET /api/v1/instruments/{id}/values
    GET /api/v1/alarms?startDate=..&endDate=..
con los datos de un historial (Excel) como si ocurrieran ahora. Luego:
    "sitrad": {"base_url": "http://localhost:8004/api/v1", "user": "x", "password": "x"}   y   run_reporter --mode sitrad

  --advance wall     el tiempo del historial corre con el reloj real (--speed veces más rápido)
  --advance request  cada consulta de /values avanza 2 min (para pruebas automáticas)
  --shape a|b        dos formas distintas de JSON, para comprobar que el bot las entiende

OJO: la forma exacta del JSON de TU servidor puede ser otra. Para ver la real: tools/inspect_sitrad_api.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, ".")
from simulation.excel_replay import load_sitrad_table

COLS = [("camera 01", "°C", "Temp Cámara", "analog"), ("Setpoint", "°C", "Setpoint", "analog"),
        ("Desvío relativo al Setpoint", "°C", "Desvío vs Setpoint", "analog"),
        ("Puerta abierta", "", "Puerta Abierta", "state"), ("DEFR", "", "Estado Deshielo", "state"),
        ("REFR", "", "Estado Refrigeración", "state"), ("SALIDA HOT", "", "Salida HOT", "state")]


class Mock:
    def __init__(self, rows, speed, advance, shape):
        self.rows, self.speed, self.advance, self.shape = rows, speed, advance, shape
        self.t0_real = time.monotonic()
        self.i = 0
        self.lock = threading.Lock()
        self.alarms = []
        self._alarm_state = False

    def _row(self):
        with self.lock:
            if self.advance == "request":
                self.i = min(self.i + 1, len(self.rows) - 1)
            else:
                virt = self.rows[0][0] + timedelta(seconds=(time.monotonic() - self.t0_real) * self.speed)
                while self.i + 1 < len(self.rows) and self.rows[self.i + 1][0] <= virt:
                    self.i += 1
            t, vals = self.rows[self.i]
            temp = vals.get("Temp Cámara")
            hot = temp is not None and temp > 8.0
            if hot and not self._alarm_state:
                self.alarms.append({"id": len(self.alarms) + 1, "instrumentId": 1,
                                    "instrumentName": "Cava Colanta (MT-512E)",
                                    "description": f"Temperatura alta: {temp:.1f} °C", "startDate": t.isoformat().replace("+00:00", "Z")})
            self._alarm_state = hot
            return t, vals

    def values(self):
        t, vals = self._row()
        iso, ms = t.isoformat().replace("+00:00", "Z"), int(t.timestamp() * 1000)
        if self.shape == "a":
            out = []
            for i, (name, unit, ch, kind) in enumerate(COLS):
                v = vals.get(ch)
                out.append({"id": 100 + i, "name": name, "value": ("Conectado" if v else "Desconectado") if kind == "state" and v is not None else v,
                            "unit": unit, "date": iso, "enabled": True})
            out.append({"id": 200, "name": "Proceso actual", "value": "Refrigeración" if vals.get("Estado Refrigeración") else "Reposo",
                        "unit": "", "date": iso, "enabled": True})
            return out
        sensors, outputs = [], []
        for i, (name, unit, ch, kind) in enumerate(COLS):
            v = vals.get(ch)
            d = {"description": name, "currentValue": (1 if v else 0) if kind == "state" and v is not None else v,
                 "unitOfMeasure": unit, "readAt": ms}
            (sensors if kind == "analog" else outputs).append(d)
        return [{"instrumentId": 1, "sensors": sensors, "outputs": outputs}]


def make_handler(mock: Mock):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            body = json.dumps({"status": 200, "results": obj}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path.replace("/api/v1", "", 1)
            if path == "/instruments":
                return self._send([{"id": 1, "name": "Cava Colanta (MT-512E)", "address": 1, "status": "online"},
                                   {"id": 2, "name": "Multipower M1/M2", "address": 2, "status": "online"}])
            if path == "/instruments/1/values":
                return self._send(mock.values())
            if path == "/instruments/2/values":
                return self._send([{"id": 300, "name": "Tensión de la Fase 1", "value": 230.1, "unit": "Vac",
                                    "date": datetime.now(timezone.utc).isoformat()}])
            if path == "/alarms":
                return self._send(list(mock.alarms))
            self.send_response(404)
            self.end_headers()
    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", required=True)
    ap.add_argument("--port", type=int, default=8004)
    ap.add_argument("--speed", type=float, default=24.0)
    ap.add_argument("--advance", default="wall", choices=["wall", "request"])
    ap.add_argument("--shape", default="a", choices=["a", "b"])
    a = ap.parse_args()
    rows, info = load_sitrad_table(a.excel)
    mock = Mock(rows, a.speed, a.advance, a.shape)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(mock))
    print(f"SITRAD falso en http://127.0.0.1:{a.port}/api/v1 · {info['filas']} filas desde "
          f"{info['primera']:%d/%m/%Y %H:%M} · avance={a.advance} · forma={a.shape}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
