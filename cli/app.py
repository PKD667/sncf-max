"""CLI for TGV Max. Thin composition of core modules.

Usage:
    sncf-max search paris lyon
    sncf-max hunt paris
    sncf-max watch paris lyon --auto-book
    sncf-max trips
"""

import sys
import os
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    import click
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.text import Text
    from rich.live import Live
    from rich import box
except ImportError:
    print("rich and click are required. Install with: pip install rich click")
    sys.exit(1)

from network.core import search, broadcast, SearchResult
from api import TGVMaxAPI
from config import SNCFConfig, get_station_name, STATIONS, default_config
from network.client import SNCFMaxClient
from network.decomposition import CompositeTrip
from network.finder import hunt

console = Console()
STYLE = {
    "ok": "green bold",
    "err": "red bold",
    "warn": "yellow",
    "info": "cyan",
    "muted": "bright_black",
    "train": "blue bold",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_route(route: str):
    parts = route.lower().replace(" ", "-").split("-")
    if len(parts) != 2:
        return None, None, None
    return parts[0], parts[1], get_station_name(parts[0])


def _parse_date(val: str | None) -> date:
    if not val:
        return date.today() + timedelta(days=1)
    if val.startswith("+"):
        return date.today() + timedelta(days=int(val[1:]))
    return datetime.strptime(val, "%Y-%m-%d").date()


def _trip_row(trip) -> list:
    return [
        f"[bold]{trip.train_number}[/bold]",
        trip.departure_date.strftime("%a %d/%m"),
        trip.departure_time.strftime("%H:%M"),
        trip.arrival_time.strftime("%H:%M"),
        f"{str(trip.origin)[:20]} -> {str(trip.destination)[:20]}",
        "[green]MAX[/green]" if trip.is_free else f"~{trip.price_display}",
    ]


def _print_trips(trips, title="Free Trips"):
    if not trips:
        console.print("  No trips", style=STYLE["muted"])
        return
    t = Table(title=title, box=box.ROUNDED, header_style="bold cyan")
    for h in ("Train", "Date", "Dep", "Arr", "Route", "Status"):
        t.add_column(h)
    for tr in trips[:30]:
        t.add_row(*_trip_row(tr))
    console.print(t)
    if len(trips) > 30:
        console.print(f"  + {len(trips)-30} more", style=STYLE["muted"])


def _print_composites(comps: list[CompositeTrip]):
    if not comps:
        return
    t = Table(title="All-MAX Decomposed", box=box.ROUNDED, header_style="bold cyan")
    for h in ("Dep->Arr", "Legs", "Duration"):
        t.add_column(h)
    for c in comps[:15]:
        route = " -> ".join(
            f"{str(leg.trip.origin)[:12]}[{leg.trip.departure_time.strftime('%H:%M')}]"
            for leg in c.legs
        ) + f" -> {str(c.destination)[:12]}[{c.arrival_time.strftime('%H:%M')}]"
        dur = f"{c.total_duration.total_seconds()//3600:.0f}h{(c.total_duration.total_seconds()%3600)//60:.0f}m"
        t.add_row(route, str(c.max_legs), dur)
    console.print(t)


# ---------------------------------------------------------------------------
# click group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--debug", is_flag=True)
@click.pass_context
def cli(ctx, debug):
    ctx.ensure_object(dict)
    cfg = SNCFConfig.from_env()
    if debug:
        cfg.DEBUG = True
    ctx.obj["config"] = cfg
    ctx.obj["api"] = TGVMaxAPI(config=cfg)
    ctx.obj["client"] = SNCFMaxClient(config=cfg)


# -- search ----------------------------------------------------------------


@cli.command()
@click.argument("route")
@click.option("--date", "-d", default=None)
@click.option("--decompose/--no-decompose", default=True)
@click.pass_context
def search_cmd(ctx, route, date, decompose):
    """Find free TGV Max trips for a route.

    \b
    ROUTE: origin-destination. E.g. paris-lyon, bordeaux-lille.
    """
    origin, dest, origin_full = _parse_route(route)
    if not origin or not dest:
        console.print("Invalid route. Use origin-destination", style=STYLE["err"])
        return
    dest_full = get_station_name(dest)
    trip_date = _parse_date(date)

    console.print(f"\nSearch: [bold]{origin_full}[/bold] -> [bold]{dest_full}[/bold] {trip_date}\n")

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as p:
        p.add_task("Searching...", total=None)
        result = search(origin, dest, trip_date, decompose=decompose)

    console.print(f"Direct MAX: [green]{len(result.direct_free)}[/green]  "
                  f"Decomposed MAX: [green]{sum(1 for c in result.decompositions if c.is_fully_max)}[/green]\n")

    if result.direct_free:
        _print_trips(result.direct_free, "Direct Free (MAX)")
    else:
        console.print("No direct MAX trips found", style=STYLE["err"])

    max_only = [c for c in result.decompositions if c.is_fully_max]
    if max_only:
        console.print()
        _print_composites(max_only)

    if not result.has_any_free:
        console.print("\nTry --date +7 or a different route", style=STYLE["info"])


# -- hunt ------------------------------------------------------------------


@cli.command()
@click.argument("origin", default="paris")
@click.option("--date", "-d", default=None)
@click.pass_context
def hunt_cmd(ctx, origin, date):
    """Find ALL free trips from a station (broadcast).

    \b
    Scans 50+ destinations in parallel.
    """
    trip_date = _parse_date(date)
    console.print(f"\nHunting free trips from [bold]{get_station_name(origin)}[/bold] {trip_date}\n")

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as p:
        p.add_task("Hunting... (parallel scan)", total=None)
        report = hunt(origin, trip_date)

    console.print(f"Found [green]{report.total_free}[/green] free trips\n")

    for b in report.buckets:
        if b.trips:
            console.print(f"[cyan]{b.label}:[/cyan] {len(b.trips)} ({b.note})")

    all_t = report.all_free_trips
    if all_t:
        console.print()
        _print_trips(all_t[:30], "All Free Trips")


# -- watch (live scan) -----------------------------------------------------


@cli.command()
@click.argument("route")
@click.option("--date", "-d", default=None)
@click.option("--interval", "-i", default=60, help="Seconds between checks")
@click.option("--auto-book/--no-auto-book", default=False)
@click.pass_context
def watch(ctx, route, date, interval, auto_book):
    """Continuously watch for new MAX availability."""
    origin, dest, _ = _parse_route(route)
    if not origin:
        return
    trip_date = _parse_date(date)

    console.print(f"\nWatching [bold]{get_station_name(origin)} -> {get_station_name(dest)}[/bold] {trip_date}")
    console.print(f"  Interval: {interval}s | Auto-book: {auto_book}\n")

    # track what we've seen
    seen: set[str] = set()

    try:
        while True:
            r = search(origin, dest, trip_date, decompose=False)
            new = [t for t in r.direct_free if t.trip_key not in seen]
            for t in new:
                seen.add(t.trip_key)
                console.print(f"[green]NEW:[/green] {t}")
                if auto_book:
                    cfg = ctx.obj["config"]
                    if cfg.SNCF_EMAIL and cfg.SNCF_PASSWORD:
                        api = ctx.obj["api"]
                        api.login(cfg.SNCF_EMAIL, cfg.SNCF_PASSWORD)
                        res = api.book(t)
                        console.print(f"  Booked: {res}" if res.is_success else f"  Failed: {res.message}")
            import time
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\nStopped", style=STYLE["muted"])


# -- trips ----------------------------------------------------------------


@cli.command()
@click.pass_context
def trips(ctx):
    """View booked TGV Max trips."""
    cfg = ctx.obj["config"]
    if not cfg.SNCF_EMAIL or not cfg.SNCF_PASSWORD:
        console.print("Set SNCF_EMAIL and SNCF_PASSWORD env vars", style=STYLE["err"])
        return

    try:
        from booking.voyages import fetch_my_trips_sync
        from booking.auth import load_or_login

        console.print("\nLoading trips...")
        session = load_or_login(cfg.SNCF_EMAIL, cfg.SNCF_PASSWORD, cfg)
        status = fetch_my_trips_sync(session, cfg)

        console.print(Panel(
            f"Trips: [bold]{status.upcoming_count}/6[/bold] | Available slots: [green]{status.slots_available}[/green]",
            title="Your TGV Max"))
        for trip in status.trips:
            flag = "[green]active[/green]" if trip.is_upcoming else "[dim]past[/dim]"
            console.print(f"  {trip.reference} | {trip.train_number} {trip.departure_date} "
                          f"{trip.departure_time.strftime('%H:%M')} - {trip.arrival_time.strftime('%H:%M')} | {flag}")
    except ImportError:
        console.print("Playwright required: pip install playwright", style=STYLE["err"])
    except Exception as e:
        console.print(f"Error: {e}", style=STYLE["err"])


# -- stations --------------------------------------------------------------


@cli.command()
def stations():
    """List station aliases."""
    t = Table(title="Station Aliases", box=box.ROUNDED)
    t.add_column("Alias", style="bold cyan")
    t.add_column("Station")
    for k, v in sorted(STATIONS.items()):
        t.add_row(k, v)
    console.print(t)


# -- server ----------------------------------------------------------------


@cli.command()
@click.option("--port", "-p", default=5000)
def serve(port):
    """Start web frontend."""
    import subprocess, sys
    frontend = PROJECT_ROOT / "frontend" / "server.py"
    subprocess.run([sys.executable, str(frontend)], env={**os.environ, "FLASK_RUN_PORT": str(port)})


if __name__ == "__main__":
    cli()


def main():
    cli()
