#!/usr/bin/env python3
"""Command-line interface for SNCF Max.

A beautiful CLI for discovering and booking TGV Max trips.

Usage:
    sncf-max search paris lyon                    # Search trips
    sncf-max search paris lyon --date 2025-01-15  # Search specific date
    sncf-max trips                                # View your booked trips
    sncf-max watch paris lyon                     # Watch for availability
    sncf-max schedule                             # Manage recurring schedules
"""

import sys
import os
from datetime import date, datetime, timedelta
from typing import Optional, List
import logging
from pathlib import Path
import shlex

try:
    import click
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
    from rich.style import Style
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    click = None

# Ensure the library code in src/ is importable when running `python -m cli`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
SRC_STR = str(SRC_DIR)
if SRC_STR not in sys.path:
    sys.path.insert(0, SRC_STR)

# Import from the library (flat modules under src/)
from config import SNCFConfig, default_config, get_station_name, STATIONS
from client import SNCFMaxClient
from api import TGVMaxAPI
from models import Trip, BookingStatus
from scheduler import TGVMaxScheduler, Weekday, TimeWindow, RecurringTrip


if not RICH_AVAILABLE:
    print("Rich and Click are required for CLI. Install with:")
    print("  pip install rich click")
    sys.exit(1)


# Rich console
console = Console()

# Logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(message)s',
)


# =============================================================================
# STYLING
# =============================================================================

STYLE_SUCCESS = Style(color="green", bold=True)
STYLE_ERROR = Style(color="red", bold=True)
STYLE_WARNING = Style(color="yellow")
STYLE_INFO = Style(color="cyan")
STYLE_MUTED = Style(color="bright_black")
STYLE_TRAIN = Style(color="blue", bold=True)
STYLE_PRICE_FREE = Style(color="green", bold=True)
STYLE_PRICE_PAID = Style(color="yellow")


def print_banner():
    """Print the app banner."""
    banner = """
╔══════════════════════════════════════════════════════════╗
║  🚄  SNCF MAX CLI                                        ║
║      Automated TGV Max Discovery & Booking               ║
╚══════════════════════════════════════════════════════════╝
"""
    console.print(banner, style="bold blue")


def print_trip_table(trips: List[Trip], title: str = "Available Trips"):
    """Print trips in a beautiful table."""
    if not trips:
        console.print("  No trips found", style=STYLE_MUTED)
        return
    
    table = Table(
        title=title,
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="blue",
    )
    
    table.add_column("Train", style="bold", justify="center")
    table.add_column("Date", justify="center")
    table.add_column("Departure", justify="center")
    table.add_column("Arrival", justify="center")
    table.add_column("Route", min_width=30)
    table.add_column("Status", justify="center")
    
    for trip in trips[:20]:  # Limit to 20
        # Shorten station names
        origin = str(trip.origin)[:20]
        dest = str(trip.destination)[:20]
        
        status = "✅ Available" if trip.available_for_max.value == "OUI" else "❌"
        
        table.add_row(
            f"[bold]{trip.train_number}[/bold]",
            trip.departure_date.strftime("%a %d/%m"),
            trip.departure_time.strftime("%H:%M"),
            trip.arrival_time.strftime("%H:%M"),
            f"{origin} → {dest}",
            status,
        )
    
    console.print(table)
    
    if len(trips) > 20:
        console.print(f"  ... and {len(trips) - 20} more", style=STYLE_MUTED)


def format_price(max_legs: int, paid_legs: int) -> str:
    """Format the price display for alternatives."""
    if paid_legs == 0:
        return "[green bold]0€ (FREE)[/green bold]"
    else:
        # Estimated price per paid leg (rough estimate)
        estimated_price = paid_legs * 30  # ~30€ per TGV leg
        return f"[yellow]~{estimated_price}€[/yellow] ({paid_legs} paid)"


def print_alternatives_table(alternatives, title: str = "Trip Alternatives (Sorted by Price)") -> None:
    """Render a table of decomposed alternatives."""
    if not alternatives:
        console.print("❌ No multi-leg alternatives found", style=STYLE_ERROR)
        return

    # Sort alternatives by price (paid_legs first, then by total duration)
    alts_sorted = sorted(alternatives, key=lambda a: (a.paid_legs, a.total_duration.total_seconds()))

    table = Table(
        title=title,
        box=box.ROUNDED,
        header_style="bold cyan",
    )

    table.add_column("#", justify="center", width=3)
    table.add_column("Price", justify="center", min_width=14)
    table.add_column("Route", min_width=35)
    table.add_column("Departure", justify="center")
    table.add_column("Arrival", justify="center")
    table.add_column("Duration", justify="center")
    table.add_column("Legs", justify="center")

    for i, alt in enumerate(alts_sorted[:15], 1):
        legs_str = " → ".join(
            [f"{str(leg.trip.origin)[:12]}" for leg in alt.legs]
        ) + f" → {str(alt.legs[-1].trip.destination)[:12]}"

        duration = f"{int(alt.total_duration.total_seconds() // 3600)}h{int((alt.total_duration.total_seconds() % 3600) // 60):02d}"
        price_str = format_price(alt.max_legs, alt.paid_legs)

        if alt.is_fully_max:
            legs_str_type = f"[green]{alt.max_legs} MAX[/green]"
        else:
            legs_str_type = f"[yellow]{alt.max_legs}M + {alt.paid_legs}P[/yellow]"

        table.add_row(
            str(i),
            price_str,
            legs_str,
            alt.departure_time.strftime("%H:%M"),
            alt.arrival_time.strftime("%H:%M"),
            duration,
            legs_str_type,
        )

    console.print(table)

    if len(alts_sorted) > 15:
        console.print(f"\n  ... and {len(alts_sorted) - 15} more alternatives", style=STYLE_MUTED)

    console.print("\n[dim]💡 Legend: M = MAX (free), P = Paid leg (~30€ each)[/dim]")


# =============================================================================
# CLI COMMANDS
# =============================================================================

@click.group()
@click.option('--debug', is_flag=True, help='Enable debug mode')
@click.option('--proxy', help='Proxy URL (e.g., http://host:port)')
@click.option(
    '--session-file',
    type=click.Path(dir_okay=False, writable=True, readable=True, path_type=Path),
    help='Path to a JSON session file (cookies/tokens)',
)
@click.pass_context
def cli(ctx, debug, proxy, session_file):
    """🚄 SNCF Max CLI - Discover and book TGV Max trips."""
    ctx.ensure_object(dict)
    
    config = SNCFConfig.from_env()
    if debug:
        config.DEBUG = True
        logging.getLogger().setLevel(logging.DEBUG)
    if proxy:
        config.PROXY = proxy
    
    ctx.obj['config'] = config
    ctx.obj['session_file'] = session_file
    ctx.obj['api'] = TGVMaxAPI(config=config)
    ctx.obj['client'] = SNCFMaxClient(config=config)


