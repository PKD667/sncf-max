"""Configuration for SNCF Max API."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List
import json


def load_dotenv(dotenv_paths: Optional[List[Path]] = None) -> None:
    """Load environment variables from .env file(s).
    
    Searches for .env files in order:
    1. Provided paths
    2. Current directory
    3. Home directory ~/.config/sncf-max/.env
    
    Format:
        SNCF_EMAIL=your@email.com
        SNCF_PASSWORD=yourpassword
        # Comments are ignored
    """
    if dotenv_paths is None:
        dotenv_paths = [
            Path.cwd() / ".env",
            Path.home() / ".config" / "sncf-max" / ".env",
            Path.home() / ".sncf-max.env",
        ]
    
    for dotenv_path in dotenv_paths:
        if dotenv_path.exists():
            _parse_dotenv(dotenv_path)
            return  # Stop after first found


def _parse_dotenv(path: Path) -> None:
    """Parse a .env file and set environment variables."""
    try:
        with open(path, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                
                # Handle export prefix
                if line.startswith("export "):
                    line = line[7:]
                
                # Parse key=value
                if "=" not in line:
                    continue
                
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                
                # Remove surrounding quotes
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                
                # Only set if not already in environment
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass  # Silently ignore errors


# Load .env on module import
load_dotenv()


@dataclass
class SNCFConfig:
    """Configuration for SNCF Max API client."""
    
    # Public API (data.sncf.com)
    PUBLIC_API_BASE_URL: str = "https://data.sncf.com/api/explore/v2.1/catalog/datasets/"
    TGVMAX_DATASET: str = "tgvmax"
    
    # SNCF Connect (booking platform)
    SNCF_CONNECT_BASE_URL: str = "https://www.sncf-connect.com"
    SNCF_CONNECT_LOGIN_URL: str = "https://www.sncf-connect.com/app/home/login"
    SNCF_CONNECT_SEARCH_URL: str = "https://www.sncf-connect.com/app/home/shop/search"
    SNCF_CONNECT_BOOKING_URL: str = "https://www.sncf-connect.com/app/home/shop/passengers"
    
    # Authentication
    SNCF_EMAIL: Optional[str] = None
    SNCF_PASSWORD: Optional[str] = None
    
    # Proxy configuration
    # Supports: http://host:port, http://user:pass@host:port, socks5://host:port
    PROXY: Optional[str] = None
    PROXY_BROWSER: Optional[str] = None  # Separate proxy for browser (if different)
    
    # Browser automation settings
    HEADLESS: bool = True
    BROWSER_TIMEOUT: int = 30000  # milliseconds
    SLOW_MO: int = 100  # milliseconds between actions
    
    # Session storage
    SESSION_FILE: Path = field(default_factory=lambda: Path.home() / ".sncf_max_session.json")
    CONFIG_DIR: Path = field(default_factory=lambda: Path.home() / ".config" / "sncf-max")
    
    # Rate limiting
    REQUEST_DELAY: float = 1.0  # seconds between requests
    MAX_RETRIES: int = 3
    
    # TGV Max limits
    MAX_BOOKINGS: int = 6  # Maximum concurrent TGV Max bookings
    
    # Debug
    DEBUG: bool = False
    SCREENSHOT_ON_ERROR: bool = True
    SCREENSHOTS_DIR: Path = field(default_factory=lambda: Path.home() / ".sncf_max_screenshots")
    
    @classmethod
    def from_env(cls) -> "SNCFConfig":
        """Create config from environment variables."""
        return cls(
            SNCF_EMAIL=os.getenv("SNCF_EMAIL"),
            SNCF_PASSWORD=os.getenv("SNCF_PASSWORD"),
            HEADLESS=os.getenv("SNCF_HEADLESS", "true").lower() == "true",
            DEBUG=os.getenv("SNCF_DEBUG", "false").lower() == "true",
            PROXY=os.getenv("SNCF_PROXY") or os.getenv("HTTP_PROXY"),
            PROXY_BROWSER=os.getenv("SNCF_PROXY_BROWSER"),
        )
    
    def get_proxy_dict(self) -> Optional[dict]:
        """Get proxy configuration for requests library."""
        if not self.PROXY:
            return None
        return {
            "http": self.PROXY,
            "https": self.PROXY,
        }
    
    def get_browser_proxy(self) -> Optional[dict]:
        """Get proxy configuration for Playwright."""
        proxy_url = self.PROXY_BROWSER or self.PROXY
        if not proxy_url:
            return None
        return {"server": proxy_url}
    
    def save_session(self, session_data: dict) -> None:
        """Save session data to file."""
        self.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(self.SESSION_FILE, "w") as f:
            json.dump(session_data, f)
    
    def load_session(self) -> Optional[dict]:
        """Load session data from file."""
        if not self.SESSION_FILE.exists():
            return None
        try:
            with open(self.SESSION_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    
    def clear_session(self) -> None:
        """Clear saved session."""
        if self.SESSION_FILE.exists():
            self.SESSION_FILE.unlink()


# Default configuration instance
default_config = SNCFConfig.from_env()


# Known station names for convenience
STATIONS = {
    # Paris
    "paris": "PARIS (intramuros)",
    "paris_lyon": "PARIS GARE DE LYON",
    "paris_montparnasse": "PARIS MONTPARNASSE 1 ET 2",
    "paris_nord": "PARIS NORD",
    "paris_est": "PARIS EST",
    
    # Major cities
    "lyon": "LYON (intramuros)",
    "marseille": "MARSEILLE ST CHARLES",
    "bordeaux": "BORDEAUX ST JEAN",
    "toulouse": "TOULOUSE MATABIAU",
    "lille": "LILLE FLANDRES",
    "nice": "NICE VILLE",
    "nantes": "NANTES",
    "strasbourg": "STRASBOURG",
    "montpellier": "MONTPELLIER ST ROCH",
    "rennes": "RENNES",
    
    # Other popular destinations
    "avignon": "AVIGNON TGV",
    "aix": "AIX EN PROVENCE TGV",
    "grenoble": "GRENOBLE",
    "dijon": "DIJON VILLE",
    "angers": "ANGERS ST LAUD",
    "tours": "ST PIERRE DES CORPS",
    "le_mans": "LE MANS",
    "reims": "CHAMPAGNE ARDENNE TGV",
    "metz": "METZ VILLE",
    "nancy": "NANCY",
    "marne": "MARNE LA VALLEE CHESSY",
    "disney": "MARNE LA VALLEE CHESSY",
    "cdg": "AEROPORT ROISSY CDG 2 TGV",
    "aeroport_cdg": "AEROPORT ROISSY CDG 2 TGV",
    "lyon_aeroport": "LYON ST EXUPERY TGV.",
    "massy": "MASSY TGV",
    "valence": "VALENCE TGV AUVERGNE RHONE ALPES",
    "creusot": "LE CREUSOT MONTCEAU MONTCHANIN",
    "macon": "MACON LOCHE",
}


def get_station_name(alias: str) -> str:
    """Get full station name from alias or return as-is if not found."""
    return STATIONS.get(alias.lower(), alias)

