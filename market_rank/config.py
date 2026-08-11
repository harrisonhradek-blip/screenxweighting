from pathlib import Path

APP_DIR = Path.home() / ".market-rank"
SNAPSHOT_FILE = APP_DIR / "snapshot.json"
FUNDAMENTALS_FILE = APP_DIR / "fundamentals.json"
UNIVERSE_FILE = APP_DIR / "universe.json"

FUNDAMENTALS_TTL_DAYS = 7
DEFAULT_OPEN_REFRESH_TIME = "09:40"