@cli.command()
@click.argument('route')
@click.option('--date', '-d', 'trip_date', help='Date (YYYY-MM-DD or +N for days from now)')
@click.option('--days', '-n', default=1, help='Number of days to search')
@click.option('--time', '-t', 'time_filter', help='Filter by departure time (e.g., 08:00-12:00)')
@click.option('--alternatives', 'show_alternatives', is_flag=True, help='Also search multi-leg alternatives')
@click.pass_context
def search(ctx, route: str, trip_date: Optional[str], days: int, time_filter: Optional[str], show_alternatives: bool):
    """Search for available TGV Max trips.
    
    ROUTE: Origin-destination (e.g., paris-lyon, bordeaux-lille)
    
    Examples:
    
        sncf-max search paris-lyon
        
        sncf-max search paris-marseille --date 2025-01-15
        
        sncf-max search lyon-paris --date +7 --time 17:00-21:00
    """
    # Parse route
    parts = route.lower().replace(' ', '-').split('-')
    if len(parts) != 2:
        console.print("❌ Invalid route format. Use: origin-destination", style=STYLE_ERROR)
        return
    
    origin, destination = parts
    origin_full = get_station_name(origin)
    dest_full = get_station_name(destination)
    
    # Parse date
    if trip_date:
        if trip_date.startswith('+'):
            target_date = date.today() + timedelta(days=int(trip_date[1:]))
        else:
            target_date = datetime.strptime(trip_date, "%Y-%m-%d").date()
    else:
        target_date = date.today() + timedelta(days=1)  # Tomorrow
    
    # Parse time filter
    time_min, time_max = None, None
    if time_filter:
        parts = time_filter.split('-')
        if len(parts) == 2:
            time_min = datetime.strptime(parts[0], "%H:%M").time()
            time_max = datetime.strptime(parts[1], "%H:%M").time()
    
    console.print(f"\n🔍 Searching: [bold]{origin_full}[/bold] → [bold]{dest_full}[/bold]")
    console.print(f"   Date: {target_date.strftime('%A %d %B %Y')}", style=STYLE_MUTED)
    
    client: SNCFMaxClient = ctx.obj['client']
    
    all_trips: List[Trip] = []
    all_alternatives = []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Searching...", total=None)
        
        # Direct MAX trips
        for i in range(days):
            d = target_date + timedelta(days=i)
            trips = client.search_trips(
                origin=origin,
                destination=destination,
                trip_date=d,
                only_available=True
            )
            
            # Filter by time
            if time_min and time_max:
                trips = [t for t in trips if time_min <= t.departure_time <= time_max]
            
            all_trips.extend(trips)
        
        # Optional: also search alternatives (multi-leg decomposition)
        if show_alternatives:
            from decomposition import TripDecomposer
            
            decomposer = TripDecomposer(config=ctx.obj['config'])
            for i in range(days):
                d = target_date + timedelta(days=i)
                alts = decomposer.find_alternatives(
                    origin=origin,
                    destination=destination,
                    trip_date=d,
                    include_paid=True,
                )
                all_alternatives.extend(alts)
    
    console.print()
    
    if all_trips:
        console.print(f"✅ Found [bold green]{len(all_trips)}[/bold green] direct TGV Max trips\n")
        print_trip_table(all_trips)
    else:
        console.print("❌ No direct TGV Max availability found", style=STYLE_ERROR)
    
    if show_alternatives:
        if all_alternatives:
            if all_trips:
                console.print("\n💡 Also found multi-leg alternatives (sorted by price):\n", style=STYLE_INFO)
            else:
                console.print("\n💡 Showing multi-leg alternatives instead:\n", style=STYLE_INFO)
            print_alternatives_table(all_alternatives)
        else:
            console.print("\n❌ No multi-leg alternatives found", style=STYLE_ERROR)
    
    if not all_trips and not (show_alternatives and all_alternatives):        
        console.print("\n💡 Tips:", style=STYLE_INFO)
        console.print("   • TGV Max slots are released 30 days ahead around 6 AM")
        console.print("   • Try different dates or set up a watch with: sncf-max watch")


@cli.command()
@click.argument('route')
@click.option('--date', '-d', 'trip_date', required=True, help='Date (YYYY-MM-DD)')
@click.option('--time', '-t', 'preferred_time', help='Preferred departure time (HH:MM)')
@click.pass_context
def book(ctx, route: str, trip_date: str, preferred_time: Optional[str]):
    """Book a TGV Max trip.
    
    ROUTE: Origin-destination (e.g., paris-lyon)
    
    Example:
    
        sncf-max book paris-lyon --date 2025-01-15 --time 18:00
    """
    config: SNCFConfig = ctx.obj['config']
    
    if not config.SNCF_EMAIL or not config.SNCF_PASSWORD:
        console.print("❌ Credentials required", style=STYLE_ERROR)
        console.print("\nSet environment variables:")
        console.print("  export SNCF_EMAIL=your@email.com")
        console.print("  export SNCF_PASSWORD=yourpassword")
        return
    
    # Parse route
    parts = route.lower().split('-')
    if len(parts) != 2:
        console.print("❌ Invalid route format", style=STYLE_ERROR)
        return
    
    origin, destination = parts
    target_date = datetime.strptime(trip_date, "%Y-%m-%d").date()
    
    api: TGVMaxAPI = ctx.obj['api']
    
    console.print(f"\n🎟️  Booking: {origin} → {destination} on {target_date}")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Searching for trips...", total=None)
        
        trips = api.search(origin, destination, target_date, only_available=True)
        
        if not trips:
            console.print("❌ No availability", style=STYLE_ERROR)
            return
        
        # Select trip
        selected = trips[0]
        if preferred_time:
            target = datetime.strptime(preferred_time, "%H:%M").time()
            selected = min(
                trips,
                key=lambda t: abs(
                    datetime.combine(target_date, t.departure_time) -
                    datetime.combine(target_date, target)
                ).total_seconds()
            )
        
        progress.update(task, description=f"Selected: Train {selected.train_number} at {selected.departure_time}")
        progress.update(task, description="Authenticating...")
        
        try:
            api.login(
                config.SNCF_EMAIL,
                config.SNCF_PASSWORD,
                session_file=str(ctx.obj.get('session_file')) if ctx.obj.get('session_file') else None,
            )
        except Exception as e:
            console.print(f"❌ Login failed: {e}", style=STYLE_ERROR)
            return
        
        progress.update(task, description="Booking...")
        result = api.book(selected)
    
    console.print()
    
    if result.is_success:
        console.print(Panel(
            f"✅ [bold green]BOOKING CONFIRMED![/bold green]\n\n"
            f"Train {selected.train_number}\n"
            f"{selected.origin} → {selected.destination}\n"
            f"{selected.departure_date} at {selected.departure_time.strftime('%H:%M')}\n\n"
            f"Reference: [bold]{result.confirmation_number or 'Check your email'}[/bold]",
            title="🎉 Success",
            border_style="green",
        ))
    else:
        console.print(f"❌ Booking failed: {result.message}", style=STYLE_ERROR)


