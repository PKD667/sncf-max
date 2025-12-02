# SNCF Max

Automated TGV Max trip discovery and booking system for SNCF.

Find and book TGV Max trips automatically. Features continuous scanning, calendar-based scheduling, and a CLI.

### Using Nix (Recommended)

```bash
# Enter development environment
nix develop

# Search for trips
python3 -m cli search paris-lyon --date +7

# Watch for availability with auto-booking
SNCF_EMAIL=your@email.com SNCF_PASSWORD=yourpass \
python3 -m cli watch paris-lyon --auto-book
```

### Manual Installation

```bash
pip install -e ".[all]"
playwright install chromium
```

## CLI Usage

```bash
# Search for trips
python3 -m cli search paris-lyon
python3 -m cli search paris-lyon --date 2025-01-15
python3 -m cli search paris-marseille --date +7 --time 08:00-12:00

# Find multi-leg alternatives (when direct isn't available)
python3 -m cli alternatives paris-lyon --date 2025-01-15
python3 -m cli alternatives paris-lyon --date 2025-01-15 --include-paid

# Search by arrival deadline (minimize wait time)
python3 -m cli deadline paris-lyon --deadline "2025-01-15 14:00"
python3 -m cli deadline paris-lyon --deadline "2025-01-15 09:00" --previous-day
python3 -m cli deadline paris-lyon --deadline "2025-01-15 18:00" --auto-book --continuous

# Watch for availability
python3 -m cli watch paris-lyon --days 14
python3 -m cli watch paris-lyon --date 2025-01-15 --auto-book

# Manage schedules
python3 -m cli schedule add paris-lyon --name "Friday home" --days fri --time 17:00-21:00
python3 -m cli schedule list
python3 -m cli schedule run

# View your trips
python3 -m cli trips
python3 -m cli cancel ABCDEF

# Other commands
python3 -m cli stations    # List station aliases
python3 -m cli status      # Show configuration
```

## Python API

```python
from sncf_max import TGVMaxAPI
from datetime import date

# Initialize
api = TGVMaxAPI()

# Search for trips
trips = api.search("paris", "lyon", date(2025, 1, 15))
for trip in trips:
    print(f"Train {trip.train_number}: {trip.departure_time}")

# Book a trip (requires authentication)
api.login("your@email.com", "password")
result = api.book(trips[0])
print(result)

# Auto-book the best available trip
result = api.auto_book(
    origin="paris",
    destination="lyon",
    trip_date=date(2025, 1, 15),
    preferred_time="18:00"
)
```

## Scheduling

Set up recurring trips that the system will automatically book:

```python
from sncf_max import TGVMaxScheduler, Weekday, TimeWindow

scheduler = TGVMaxScheduler(
    email="your@email.com",
    password="password"
)

# Every Friday evening Paris → Lyon
scheduler.add_recurring(
    name="Weekend home",
    origin="paris",
    destination="lyon",
    weekdays=[Weekday.FRIDAY],
    time_windows=[TimeWindow.evening()],
)

# Every Sunday evening Lyon → Paris
scheduler.add_recurring(
    name="Sunday return",
    origin="lyon",
    destination="paris",
    weekdays=[Weekday.SUNDAY],
    time_windows=[TimeWindow.evening()],
)

# Run the scheduler (scans and books automatically)
scheduler.run()
```

## Continuous Scanning

The scanner is optimized for catching TGV Max slots the moment they're released (around 6 AM French time):

```python
from sncf_max import ContinuousScanner
from datetime import date

scanner = ContinuousScanner(
    email="your@email.com",
    password="password",
    auto_book=True
)

# Add targets to watch
scanner.add_target("paris", "lyon", date(2025, 1, 15), time_min="17:00")

# Callbacks
scanner.on_found(lambda trip, target: print(f"Found: {trip}"))
scanner.on_booked(lambda result, target: print(f"Booked: {result}"))

# Run continuously
scanner.run()
```

## Configuration

### Using a .env file (recommended)

Create a `.env` file in your project directory or `~/.config/sncf-max/.env`:

```bash
# Copy from env.example
cp env.example .env

# Edit with your credentials
nano .env
```

```env
SNCF_EMAIL=your@email.com
SNCF_PASSWORD=yourpassword
SNCF_PROXY=http://proxy:port     # Optional
SNCF_DEBUG=false
SNCF_HEADLESS=true
```

### Or use environment variables

```bash
export SNCF_EMAIL=your@email.com
export SNCF_PASSWORD=yourpassword
export SNCF_PROXY=http://proxy:port     # Optional proxy
export SNCF_DEBUG=true                   # Enable debug mode
export SNCF_HEADLESS=false               # Show browser (for debugging)
```

## Station Aliases

Use short aliases instead of full station names:

| Alias | Station |
|-------|---------|
| paris | PARIS (intramuros) |
| lyon | LYON (intramuros) |
| marseille | MARSEILLE ST CHARLES |
| bordeaux | BORDEAUX ST JEAN |
| toulouse | TOULOUSE MATABIAU |
| lille | LILLE FLANDRES |
| nice | NICE VILLE |
| nantes | NANTES |
| strasbourg | STRASBOURG |
| montpellier | MONTPELLIER ST ROCH |

See all aliases with: `python3 -m cli stations`

## TGV Max Limits

- Maximum 6 trips can be booked at any time
- Trips can be booked up to 30 days in advance
- New slots are released daily around 6 AM (French time)

The system automatically tracks your booking count and will stop booking when you reach the limit.

## Trip Decomposition

When a direct TGV Max trip isn't available, the system can find alternatives via intermediate stations:

```python
from sncf_max import TripDecomposer
from datetime import date

decomposer = TripDecomposer()

# Find alternatives: Paris → Le Creusot → Lyon
alternatives = decomposer.find_alternatives(
    origin="paris",
    destination="lyon",
    trip_date=date(2025, 1, 15),
    include_paid=False  # Only fully MAX options
)

for alt in alternatives:
    print(f"{alt} - Duration: {alt.total_duration}")
```

## Deadline-Based Booking

Find the best trip arriving before a deadline:

```python
from sncf_max import DeadlineSearcher, DeadlineConstraint, DeadlineStrategy
from datetime import datetime

constraint = DeadlineConstraint(
    departure_city="paris",
    arrival_city="lyon",
    deadline=datetime(2025, 1, 15, 14, 0),  # Arrive by 2 PM
    strategy=DeadlineStrategy.PREVIOUS_DAY,  # Also check day before
)

searcher = DeadlineSearcher()
matches = searcher.search(constraint)

# Best option (minimizes wait time at destination)
best = matches[0]
print(f"Arrive at {best.arrival_datetime}, wait {best.wait_time} before deadline")
```

## Architecture

```
sncf_max/
├── client.py        # Public API client (trip discovery)
├── auth.py          # Authentication via browser automation
├── booking.py       # Booking via browser automation
├── voyages.py       # "Mes Voyages" integration (view/cancel trips)
├── scheduler.py     # Calendar-based scheduling
├── scanner.py       # Continuous availability scanner
├── monitor.py       # Availability monitoring with notifications
├── decomposition.py # Multi-leg trip alternatives
├── deadline.py      # Deadline-based search and booking
├── api.py           # High-level unified API
├── cli.py           # Command-line interface
├── models.py        # Data models
└── config.py        # Configuration (with .env support)
```

## Development

```bash
# Enter dev environment
nix develop

# Run tests
pytest

# Format code
black src/

# Type check
mypy src/
```

## License

MIT
