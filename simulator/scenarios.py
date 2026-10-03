"""
Define qué pasa en cada "tick" del simulador.

Tick 0-4:   Todo normal
Tick 5-9:   MT-512E reporta "Err" en Sensor Evaporador
Tick 10-14: PCT-3001 pasa a Offline
Tick 15-19: RCK-862 reporta presión muy fuera de rango (CRÍTICO)
Tick 20+:   Todo se recupera
"""
from simulator.device_factory import DeviceFactory


class ScenarioTimeline:
    def get_snapshot(self, tick):
        return [
            self._pct_3001(tick),
            self._mt_512e(tick),
            self._rck_862(tick),
        ]

    def _pct_3001(self, tick):
        """Controlador de presión. Offline entre ticks 10-14."""
        return DeviceFactory.pct_3001_plus(
            instrument_id=101,
            name="Compresor Cámara 1",
            suction_psi=45.2,
            discharge_psi=180.5,
            demand_pct=78,
            offline=(10 <= tick <= 14),
        )

    def _mt_512e(self, tick):
        """Termostato. Sensor Evaporador falla entre ticks 5-9."""
        return DeviceFactory.mt_512e_log(
            instrument_id=102,
            name="Cámara Congelados",
            ambient_c=-18.2,
            evaporator=("Err" if 5 <= tick <= 9 else -22.5),
            product_c=-17.8,
        )

    def _rck_862(self, tick):
        """Rack. Presión se sale de rango entre ticks 15-19."""
        return DeviceFactory.rck_862_plus(
            instrument_id=103,
            name="Rack Refrigeración Sala",
            line_psi=(95.0 if 15 <= tick <= 19 else 68.0),
            return_c=4.5,
        )


SCENARIOS = ScenarioTimeline()