@cli.command()
@click.pass_context
def shell(ctx):
    """Interactive shell for searching and booking trips.
    
    Examples (inside the shell):
    
        search lyon-paris --date 2025-12-04 --alternatives
        book 3
        show
        help
        quit
    """
    config: SNCFConfig = ctx.obj['config']
    api: TGVMaxAPI = ctx.obj['api']
    client: SNCFMaxClient = ctx.obj['client']

    last_trips: List[Trip] = []
    last_alternatives = []

    console.print(Panel(
        "[bold]Interactive SNCF Max Shell[/bold]\n\n"
        "Type commands like:\n"
        "  [cyan]search lyon-paris --date 2025-12-04 --alternatives[/cyan]\n"
        "  [cyan]book 3[/cyan]    (book 3rd trip from last search)\n"
        "  [cyan]show[/cyan]      (show last search results)\n"
        "  [cyan]help[/cyan], [cyan]quit[/cyan]",
        title="🖥️ Shell",
        border_style="blue",
    ))

    while True:
        try:
            raw = Prompt.ask("[bold cyan]sncf-max[/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n👋 Bye", style=STYLE_MUTED)
            break

        if not raw:
            continue

        # Simple commands
        if raw.lower() in {"quit", "exit"}:
            console.print("👋 Bye", style=STYLE_MUTED)
            break
        if raw.lower() == "help":
            console.print(
                "\nCommands:\n"
                "  search ORIGIN-DEST [--date YYYY-MM-DD] [--time HH:MM-HH:MM] [--alternatives]\n"
                "  book N        Book N-th trip from last direct search\n"
                "  show          Show last search results\n"
                "  quit/exit     Leave shell\n",
                style=STYLE_INFO,
            )
            continue
        if raw.lower() == "show":
            if last_trips:
                console.print("\nLast direct trips:\n", style=STYLE_INFO)
                print_trip_table(last_trips)
            else:
                console.print("No trips in history yet (run search first)", style=STYLE_MUTED)
            if last_alternatives:
                console.print("\nLast alternatives:\n", style=STYLE_INFO)
                print_alternatives_table(last_alternatives)
            continue

        # Tokenize command
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            console.print(f"❌ Parse error: {e}", style=STYLE_ERROR)
            continue

        if not parts:
            continue

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd == "search":
            if not args:
                console.print("Usage: search ORIGIN-DEST [--date YYYY-MM-DD] [--time HH:MM-HH:MM] [--alternatives]", style=STYLE_ERROR)
                continue

            route = args[0]
            trip_date_str: Optional[str] = None
            time_filter: Optional[str] = None
            show_alts = False

            i = 1
            while i < len(args):
                tok = args[i]
                if tok in ("-d", "--date") and i + 1 < len(args):
                    trip_date_str = args[i + 1]
                    i += 2
                elif tok in ("-t", "--time") and i + 1 < len(args):
                    time_filter = args[i + 1]
                    i += 2
                elif tok in ("--alternatives", "--alts", "--alt"):
                    show_alts = True
                    i += 1
                else:
                    console.print(f"❌ Unknown option: {tok}", style=STYLE_ERROR)
                    i += 1

            # Reuse the same logic as the search command, but capture results
            parts_route = route.lower().replace(' ', '-').split('-')
            if len(parts_route) != 2:
                console.print("❌ Invalid route format. Use: origin-destination", style=STYLE_ERROR)
                continue

            origin, destination = parts_route
            origin_full = get_station_name(origin)
            dest_full = get_station_name(destination)

            if trip_date_str:
                if trip_date_str.startswith('+'):
                    target_date = date.today() + timedelta(days=int(trip_date_str[1:]))
                else:
                    try:
                        target_date = datetime.strptime(trip_date_str, "%Y-%m-%d").date()
                    except ValueError:
                        console.print("❌ Invalid date format. Use YYYY-MM-DD or +N", style=STYLE_ERROR)
                        continue
            else:
                target_date = date.today() + timedelta(days=1)

            time_min = time_max = None
            if time_filter:
                try:
                    tparts = time_filter.split('-')
                    if len(tparts) == 2:
                        time_min = datetime.strptime(tparts[0], "%H:%M").time()
                        time_max = datetime.strptime(tparts[1], "%H:%M").time()
                except ValueError:
                    console.print("❌ Invalid time filter. Use HH:MM-HH:MM", style=STYLE_ERROR)
                    continue

            console.print(f"\n🔍 Searching: [bold]{origin_full}[/bold] → [bold]{dest_full}[/bold]")
            console.print(f"   Date: {target_date.strftime('%A %d %B %Y')}", style=STYLE_MUTED)

            last_trips = []
            last_alternatives = []

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Searching...", total=None)

                trips = client.search_trips(
                    origin=origin,
                    destination=destination,
                    trip_date=target_date,
                    only_available=True,
                )
                if time_min and time_max:
                    trips = [t for t in trips if time_min <= t.departure_time <= time_max]
                last_trips = trips

                if show_alts:
                    from decomposition import TripDecomposer

                    decomposer = TripDecomposer(config=config)
                    alts = decomposer.find_alternatives(
                        origin=origin,
                        destination=destination,
                        trip_date=target_date,
                        include_paid=True,
                    )
                    last_alternatives = alts

            console.print()

            if last_trips:
                console.print(f"✅ Found [bold green]{len(last_trips)}[/bold green] direct TGV Max trips\n")
                print_trip_table(last_trips)
            else:
                console.print("❌ No direct TGV Max availability found", style=STYLE_ERROR)

            if show_alts:
                if last_alternatives:
                    console.print("\n💡 Also found multi-leg alternatives (sorted by price):\n", style=STYLE_INFO)
                    print_alternatives_table(last_alternatives)
                else:
                    console.print("\n❌ No multi-leg alternatives found", style=STYLE_ERROR)

            continue

        if cmd == "book":
            if not args:
                console.print("Usage: book N   (N = index from last search)", style=STYLE_ERROR)
                continue
            if not last_trips:
                console.print("❌ No previous search results to book from", style=STYLE_ERROR)
                continue
            try:
                idx = int(args[0])
            except ValueError:
                console.print("❌ N must be an integer", style=STYLE_ERROR)
                continue
            if idx < 1 or idx > len(last_trips):
                console.print(f"❌ N must be between 1 and {len(last_trips)}", style=STYLE_ERROR)
                continue

            selected = last_trips[idx - 1]
            console.print(f"\n🎟️  Booking trip #{idx}: {selected}", style=STYLE_INFO)

            if not config.SNCF_EMAIL or not config.SNCF_PASSWORD:
                console.print("❌ Credentials required", style=STYLE_ERROR)
                console.print("\nSet environment variables:")
                console.print("  export SNCF_EMAIL=your@email.com")
                console.print("  export SNCF_PASSWORD=yourpassword")
                continue

            if not Confirm.ask("Proceed with booking?"):
                console.print("Cancelled", style=STYLE_MUTED)
                continue

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Authenticating...", total=None)
                try:
                    api.login(
                        config.SNCF_EMAIL,
                        config.SNCF_PASSWORD,
                        session_file=str(ctx.obj.get('session_file')) if ctx.obj.get('session_file') else None,
                    )
                except Exception as e:
                    console.print(f"❌ Login failed: {e}", style=STYLE_ERROR)
                    continue

                progress.update(task, description="Booking...")
                result = api.book(selected)

            console.print()
            if result.is_success:
                console.print(Panel(
                    f"✅ [bold green]BOOKING CONFIRMED![/bold green]\n\n"
                    f"Train {selected.train_number}\n"
                    f"{selected.origin} → {selected.destination}\n"
                    f"{selected.departure_date} at {selected.departure_time.strftime('%H:%M')}\n\n"
                    f"Reference: [bold]{result.confirmation_number or 'Check your email'}[/bold]",
                    title="🎉 Success",
                    border_style="green",
                ))
            else:
                console.print(f"❌ Booking failed: {result.message}", style=STYLE_ERROR)
            continue

        console.print(f"❌ Unknown command: {cmd}. Type 'help' for available commands.", style=STYLE_ERROR)


