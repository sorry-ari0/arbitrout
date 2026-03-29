"""Arbitrage history module — stores historical opportunity and market data."""
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("arbitrage_history")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"
HISTORY_FILE = DATA_DIR / "opportunity_history.json"
MARKET_HISTORY_FILE = DATA_DIR / "market_history.json" # NEW: File for individual market history

# Max number of history entries to keep per opportunity/market
MAX_HISTORY_ENTRIES = 500


def _load_history(file_path: Path) -> dict[str, list[dict]]:
    """Load history from a JSON file."""
    if file_path.exists():
        try:
            return json.loads(file_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load history from %s: %s", file_path, e)
    return defaultdict(list)


def _save_history(file_path: Path, data: dict[str, list[dict]]):
    """Save history to a JSON file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        file_path.write_text(json.dumps(data, indent=2))
    except (TypeError, OSError) as e:
        logger.error("Failed to save history to %s: %s", file_path, e)


def save_opportunity_history(match_id: str, profit_pct: float, net_profit_pct: float):
    """Save the historical profit percentage for a given arbitrage opportunity."""
    history = _load_history(HISTORY_FILE)
    entry = {
        "timestamp": int(time.time()),
        "profit_pct": round(profit_pct, 2),
        "net_profit_pct": round(net_profit_pct, 2),
    }
    history.setdefault(match_id, []).append(entry)
    # Prune old entries
    if len(history[match_id]) > MAX_HISTORY_ENTRIES:
        history[match_id] = history[match_id][-MAX_HISTORY_ENTRIES:]
    _save_history(HISTORY_FILE, history)


def get_opportunity_history(match_id: str) -> list[dict]:
    """Get the historical profit data for a specific arbitrage opportunity."""
    history = _load_history(HISTORY_FILE)
    return history.get(match_id, [])


# NEW: Functions for individual market history
def save_market_history(platform: str, event_id: str, yes_price: float, no_price: float):
    """Save the historical prices for a specific individual market."""
    key = f"{platform}:{event_id}"
    history = _load_history(MARKET_HISTORY_FILE)
    entry = {
        "timestamp": int(time.time()),
        "yes_price": round(yes_price, 4),
        "no_price": round(no_price, 4),
    }
    history.setdefault(key, []).append(entry)
    # Prune old entries
    if len(history[key]) > MAX_HISTORY_ENTRIES:
        history[key] = history[key][-MAX_HISTORY_ENTRIES:]
    _save_history(MARKET_HISTORY_FILE, history)


def get_market_history(platform: str, event_id: str) -> list[dict]:
    """Get the historical price data for a specific individual market."""
    key = f"{platform}:{event_id}"
    history = _load_history(MARKET_HISTORY_FILE)
    return history.get(key, [])

