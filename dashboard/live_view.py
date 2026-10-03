"""
Vista de terminal en vivo usando rich.

NOTA IMPORTANTE SOBRE WINDOWS:
  Se usa `screen=False` (modo inline) porque el *alternate screen buffer*
  de rich (`screen=True`) falla en varias terminales Windows (PowerShell,
  cmd) provocando que la pantalla quede en blanco o que el terminal envíe
  un Ctrl+C simulado.

  El modo inline funciona en Windows, Linux, macOS y WSL sin cambios.
"""
from datetime import datetime
from typing import List, Optional
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.models import Severity


class LiveDashboard:
    """Dashboard de terminal que se actualiza sin parpadeo."""

    def __init__(self, refresh_per_second: int = 2):
        self.console = Console()
        # screen=False → modo inline, compatible con Windows
        self.live = Live(
            self._empty_layout(),
            refresh_per_second=refresh_per_second,
            screen=False,
            transient=False,
            console=self.console,
        )
        self.start_time = datetime.now()
        self.counters = {"CRÍTICO": 0, "ADVERTENCIA": 0, "RECUPERACIÓN": 0}
        self.recent_alerts: List[str] = []
        self.last_instruments: List[dict] = []
        self.cycle = 0

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *exc):
        try:
            self.live.stop()
        except Exception:
            # Nunca dejar que un fallo del dashboard tumbe el worker
            pass

    def update(self, cycle: int, instruments: List[dict],
               new_alerts: Optional[List[str]] = None) -> None:
        """Actualiza el dashboard con el estado del ciclo actual."""
        try:
            self.cycle = cycle
            self.last_instruments = instruments or []
            if new_alerts:
                for a in new_alerts:
                    self.recent_alerts.insert(0, a)
                self.recent_alerts = self.recent_alerts[:8]
            self.live.update(self._render())
        except Exception as e:
            # El dashboard nunca debe romper el worker
            self.console.print(f"[dim]Dashboard warning: {e}[/dim]")

    def bump_counter(self, severity: Severity) -> None:
        """Incrementa el contador global por severidad."""
        self.counters[severity.value] = self.counters.get(severity.value, 0) + 1

    def bump_recovery(self) -> None:
        """Incrementa el contador de recuperaciones."""
        self.counters["RECUPERACIÓN"] = self.counters.get("RECUPERACIÓN", 0) + 1

    # ------------------------------------------------------------------ #
    # RENDER
    # ------------------------------------------------------------------ #
    def _empty_layout(self) -> Layout:
        return Layout(name="root")

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=2),
            Layout(name="alerts", ratio=1),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="devices", ratio=3),
            Layout(name="counters", ratio=1),
        )
        layout["header"].update(self._render_header())
        layout["devices"].update(self._render_devices())
        layout["counters"].update(self._render_counters())
        layout["alerts"].update(self._render_recent_alerts())
        layout["footer"].update(self._render_footer())
        return layout

    def _render_header(self) -> Panel:
        txt = Text.from_markup(
            "[bold cyan]MONITOR SITRAD PRO[/bold cyan]  "
            "[white]·[/white]  "
            f"[yellow]Ciclo #{self.cycle}[/yellow]  "
            "[white]·[/white]  "
            f"[dim]Inicio: {self.start_time.strftime('%H:%M:%S')}[/dim]  "
            "[white]·[/white]  "
            f"[dim]Ahora: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )
        return Panel(txt, border_style="cyan")

    def _render_devices(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta",
                      expand=True)
        table.add_column("Equipo", style="cyan", no_wrap=True)
        table.add_column("Modelo", style="dim")
        table.add_column("Estado", justify="center")
        table.add_column("Sensor", style="white")
        table.add_column("Valor", justify="right")
        table.add_column("SP ±H", justify="right", style="dim")

        for inst in self.last_instruments:
            status = inst.get("status", "?")
            status_txt = Text(
                status,
                style="green" if status == "Online" else "red bold",
            )
            sensors = inst.get("sensors", [])
            first_row = True
            for s in sensors:
                value = s.get("value")
                if isinstance(value, str) and value.upper() == "ERR":
                    value_txt = Text("Err", style="red bold")
                elif value is None:
                    value_txt = Text("—", style="dim")
                else:
                    sp = s.get("setpoint")
                    h = s.get("hysteresis")
                    out = (
                        isinstance(value, (int, float))
                        and sp is not None and h is not None
                        and abs(value - sp) > h
                    )
                    value_txt = Text(
                        f"{value} {s.get('unit', '')}",
                        style="yellow bold" if out else "white",
                    )

                sp_h = ""
                if s.get("setpoint") is not None and s.get("hysteresis") is not None:
                    sp_h = f"{s['setpoint']} ±{s['hysteresis']}"

                table.add_row(
                    inst.get("name", "") if first_row else "",
                    inst.get("model", "") if first_row else "",
                    status_txt if first_row else "",
                    s.get("name", ""),
                    value_txt,
                    sp_h,
                )
                first_row = False

        return Panel(table, title="[bold]Instrumentos[/bold]",
                     border_style="blue")

    def _render_counters(self) -> Panel:
        t = Table(show_header=False, expand=True)
        t.add_column("Sev", style="bold")
        t.add_column("N", justify="right")

        colors = {"CRÍTICO": "red", "ADVERTENCIA": "yellow",
                  "RECUPERACIÓN": "green"}
        icons = {"CRÍTICO": "🔴", "ADVERTENCIA": "🟡", "RECUPERACIÓN": "🟢"}

        for sev, n in self.counters.items():
            t.add_row(
                Text(f"{icons[sev]} {sev}", style=colors[sev]),
                Text(str(n), style=colors[sev]),
            )
        return Panel(t, title="[bold]Contadores[/bold]",
                     border_style="magenta")

    def _render_recent_alerts(self) -> Panel:
        if not self.recent_alerts:
            body = Text("Sin alertas recientes.", style="dim")
        else:
            body = Text("\n".join(f"  {a}" for a in self.recent_alerts))
        return Panel(body, title="[bold]Últimas alertas[/bold]",
                     border_style="yellow")

    def _render_footer(self) -> Panel:
        uptime = datetime.now() - self.start_time
        txt = Text.from_markup(
            f"[dim]Uptime: {str(uptime).split('.')[0]}[/dim]   "
            f"[dim]|[/dim]   [dim]Ctrl+C para detener[/dim]"
        )
        return Panel(txt, border_style="dim")