@cli.command()
@click.argument('route')
@click.option('--date', '-d', 'dates', multiple=True, help='Dates to watch (YYYY-MM-DD, can specify multiple)')
@click.option('--days', '-n', default=7, help='Days ahead to watch (default: 7)')
@click.option('--time', '-t', 'time_filter', help='Time window (e.g., 17:00-21:00)')
@click.option('--interval', '-i', default=120, help='Check interval in seconds (default: 120)')
@click.option('--auto-book', is_flag=True, help='Automatically book when available')
@click.pass_context
def watch(ctx, route: str, dates: tuple, days: int, time_filter: Optional[str], 
          interval: int, auto_book: bool):
    """Watch for TGV Max availability and optionally auto-book.
    
    ROUTE: Origin-destination (e.g., paris-lyon)
    
    Examples:
    
        sncf-max watch paris-lyon --days 14
        
        sncf-max watch paris-lyon --date 2025-01-15 --auto-book
        
        sncf-max watch lyon-bordeaux --time 08:00-12:00 --interval 60
    """
    from scanner import ContinuousScanner
    
    config: SNCFConfig = ctx.obj['config']
    
    # Parse route
    parts = route.lower().split('-')
    if len(parts) != 2:
        console.print("❌ Invalid route format", style=STYLE_ERROR)
        return
    
    origin, destination = parts
    
    # Determine dates to watch
    watch_dates = []
    if dates:
        for d in dates:
            watch_dates.append(datetime.strptime(d, "%Y-%m-%d").date())
    else:
        today = date.today()
        for i in range(1, days + 1):
            watch_dates.append(today + timedelta(days=i))
    
    # Parse time filter
    time_min, time_max = None, None
    if time_filter:
        parts = time_filter.split('-')
        if len(parts) == 2:
            time_min = parts[0]
            time_max = parts[1]
    
    console.print(Panel(
        f"[bold]Route:[/bold] {get_station_name(origin)} → {get_station_name(destination)}\n"
        f"[bold]Dates:[/bold] {len(watch_dates)} dates ({watch_dates[0]} to {watch_dates[-1]})\n"
        f"[bold]Time:[/bold] {time_filter or 'Any'}\n"
        f"[bold]Interval:[/bold] {interval}s\n"
        f"[bold]Auto-book:[/bold] {'Yes ✅' if auto_book else 'No'}",
        title="👀 Starting Watch",
        border_style="cyan",
    ))
    
    if auto_book and (not config.SNCF_EMAIL or not config.SNCF_PASSWORD):
        console.print("⚠️  Auto-book requires credentials", style=STYLE_WARNING)
        console.print("   Set SNCF_EMAIL and SNCF_PASSWORD environment variables")
        auto_book = False
    
    # Create scanner
    scanner = ContinuousScanner(
        email=config.SNCF_EMAIL,
        password=config.SNCF_PASSWORD,
        config=config,
        auto_book=auto_book,
    )
    
    # Add targets
    for d in watch_dates:
        scanner.add_target(origin, destination, d, time_min, time_max)
    
    # Live status
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text
    
    status_text = Text()
    scan_info = {"cycle": 0, "target": 0, "total": 0, "found": 0, "last_scan": None}
    
    def make_status() -> Text:
        mode = scanner._get_mode().value
        mode_emoji = "🔥" if mode == "aggressive" else "🔍" if mode == "normal" else "😴"
        
        text = Text()
        text.append(f"{mode_emoji} ", style="bold")
        text.append(f"Cycle {scan_info['cycle']} ", style="cyan")
        
        if scan_info['total'] > 0:
            text.append(f"[{scan_info['target']}/{scan_info['total']}] ", style="dim")
        
        if scan_info['last_scan']:
            text.append(f"| Last: {scan_info['last_scan']} ", style="dim")
        
        text.append(f"| Found: {scan_info['found']} ", style="green" if scan_info['found'] > 0 else "dim")
        text.append(f"| Mode: {mode}", style="yellow" if mode == "aggressive" else "dim")
        
        return text
    
    # Callbacks
    def on_found(trip: Trip, target):
        scan_info['found'] += 1
        console.print(f"\n🚄 [bold green]FOUND:[/bold green] Train {trip.train_number}")
        console.print(f"   {trip.departure_date} {trip.departure_time.strftime('%H:%M')} - {trip.arrival_time.strftime('%H:%M')}")
    
    def on_booked(result, target):
        if result.is_success:
            console.print(f"\n✅ [bold green]BOOKED![/bold green] {result.confirmation_number}")
        else:
            console.print(f"\n❌ Booking failed: {result.message}", style=STYLE_ERROR)
    
    def on_cycle_start(cycle_num, total_targets):
        scan_info['cycle'] = cycle_num
        scan_info['total'] = total_targets
        scan_info['target'] = 0
    
    def on_target_scanned(target, current, total):
        scan_info['target'] = current
        scan_info['last_scan'] = f"{target.origin[:10]}→{target.destination[:10]} {target.trip_date.strftime('%d/%m')}"
    
    scanner.on_found(on_found)
    scanner.on_booked(on_booked)
    scanner.on_cycle_start(on_cycle_start)
    scanner.on_target_scanned(on_target_scanned)
    
    console.print("\n🔍 Scanning... Press Ctrl+C to stop\n")
    
    # Run with live status display
    import asyncio
    import threading
    
    stop_event = threading.Event()
    
    async def run_scanner():
        scanner.NORMAL_INTERVAL = interval
        scanner._running = True
        
        while scanner._running and not stop_event.is_set():
            try:
                await scanner.scan_all()
                
                # Wait for next cycle
                wait_time = scanner._get_interval()
                for _ in range(int(wait_time)):
                    if stop_event.is_set():
                        break
                    await asyncio.sleep(1)
                    
            except Exception as e:
                console.print(f"\n⚠️ Error: {e}", style=STYLE_WARNING)
    
    def run_in_thread():
        asyncio.run(run_scanner())
    
    try:
        with Live(make_status(), console=console, refresh_per_second=2) as live:
            thread = threading.Thread(target=run_in_thread, daemon=True)
            thread.start()
            
            while thread.is_alive():
                live.update(make_status())
                thread.join(timeout=0.5)
                
    except KeyboardInterrupt:
        stop_event.set()
        scanner._running = False
        console.print("\n\n👋 Watch stopped", style=STYLE_MUTED)


@cli.command()
@click.pass_context
def trips(ctx):
    """View your booked TGV Max trips."""
    config: SNCFConfig = ctx.obj['config']
    
    if not config.SNCF_EMAIL or not config.SNCF_PASSWORD:
        console.print("❌ Credentials required to view trips", style=STYLE_ERROR)
        console.print("\n💡 Set up credentials:")
        console.print("   1. Create a .env file: cp env.example .env")
        console.print("   2. Edit .env with your SNCF_EMAIL and SNCF_PASSWORD")
        console.print("   Or: export SNCF_EMAIL=... SNCF_PASSWORD=...")
        return
    
    try:
        from voyages import MesVoyagesClient, VoyagesStatus
        from auth import load_or_login, AuthenticationError
        
        console.print("\n📋 Fetching your trips...")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Loading...", total=None)
            
            from pathlib import Path

            try:
                session = load_or_login(
                    config.SNCF_EMAIL,
                    config.SNCF_PASSWORD,
                    config,
                    Path(ctx.obj['session_file']) if ctx.obj.get('session_file') else None,
                )
            except AuthenticationError as auth_err:
                console.print(f"\n❌ Authentication failed", style=STYLE_ERROR)
                console.print(f"   {auth_err}", style=STYLE_MUTED)
                console.print("\n💡 Debugging tips:")
                console.print("   • Run with SNCF_HEADLESS=false to see the browser")
                console.print("   • Check screenshots in ~/.sncf_max_screenshots/")
                console.print("   • The SNCF website may have changed - please report an issue")
                return
            
            import asyncio
            async def fetch():
                async with MesVoyagesClient(session, config) as client:
                    return await client.fetch_trips()
            
            status = asyncio.run(fetch())
        
        console.print()
        
        # Show status
        slots_color = "green" if status.slots_available > 2 else "yellow" if status.slots_available > 0 else "red"
        console.print(Panel(
            f"[bold]Upcoming trips:[/bold] {status.upcoming_count}/6\n"
            f"[bold]Slots available:[/bold] [{slots_color}]{status.slots_available}[/{slots_color}]",
            title="📊 TGV Max Status",
            border_style="blue",
        ))
        
        if status.trips:
            table = Table(
                title="Your Trips",
                box=box.ROUNDED,
                header_style="bold cyan",
            )
            
            table.add_column("Ref", style="bold")
            table.add_column("Train")
            table.add_column("Date")
            table.add_column("Time")
            table.add_column("Route")
            table.add_column("Status")
            
            for trip in status.trips:
                status_str = "🟢 Upcoming" if trip.is_upcoming else "⚫ Past"
                
                table.add_row(
                    trip.reference,
                    trip.train_number,
                    trip.departure_date.strftime("%d/%m"),
                    trip.departure_time.strftime("%H:%M"),
                    f"{trip.origin[:15]} → {trip.destination[:15]}",
                    status_str,
                )
            
            console.print(table)
        else:
            console.print("No trips found", style=STYLE_MUTED)
            
    except ImportError:
        console.print("❌ Playwright required. Install with: pip install playwright", style=STYLE_ERROR)
    except Exception as e:
        console.print(f"❌ Error: {e}", style=STYLE_ERROR)


@cli.command()
@click.argument('reference')
@click.pass_context
def cancel(ctx, reference: str):
    """Cancel a booked trip.
    
    REFERENCE: The 6-letter booking reference (e.g., ABCDEF)
    """
    config: SNCFConfig = ctx.obj['config']
    
    if not config.SNCF_EMAIL or not config.SNCF_PASSWORD:
        console.print("❌ Credentials required", style=STYLE_ERROR)
        return
    
    if not Confirm.ask(f"Cancel trip [bold]{reference}[/bold]?"):
        console.print("Cancelled", style=STYLE_MUTED)
        return
    
    try:
        from voyages import cancel_trip_sync
        from auth import load_or_login
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Cancelling...", total=None)

            from pathlib import Path

            session = load_or_login(
                config.SNCF_EMAIL,
                config.SNCF_PASSWORD,
                config,
                Path(ctx.obj['session_file']) if ctx.obj.get('session_file') else None,
            )
            success = cancel_trip_sync(session, reference, config)
        
        if success:
            console.print(f"✅ Trip {reference} cancelled", style=STYLE_SUCCESS)
        else:
            console.print(f"❌ Failed to cancel {reference}", style=STYLE_ERROR)
            
    except Exception as e:
        console.print(f"❌ Error: {e}", style=STYLE_ERROR)


@cli.group()
def schedule():
    """Manage recurring trip schedules."""
    pass


