#!/usr/bin/env python3
"""
DEMOSTRACIÓN END-TO-END.

Uso:
    python run_demo.py             → dashboard en vivo (rich, inline)
    python run_demo.py --simple    → sin dashboard, prints directos
                                     (modo a prueba de balas para Windows)

El modo --simple es útil si tu terminal Windows no soporta bien rich.
"""
import argparse
import json
import time
import logging
import sys
from rich.console import Console
from rich.panel import Panel

from simulator.fake_sitrad_api import run_server, advance_tick, reset_ticks
from core.sitrad_client import SitradClient
from core.worker import MonitorWorker
from notifiers.file_log import FileLogNotifier
from notifiers.callback import CallbackNotifier
from notifiers.console import ConsoleNotifier

# Silenciar logs ruidosos de Flask
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

console = Console()
CONFIG_FILE = "config.json"


def load_config(path: str = CONFIG_FILE) -> dict:
    """Carga la configuración desde JSON (sin dependencias externas)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        console.print(f"[red]No se encontró {path}.[/red]")
        sys.exit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]Error parseando {path}: {e}[/red]")
        sys.exit(1)


def banner(simple: bool):
    modo = "SIMPLE (sin dashboard)" if simple else "DASHBOARD EN VIVO"
    console.print(Panel.fit(
        "[bold cyan]MONITOR SITRAD PRO — DEMOSTRACIÓN END-TO-END[/bold cyan]\n"
        f"[white]Modo: {modo}[/white]\n"
        "[dim]Simulador → Cliente HTTP → Reglas → Deduplicación → Notificadores[/dim]",
        border_style="cyan",
    ))


def run_simple_mode(cfg, ciclos_total=20, intervalo=1.5):
    """Modo simple: sin dashboard. Imprime alertas a medida que se emiten."""
    console.print("[yellow]▶ Iniciando simulador de Sitrad PRO en :8001...[/yellow]")
    run_server(port=8001)
    time.sleep(1.5)
    reset_ticks()
    console.print("[green]✔ Simulador listo.[/green]\n")

    host = (cfg["sitrad"]["demo_host"] if cfg["mode"] == "demo"
            else cfg["sitrad"]["production_host"])
    client = SitradClient(host, cfg["sitrad"]["verify_ssl"],
                          cfg["sitrad"]["timeout"])

    notifiers = [
        ConsoleNotifier(),
        FileLogNotifier("logs/alerts.jsonl"),
    ]
    worker = MonitorWorker(
        client, notifiers,
        cooldown=cfg["rules"]["cooldown_seconds"],
        required_consecutive=cfg["rules"]["required_consecutive"],
    )

    console.print(
        f"[bold]Ejecutando {ciclos_total} ciclos "
        f"(uno cada {intervalo}s)...[/bold]\n"
    )
    try:
        for i in range(1, ciclos_total + 1):
            console.print(f"[dim]── Ciclo #{i}/{ciclos_total} ──[/dim]",
                          highlight=False)
            worker.run_cycle()
            advance_tick()
            time.sleep(intervalo)
    except KeyboardInterrupt:
        console.print("\n[yellow]Demo interrumpida.[/yellow]")

    console.print(Panel.fit(
        "[bold green]✔ DEMOSTRACIÓN COMPLETADA[/bold green]\n"
        "Revisa [cyan]logs/alerts.jsonl[/cyan] para el registro histórico.",
        border_style="green",
    ))


def run_dashboard_mode(cfg, ciclos_total=20, intervalo=1.5):
    """Modo con dashboard rich en vivo (inline)."""
    from dashboard.live_view import LiveDashboard

    console.print("[yellow]▶ Iniciando simulador de Sitrad PRO en :8001...[/yellow]")
    run_server(port=8001)
    time.sleep(1.5)
    reset_ticks()
    console.print("[green]✔ Simulador listo.[/green]\n")

    host = (cfg["sitrad"]["demo_host"] if cfg["mode"] == "demo"
            else cfg["sitrad"]["production_host"])
    client = SitradClient(host, cfg["sitrad"]["verify_ssl"],
                          cfg["sitrad"]["timeout"])

    dash = LiveDashboard(refresh_per_second=2)

    def on_alert(alert):
        dash.bump_counter(alert.severity)

    notifiers = [
        FileLogNotifier("logs/alerts.jsonl"),
        CallbackNotifier(on_alert),
    ]
    worker = MonitorWorker(
        client, notifiers,
        cooldown=cfg["rules"]["cooldown_seconds"],
        required_consecutive=cfg["rules"]["required_consecutive"],
    )

    console.print(
        f"[bold]Ejecutando {ciclos_total} ciclos con dashboard.[/bold] "
        "[dim]Ctrl+C para detener.[/dim]\n"
    )
    time.sleep(1.5)

    try:
        with dash:
            for i in range(1, ciclos_total + 1):
                alerts = worker.run_cycle()
                dash.update(
                    cycle=worker.cycle,
                    instruments=worker.last_snapshot,
                    new_alerts=[a.format_oneline() for a in alerts],
                )
                advance_tick()
                time.sleep(intervalo)
    except KeyboardInterrupt:
        console.print("\n[yellow]Demo interrumpida por el usuario.[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Error en el dashboard: {e}[/red]")
        console.print("[yellow]Reintenta con: python run_demo.py --simple[/yellow]")
        return

    console.print(Panel.fit(
        "[bold green]✔ DEMOSTRACIÓN COMPLETADA[/bold green]\n"
        "Revisa [cyan]logs/alerts.jsonl[/cyan] para el registro histórico.",
        border_style="green",
    ))


def main():
    parser = argparse.ArgumentParser(description="Demo Monitor Sitrad PRO")
    parser.add_argument("--simple", action="store_true",
                        help="Ejecutar sin dashboard (modo seguro Windows)")
    parser.add_argument("--ciclos", type=int, default=20,
                        help="Número de ciclos a ejecutar (default: 20)")
    parser.add_argument("--intervalo", type=float, default=1.5,
                        help="Segundos entre ciclos (default: 1.5)")
    args = parser.parse_args()

    banner(args.simple)
    cfg = load_config()

    if args.simple:
        run_simple_mode(cfg, args.ciclos, args.intervalo)
    else:
        run_dashboard_mode(cfg, args.ciclos, args.intervalo)


if __name__ == "__main__":
    main()