@schedule.command('add')
@click.argument('route')
@click.option('--name', '-n', required=True, help='Schedule name')
@click.option('--days', '-d', required=True, help='Weekdays (e.g., mon,fri or monday,friday)')
@click.option('--time', '-t', 'time_window', help='Time window (e.g., 17:00-21:00)')
@click.pass_context
def schedule_add(ctx, route: str, name: str, days: str, time_window: Optional[str]):
    """Add a recurring trip schedule.
    
    Example:
    
        sncf-max schedule add paris-lyon --name "Weekend home" --days fri --time 17:00-21:00
    """
    config: SNCFConfig = ctx.obj['config']
    
    # Parse route
    parts = route.lower().split('-')
    if len(parts) != 2:
        console.print("❌ Invalid route format", style=STYLE_ERROR)
        return
    
    origin, destination = parts
    
    # Parse weekdays
    day_map = {
        'mon': Weekday.MONDAY, 'monday': Weekday.MONDAY,
        'tue': Weekday.TUESDAY, 'tuesday': Weekday.TUESDAY,
        'wed': Weekday.WEDNESDAY, 'wednesday': Weekday.WEDNESDAY,
        'thu': Weekday.THURSDAY, 'thursday': Weekday.THURSDAY,
        'fri': Weekday.FRIDAY, 'friday': Weekday.FRIDAY,
        'sat': Weekday.SATURDAY, 'saturday': Weekday.SATURDAY,
        'sun': Weekday.SUNDAY, 'sunday': Weekday.SUNDAY,
    }
    
    weekdays = []
    for d in days.lower().split(','):
        d = d.strip()
        if d in day_map:
            weekdays.append(day_map[d])
        else:
            console.print(f"❌ Unknown day: {d}", style=STYLE_ERROR)
            return
    
    # Parse time window
    windows = None
    if time_window:
        parts = time_window.split('-')
        if len(parts) == 2:
            start = datetime.strptime(parts[0], "%H:%M").time()
            end = datetime.strptime(parts[1], "%H:%M").time()
            windows = [TimeWindow(start, end)]
    
    # Create scheduler and add
    scheduler = TGVMaxScheduler(
        email=config.SNCF_EMAIL,
        password=config.SNCF_PASSWORD,
        config=config,
    )
    
    schedule_id = scheduler.add_recurring(
        name=name,
        origin=origin,
        destination=destination,
        weekdays=weekdays,
        time_windows=windows,
    )
    
    console.print(f"✅ Added schedule: [bold]{name}[/bold] (ID: {schedule_id})", style=STYLE_SUCCESS)


@schedule.command('list')
@click.pass_context
def schedule_list(ctx):
    """List all scheduled trips."""
    config: SNCFConfig = ctx.obj['config']
    
    scheduler = TGVMaxScheduler(config=config)
    schedules = scheduler.list_schedules()
    
    if not schedules['recurring'] and not schedules['one_time']:
        console.print("No schedules configured", style=STYLE_MUTED)
        console.print("\nAdd one with: sncf-max schedule add paris-lyon --name 'My trip' --days fri")
        return
    
    if schedules['recurring']:
        table = Table(title="Recurring Schedules", box=box.ROUNDED)
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Route")
        table.add_column("Days")
        table.add_column("Time")
        table.add_column("Status")
        
        for s in schedules['recurring']:
            days_str = ', '.join([d.name[:3] for d in s.weekdays])
            time_str = ', '.join([f"{tw.start}-{tw.end}" for tw in s.time_windows])
            status = "✅ Active" if s.enabled else "⏸️ Paused"
            
            table.add_row(
                s.id,
                s.name,
                f"{s.origin[:12]}→{s.destination[:12]}",
                days_str,
                time_str[:20],
                status,
            )
        
        console.print(table)
    
    if schedules['one_time']:
        table = Table(title="One-Time Trips", box=box.ROUNDED)
        table.add_column("ID")
        table.add_column("Route")
        table.add_column("Date")
        table.add_column("Status")
        
        for s in schedules['one_time']:
            status = "✅ Booked" if s.booked else "⏳ Pending"
            table.add_row(
                s.id,
                f"{s.origin[:12]}→{s.destination[:12]}",
                str(s.trip_date),
                status,
            )
        
        console.print(table)


@schedule.command('run')
@click.option('--interval', '-i', default=300, help='Scan interval (seconds)')
@click.pass_context
def schedule_run(ctx, interval: int):
    """Run the scheduler to scan and auto-book."""
    config: SNCFConfig = ctx.obj['config']
    
    if not config.SNCF_EMAIL or not config.SNCF_PASSWORD:
        console.print("❌ Credentials required for auto-booking", style=STYLE_ERROR)
        return
    
    scheduler = TGVMaxScheduler(
        email=config.SNCF_EMAIL,
        password=config.SNCF_PASSWORD,
        config=config,
    )
    
    status = scheduler.status()
    
    if status['recurring_schedules'] == 0 and status['one_time_schedules'] == 0:
        console.print("No schedules to run", style=STYLE_MUTED)
        console.print("\nAdd schedules first with: sncf-max schedule add ...")
        return
    
    console.print(Panel(
        f"[bold]Recurring:[/bold] {status['recurring_schedules']} schedules\n"
        f"[bold]One-time:[/bold] {status['one_time_schedules']} trips\n"
        f"[bold]Pending dates:[/bold] {status['pending_dates']}\n"
        f"[bold]Interval:[/bold] {interval}s",
        title="🚀 Starting Scheduler",
        border_style="green",
    ))
    
    def on_booking(record):
        console.print(f"\n✅ [bold green]BOOKED:[/bold green] {record.trip}")
        console.print(f"   Confirmation: {record.confirmation}")
    
    scheduler.on_booking(on_booking)
    
    console.print("\n🔄 Running... Press Ctrl+C to stop\n")
    
    try:
        scheduler.run(interval=interval)
    except KeyboardInterrupt:
        console.print("\n👋 Scheduler stopped", style=STYLE_MUTED)


@cli.command()
def stations():
    """List known station aliases."""
    table = Table(title="Station Aliases", box=box.ROUNDED)
    table.add_column("Alias", style="bold cyan")
    table.add_column("Full Name")
    
    for alias, full_name in sorted(STATIONS.items()):
        table.add_row(alias, full_name)
    
    console.print(table)
    console.print("\n💡 You can use aliases in commands: [cyan]sncf-max search paris-lyon[/cyan]")


@cli.command()
@click.argument('route')
@click.option('--deadline', '-d', required=True, help='Deadline (YYYY-MM-DD HH:MM)')
@click.option('--previous-day', is_flag=True, help='Also search day before')
@click.option('--auto-book', is_flag=True, help='Book the best option')
@click.option('--continuous', is_flag=True, help='Keep scanning until booked')
@click.option('--interval', '-i', default=120, help='Scan interval (seconds)')
@click.option('--alternatives', '-a', is_flag=True, help='Show ALL alternatives including priced tickets')
@click.option('--max-price', type=int, help='Maximum price in euros for paid alternatives')
@click.pass_context
def deadline(ctx, route: str, deadline: str, previous_day: bool, auto_book: bool, 
             continuous: bool, interval: int, alternatives: bool, max_price: Optional[int]):
    """Search for trips arriving before a deadline.
    
    Find the best trip that arrives before a specific time, minimizing wait.
    Uses ARRIVAL time for deadline computation.
    
    Examples:
    
        sncf-max deadline paris-lyon --deadline "2025-01-15 13:00"
        
        sncf-max deadline paris-lyon --deadline "2025-01-15 09:00" --previous-day
        
        sncf-max deadline paris-lyon --deadline "2025-01-15 18:00" --alternatives
        
        sncf-max deadline paris-lyon --deadline "2025-01-15 18:00" -a --max-price 50
        
        sncf-max deadline paris-lyon --deadline "2025-01-15 18:00" --auto-book --continuous
    """
    from deadline import (
        DeadlineSearcher, DeadlineBooker, DeadlineConstraint, 
        DeadlineStrategy, get_all_options
    )
    
    config: SNCFConfig = ctx.obj['config']
    
    # Parse route
    parts = route.lower().split('-')
    if len(parts) != 2:
        console.print("❌ Invalid route format", style=STYLE_ERROR)
        return
    
    origin, destination = parts
    
    # Parse deadline
    try:
        deadline_dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M")
    except ValueError:
        console.print("❌ Invalid deadline format. Use: YYYY-MM-DD HH:MM", style=STYLE_ERROR)
        return
    
    strategy = DeadlineStrategy.PREVIOUS_DAY if previous_day else DeadlineStrategy.SAME_DAY_ONLY
    
    # Convert max_price from euros to cents
    max_price_cents = max_price * 100 if max_price else None
    
    constraint = DeadlineConstraint(
        departure_city=origin,
        arrival_city=destination,
        deadline=deadline_dt,
        strategy=strategy,
        include_priced=alternatives,  # Include paid alternatives if --alternatives
        max_price_cents=max_price_cents,
    )
    
    mode_str = "BROAD (all alternatives)" if alternatives else "MAX only"
    price_str = f"Max price: {max_price}€" if max_price else "No price limit"
    
    console.print(Panel(
        f"[bold]Route:[/bold] {get_station_name(origin)} → {get_station_name(destination)}\n"
        f"[bold]Deadline:[/bold] {deadline_dt.strftime('%A %d %B %Y at %H:%M')} (ARRIVAL time)\n"
        f"[bold]Strategy:[/bold] {strategy.value}\n"
        f"[bold]Mode:[/bold] {mode_str}\n"
        f"[bold]{price_str}[/bold]\n"
        f"[bold]Auto-book:[/bold] {'Yes ✅' if auto_book else 'No'}",
        title="⏰ Deadline Search",
        border_style="yellow",
    ))
    
    if auto_book and (not config.SNCF_EMAIL or not config.SNCF_PASSWORD):
        console.print("⚠️  Auto-book requires credentials", style=STYLE_WARNING)
        auto_book = False
    
    if continuous and auto_book:
        # Continuous scanning mode
        booker = DeadlineBooker(
            email=config.SNCF_EMAIL,
            password=config.SNCF_PASSWORD,
            config=config,
        )
        
        def on_better(new, old):
            wait_h = new.wait_time.total_seconds() / 3600
            console.print(f"\n📊 [bold cyan]Better option found:[/bold cyan]")
            console.print(f"   {new.trip.train_number}: arrives {new.arrival_datetime.strftime('%H:%M')}")
            console.print(f"   Wait time: {wait_h:.1f}h before deadline")
            console.print(f"   Type: {new.alternative_type.value} | Price: {new.price_display}")
        
        def on_booked(result, match):
            if result.is_success:
                console.print(f"\n✅ [bold green]BOOKED![/bold green] {result.confirmation_number}")
            else:
                console.print(f"\n❌ Booking failed: {result.message}", style=STYLE_ERROR)
        
        booker.on_better_found(on_better)
        booker.on_booked(on_booked)
        
        console.print("\n🔍 Scanning continuously... Press Ctrl+C to stop\n")
        
        try:
            result = booker.run(
                constraint, 
                auto_book=True, 
                scan_interval=interval,
                broad_search=alternatives,  # Use broad search if --alternatives
                prefer_free=True,  # Still prefer MAX over paid
            )
            if result and result.is_success:
                console.print("\n🎉 Trip booked successfully!")
        except KeyboardInterrupt:
            console.print("\n\n👋 Stopped", style=STYLE_MUTED)
    else:
        # Single search
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task_desc = "Searching (broad)..." if alternatives else "Searching..."
            progress.add_task(task_desc, total=None)
            
            searcher = DeadlineSearcher(config=config)
            if alternatives:
                matches = searcher.search_broad(constraint)
            else:
                matches = searcher.search(constraint)
        
        if not matches:
            console.print("\n❌ No trips found meeting the deadline", style=STYLE_ERROR)
            console.print("\n💡 Try --previous-day to search the day before")
            if not alternatives:
                console.print("💡 Try --alternatives to include priced tickets")
            return
        
        # Show price summary if alternatives mode
        if alternatives:
            summary = searcher.get_price_summary(matches)
            console.print(Panel(
                f"[bold]Total options:[/bold] {len(matches)}\n"
                f"[bold green]Free (MAX):[/bold green] {summary['count_free']}\n"
                f"[bold yellow]Paid:[/bold yellow] {summary['count_paid']}\n"
                + (f"[bold]Price range:[/bold] {summary['min_price']:.2f}€ - {summary['max_price']:.2f}€" 
                   if summary['min_price'] else ""),
                title="📊 Options Summary",
                border_style="cyan",
            ))
        
        console.print(f"\n✅ Found [bold green]{len(matches)}[/bold green] options\n")
        
        table = Table(
            title="Trips Before Deadline (by ARRIVAL time)",
            box=box.ROUNDED,
            header_style="bold cyan",
        )
        
        table.add_column("#", justify="center", width=3)
        table.add_column("Train", style="bold", min_width=8)
        table.add_column("Route", min_width=40)
        table.add_column("Date")
        table.add_column("Dep", justify="center")
        table.add_column("Arr", justify="center")
        table.add_column("Wait", justify="center")
        table.add_column("Price", justify="right", min_width=10)  # Always show price
        
        for i, match in enumerate(matches[:15], 1):
            wait_h = match.wait_time.total_seconds() / 3600
            
            # Build route string showing all stops
            if match.is_decomposed:
                train_str = f"[dim]{len(match.composite.legs)} legs[/dim]"
                # Show full route: Origin -> Via1 -> Via2 -> Destination
                route_parts = []
                for leg in match.composite.legs:
                    origin_short = str(leg.trip.origin)[:15]
                    max_marker = "[green]M[/green]" if leg.is_max else "[red]P[/red]"
                    route_parts.append(f"{origin_short}{max_marker}")
                # Add final destination
                final_dest = str(match.composite.legs[-1].trip.destination)[:15]
                route_parts.append(final_dest)
                route_str = " → ".join(route_parts)
            else:
                train_str = match.trip.train_number
                origin_short = str(match.trip.origin)[:15]
                dest_short = str(match.trip.destination)[:15]
                route_str = f"{origin_short} → {dest_short}"
            
            # Color wait time
            if wait_h < 1:
                wait_str = f"[green]{wait_h:.1f}h[/green]"
            elif wait_h < 3:
                wait_str = f"[yellow]{wait_h:.1f}h[/yellow]"
            else:
                wait_str = f"[red]{wait_h:.1f}h[/red]"
            
            # Format departure date - highlight if previous day
            dep_date = match.trip.departure_date
            arr_date = match.arrival_datetime.date()
            deadline_date = constraint.deadline_date
            
            # Color-code dates: green=deadline day, yellow=day before
            if dep_date == deadline_date:
                dep_date_str = f"[green]{dep_date.strftime('%d/%m')}[/green]"
            else:
                dep_date_str = f"[yellow]{dep_date.strftime('%d/%m')}[/yellow]"  # Previous day
            
            # Show arrival with date if different from departure
            if arr_date != dep_date:
                arr_str = f"{match.arrival_datetime.strftime('%H:%M')} [dim]({arr_date.strftime('%d/%m')})[/dim]"
            else:
                arr_str = match.arrival_datetime.strftime("%H:%M")
            
            # Format price clearly
            if match.is_free:
                price_str = "[green bold]0€ FREE[/green bold]"
            elif match.total_price_cents:
                price_str = f"[yellow bold]{match.total_price:.0f}€[/yellow bold]"
            else:
                price_str = "[dim]~50€[/dim]"  # Estimated
            
            row = [
                str(i),
                train_str,
                route_str,
                dep_date_str,
                match.trip.departure_time.strftime("%H:%M"),
                arr_str,
                wait_str,
                price_str,
            ]
            
            table.add_row(*row)
        
        console.print(table)
        
        if len(matches) > 15:
            console.print(f"\n  ... and {len(matches) - 15} more options", style=STYLE_MUTED)
        
        # Show legend for route markers
        console.print("\n[dim]Route: [green]M[/green]=MAX leg, [red]P[/red]=Paid leg | Date: [green]same day[/green], [yellow]previous day[/yellow][/dim]")
        
        # Helper to format match info with date
        def format_match_info(m):
            dep_date = m.trip.departure_date.strftime('%d/%m')
            arr_time = m.arrival_datetime.strftime('%H:%M')
            if m.is_decomposed:
                return f"{m.composite} on {dep_date}"
            else:
                return f"Train {m.trip.train_number} on {dep_date}, arr {arr_time}"
        
        # Show best recommendation
        best = matches[0]
        best_free = next((m for m in matches if m.is_free), None)
        best_paid = next((m for m in matches if not m.is_free), None)
        
        console.print(f"\n💡 [bold]Recommended:[/bold] {format_match_info(best)} [{best.price_display}]")
        
        # Show alternatives summary if in alternatives mode
        if alternatives and best_free and best_paid:
            console.print(f"\n[green]🆓 Best FREE:[/green] {format_match_info(best_free)}")
            console.print(f"[yellow]💳 Best PAID:[/yellow] {format_match_info(best_paid)} [{best_paid.price_display}]")
        
        if auto_book and not continuous:
            # Book the best option
            book_match = best_free if best_free else best
            book_msg = f"Book {book_match.trip.train_number} on {book_match.trip.departure_date.strftime('%d/%m')} ({book_match.price_display})?"
            
            if Confirm.ask(f"\n{book_msg}"):
                from api import TGVMaxAPI
                
                api = TGVMaxAPI(config=config)
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    progress.add_task("Booking...", total=None)
                    
                    try:
                        api.login(config.SNCF_EMAIL, config.SNCF_PASSWORD)
                        result = api.book(book_match.trip)
                        
                        if result.is_success:
                            console.print(f"\n✅ [bold green]BOOKED![/bold green] {result.confirmation_number}")
                        else:
                            console.print(f"\n❌ Booking failed: {result.message}", style=STYLE_ERROR)
                    except Exception as e:
                        console.print(f"\n❌ Error: {e}", style=STYLE_ERROR)
        
        # Show legend
        if alternatives:
            console.print("\n[dim]Legend: ✅ MAX = Free | 🔀 = Multi-leg | 💳 = Requires payment[/dim]")


@cli.command()
@click.pass_context
def status(ctx):
    """Show current status and configuration."""
    config: SNCFConfig = ctx.obj['config']
    
    console.print(Panel(
        f"[bold]Email:[/bold] {config.SNCF_EMAIL or '[red]Not set[/red]'}\n"
        f"[bold]Password:[/bold] {'✅ Set' if config.SNCF_PASSWORD else '[red]Not set[/red]'}\n"
        f"[bold]Proxy:[/bold] {config.PROXY or 'None'}\n"
        f"[bold]Headless:[/bold] {config.HEADLESS}\n"
        f"[bold]Debug:[/bold] {config.DEBUG}\n"
        f"[bold]Session file:[/bold] {config.SESSION_FILE}\n"
        f"[bold]Screenshots:[/bold] {config.SCREENSHOTS_DIR}",
        title="⚙️ Configuration",
        border_style="blue",
    ))
    
    # Check session
    if config.SESSION_FILE.exists():
        console.print("✅ Session file exists", style=STYLE_SUCCESS)
    else:
        console.print("ℹ️  No saved session", style=STYLE_MUTED)
    
    # Show debugging tips
    console.print("\n💡 [bold]Debugging Tips:[/bold]", style=STYLE_INFO)
    console.print("   • Set SNCF_HEADLESS=false to see the browser")
    console.print("   • Set SNCF_DEBUG=true for verbose logging + screenshots")
    console.print(f"   • Screenshots saved to: {config.SCREENSHOTS_DIR}")
    console.print("   • Create .env file with credentials (see env.example)")
    console.print("   • Run 'sncf-max debug' for interactive browser debugging")


# =============================================================================
# DEBUG / CALIBRATION COMMANDS
# =============================================================================

@cli.group()
def debug():
    """Browser debugging and calibration tools.
    
    These commands help diagnose and fix browser automation issues
    when the SNCF Connect website changes.
    """
    pass


@debug.command('interactive')
@click.pass_context
def debug_interactive(ctx):
    """Start an interactive browser debugging session.
    
    Opens a visible browser with developer tools and provides
    an interactive shell to test selectors and debug automation.
    
    Example:
    
        sncf-max debug interactive
    """
    config: SNCFConfig = ctx.obj['config']
    
    try:
        from browser_debug import BrowserDebugger
        import asyncio
        
        console.print(Panel(
            "[bold]Browser Debug Mode[/bold]\n\n"
            "A browser window will open with developer tools.\n"
            "Use the interactive shell to test selectors and debug flows.\n\n"
            "Commands in the shell:\n"
            "  [cyan]goto home[/cyan]     - Navigate to SNCF Connect\n"
            "  [cyan]test login_button[/cyan] - Test login button selectors\n"
            "  [cyan]test-all[/cyan]     - Test all known selectors\n"
            "  [cyan]login[/cyan]        - Step through login flow\n"
            "  [cyan]screenshot[/cyan]   - Capture current page\n"
            "  [cyan]quit[/cyan]         - Exit debugger",
            title="🔧 Debug Session",
            border_style="yellow",
        ))
        
        async def run():
            async with BrowserDebugger(config) as debugger:
                await debugger.interactive_shell()
        
        asyncio.run(run())
        
    except ImportError:
        console.print("❌ Playwright required. Install with: pip install playwright", style=STYLE_ERROR)
    except Exception as e:
        console.print(f"❌ Error: {e}", style=STYLE_ERROR)


@debug.command('login')
@click.option('--email', '-e', help='SNCF Connect email (uses env if not provided)')
@click.option('--password', '-p', help='SNCF Connect password (uses env if not provided)')
@click.option('--headless/--no-headless', default=False, help='Run in headless mode (default: visible)')
@click.pass_context
def debug_login(ctx, email: Optional[str], password: Optional[str], headless: bool):
    """Step through the login flow with a visible browser.
    
    Opens a visible browser so you can see what's happening and manually
    solve CAPTCHAs if needed (SNCF uses DataDome bot protection).
    
    Example:
    
        sncf-max debug login
        sncf-max debug login --headless  # Run without visible browser
    """
    config: SNCFConfig = ctx.obj['config']
    
    email = email or config.SNCF_EMAIL
    password = password or config.SNCF_PASSWORD
    
    if not email or not password:
        console.print("❌ Email and password required", style=STYLE_ERROR)
        console.print("\nProvide via --email/--password or set SNCF_EMAIL/SNCF_PASSWORD env vars")
        return
    
    try:
        from playwright.async_api import async_playwright
        import asyncio
        
        console.print(Panel(
            "[bold]Login Debug Mode[/bold]\n\n"
            f"Browser mode: [cyan]{'Headless' if headless else 'Visible'}[/cyan]\n\n"
            "[yellow]⚠️ IMPORTANT:[/yellow] SNCF Connect uses DataDome bot protection.\n"
            "The login popup often shows a CAPTCHA that must be solved manually.\n\n"
            "If you see a CAPTCHA:\n"
            "  1. Complete the challenge in the browser window\n"
            "  2. The login form will appear after verification\n"
            "  3. Credentials will be filled automatically\n\n"
            "Press Ctrl+C to cancel at any time.",
            title="🔐 Login Debug" + (" (Headless)" if headless else " (Visible Browser)"),
            border_style="yellow",
        ))
        
        async def run_login():
            debug_dir = config.SCREENSHOTS_DIR / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            
            async with async_playwright() as p:
                # Use Firefox for better bot evasion
                browser = await p.firefox.launch(
                    headless=headless,
                    slow_mo=100,
                )
                
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
                    locale='fr-FR',
                    timezone_id='Europe/Paris',
                )
                
                page = await context.new_page()
                
                # Step 1: Navigate
                console.print("\n📍 Step 1: Navigating to SNCF Connect...")
                await page.goto("https://www.sncf-connect.com/", timeout=30000)
                await asyncio.sleep(3)
                
                html = await page.content()
                if len(html) < 5000:
                    console.print("❌ Page blocked by bot protection", style=STYLE_ERROR)
                    await browser.close()
                    return
                
                console.print("   ✅ Page loaded")
                
                # Step 2: Cookies
                console.print("\n📍 Step 2: Handling cookies...")
                try:
                    cookie_btn = page.locator("button#didomi-notice-agree-button")
                    if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
                        await cookie_btn.click()
                        console.print("   ✅ Cookies accepted")
                        await asyncio.sleep(1)
                except:
                    pass
                
                # Step 3: Click login
                console.print("\n📍 Step 3: Clicking login button...")
                login_btn = page.locator("#vsc-login")
                if await login_btn.count() == 0:
                    console.print("   ❌ Login button not found", style=STYLE_ERROR)
                    await browser.close()
                    return
                
                await login_btn.click()
                console.print("   ✅ Login button clicked")
                await asyncio.sleep(3)
                
                # Get the popup
                if len(context.pages) > 1:
                    login_page = context.pages[-1]
                    console.print(f"   📌 Login popup opened")
                else:
                    login_page = page
                
                await login_page.screenshot(path=str(debug_dir / "login_popup.png"))
                
                # Step 4: Wait for CAPTCHA to be solved (if present)
                console.print("\n📍 Step 4: Checking for CAPTCHA...")
                
                captcha_present = False
                for frame in login_page.frames:
                    if 'captcha' in frame.url.lower():
                        captcha_present = True
                        break
                
                if captcha_present:
                    if headless:
                        console.print("   ❌ CAPTCHA detected but running in headless mode!", style=STYLE_ERROR)
                        console.print("   Run without --headless to solve CAPTCHA manually")
                        await browser.close()
                        return
                    
                    console.print("   ⚠️ [yellow]CAPTCHA DETECTED![/yellow]")
                    console.print("   Please solve the CAPTCHA in the browser window...")
                    console.print("   Waiting up to 2 minutes...\n")
                    
                    # Wait for CAPTCHA to be solved
                    for i in range(24):
                        await asyncio.sleep(5)
                        # Check if email input appeared
                        for sel in ['input[type="email"]', 'input[name="email"]', '#email']:
                            try:
                                loc = login_page.locator(sel)
                                if await loc.count() > 0 and await loc.first.is_visible():
                                    console.print("   ✅ CAPTCHA solved! Form is now visible.")
                                    captcha_present = False
                                    break
                            except:
                                pass
                        if not captcha_present:
                            break
                        console.print(f"   ⏳ Waiting... ({(i+1)*5}s)", end="\r")
                    
                    if captcha_present:
                        console.print("\n   ❌ Timeout waiting for CAPTCHA solution", style=STYLE_ERROR)
                        await browser.close()
                        return
                else:
                    console.print("   ✅ No CAPTCHA detected")
                
                # Step 5: Fill credentials
                console.print("\n📍 Step 5: Filling credentials...")
                
                # Find email input
                email_input = None
                for sel in ['input[type="email"]', 'input[name="email"]', '#email']:
                    try:
                        loc = login_page.locator(sel)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            email_input = loc.first
                            break
                    except:
                        pass
                
                if not email_input:
                    console.print("   ❌ Email input not found", style=STYLE_ERROR)
                    await login_page.screenshot(path=str(debug_dir / "no_email_input.png"))
                    await browser.close()
                    return
                
                await email_input.fill(email)
                console.print(f"   ✅ Email filled")
                
                # Find password input
                password_input = None
                for sel in ['input[type="password"]', '#password']:
                    try:
                        loc = login_page.locator(sel)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            password_input = loc.first
                            break
                    except:
                        pass
                
                if password_input:
                    await password_input.fill(password)
                    console.print(f"   ✅ Password filled")
                else:
                    console.print("   ⚠️ Password input not found yet")
                
                await login_page.screenshot(path=str(debug_dir / "credentials_filled.png"))
                
                # Step 6: Submit
                console.print("\n📍 Step 6: Submitting...")
                
                submit_btn = None
                for sel in ['button[type="submit"]', 'button:has-text("Se connecter")']:
                    try:
                        loc = login_page.locator(sel)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            submit_btn = loc.first
                            break
                    except:
                        pass
                
                if submit_btn:
                    await submit_btn.click()
                    console.print("   ✅ Submit clicked")
                else:
                    await login_page.keyboard.press("Enter")
                    console.print("   ✅ Enter pressed")
                
                await asyncio.sleep(5)
                
                # Step 7: Check result
                console.print("\n📍 Step 7: Checking result...")
                
                try:
                    if len(context.pages) > 1 and context.pages[-1].is_closed():
                        console.print("   📌 Popup closed (good sign!)")
                except:
                    pass
                
                await page.reload()
                await asyncio.sleep(3)
                await page.screenshot(path=str(debug_dir / "after_login.png"))
                
                # Check for success indicators
                for sel in ['#vsc-user-account', '[class*="account"]']:
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            text = await loc.first.text_content()
                            if text and 'compte' in text.lower():
                                console.print(f"\n✅ [bold green]LOGIN SUCCESSFUL![/bold green]")
                                console.print(f"   Account menu visible: {text[:50]}")
                                await browser.close()
                                return
                    except:
                        pass
                
                console.print("\n❓ Login status unclear - check screenshots")
                console.print(f"   📁 Screenshots saved to: {debug_dir}")
                
                await browser.close()
        
        asyncio.run(run_login())
        
    except ImportError:
        console.print("❌ Playwright required. Install with: pip install playwright", style=STYLE_ERROR)
    except KeyboardInterrupt:
        console.print("\n👋 Cancelled", style=STYLE_MUTED)
    except Exception as e:
        console.print(f"❌ Error: {e}", style=STYLE_ERROR)


@debug.command('selectors')
@click.option('--page', '-p', default='home', help='Page to test (home, login, search, account, trips)')
@click.pass_context
def debug_selectors(ctx, page: str):
    """Test all known selectors on a page.
    
    Navigates to a page and tests all known selectors, reporting
    which ones are working and which are broken.
    
    Example:
    
        sncf-max debug selectors
        sncf-max debug selectors --page login
    """
    config: SNCFConfig = ctx.obj['config']
    
    try:
        from browser_debug import BrowserDebugger
        import asyncio
        
        async def run():
            async with BrowserDebugger(config) as debugger:
                console.print(f"\n📍 Navigating to '{page}'...")
                state = await debugger.goto(page)
                console.print(f"   URL: {state.url}")
                
                console.print("\n🔍 Testing all known selectors...\n")
                
                results = await debugger.test_all_selectors()
                
                for group_name, tests in results.items():
                    working = [t for t in tests if t.found and t.visible]
                    found_only = [t for t in tests if t.found and not t.visible]
                    
                    if working:
                        status = f"[green]✅ {len(working)} working[/green]"
                    elif found_only:
                        status = f"[yellow]⚠️ {len(found_only)} found (not visible)[/yellow]"
                    else:
                        status = "[red]❌ None working[/red]"
                    
                    console.print(f"[bold]{group_name}[/bold]: {status}")
                    
                    for t in working[:2]:  # Show first 2 working
                        console.print(f"   ✅ [dim]{t.selector}[/dim]")
                        if t.text:
                            console.print(f"      text: {t.text[:50]}")
                
                console.print(f"\n📸 Screenshot saved: {state.screenshot_path}")
        
        asyncio.run(run())
        
    except ImportError:
        console.print("❌ Playwright required. Install with: pip install playwright", style=STYLE_ERROR)
    except Exception as e:
        console.print(f"❌ Error: {e}", style=STYLE_ERROR)


@debug.command('screenshot')
@click.option('--page', '-p', default='home', help='Page to screenshot')
@click.option('--name', '-n', default='debug', help='Screenshot name prefix')
@click.pass_context
def debug_screenshot(ctx, page: str, name: str):
    """Take a screenshot of a page.
    
    Useful for comparing page layouts over time to detect changes.
    
    Example:
    
        sncf-max debug screenshot --page login --name login_current
    """
    config: SNCFConfig = ctx.obj['config']
    
    try:
        from browser_debug import BrowserDebugger
        import asyncio
        
        async def run():
            async with BrowserDebugger(config) as debugger:
                console.print(f"📍 Navigating to '{page}'...")
                await debugger.goto(page)
                
                state = await debugger.capture_state(name)
                console.print(f"\n📸 Screenshot saved: {state.screenshot_path}")
                console.print(f"   URL: {state.url}")
                console.print(f"   Title: {state.title}")
        
        asyncio.run(run())
        
    except ImportError:
        console.print("❌ Playwright required. Install with: pip install playwright", style=STYLE_ERROR)
    except Exception as e:
        console.print(f"❌ Error: {e}", style=STYLE_ERROR)


def main():
    """Entry point."""
    print_banner()
    cli(obj={})


if __name__ == '__main__':
    main()

