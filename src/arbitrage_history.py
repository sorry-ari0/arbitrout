"""Arbitrage history — stores historical profit data for matched opportunities."""
import json
import logging
import time
from pathlib import Path
from typing import List, Dict

logger = logging.getLogger("arbitrage_history")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"
HISTORY_FILE = DATA_DIR / "opportunity_history.json"

# In-memory cache for faster access
_history_cache: Dict[str, List[Dict]] = {}
_cache_loaded = False
_MAX_HISTORY_ENTRIES = 1000  # Max entries per match_id
_MAX_HISTORY_TTL = 30 * 24 * 3600 # 30 days in seconds

def _load_history_from_file() -> Dict[str, List[Dict]]:
    """Loads all historical data from file into memory."""
    global _history_cache, _cache_loaded
    if HISTORY_FILE.exists():
        try:
            _history_cache = json.loads(HISTORY_FILE.read_text())
            _cache_loaded = True
            logger.info("Loaded arbitrage history from %s", HISTORY_FILE)
            return _history_cache
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load arbitrage history from %s: %s", HISTORY_FILE, e)
            _history_cache = {}
            _cache_loaded = True
    return {}

def _save_history_to_file(history_data: Dict[str, List[Dict]]):
    """Saves all historical data from memory to file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        HISTORY_FILE.write_text(json.dumps(history_data, indent=2))
        logger.debug("Saved arbitrage history to %s", HISTORY_FILE)
    except OSError as e:
        logger.error("Failed to save arbitrage history to %s: %s", HISTORY_FILE, e)

def save_opportunity_history(match_id: str, gross_profit_pct: float, net_profit_pct: float):
    """Saves the current profit percentage for a given match_id."""
    global _history_cache
    if not _cache_loaded:
        _load_history_from_file() # Ensure cache is loaded on first call

    now = time.time()
    
    # Prune old entries for this match_id
    current_match_history = [
        entry for entry in _history_cache.get(match_id, [])
        if now - entry["timestamp"] < _MAX_HISTORY_TTL
    ]

    new_entry = {
        "timestamp": now,
        "gross_profit_pct": round(gross_profit_pct, 2),
        "net_profit_pct": round(net_profit_pct, 2),
    }
    current_match_history.append(new_entry)

    # Keep only the latest N entries
    current_match_history = sorted(current_match_history, key=lambda x: x["timestamp"], reverse=True)[:_MAX_HISTORY_ENTRIES]
    _history_cache[match_id] = sorted(current_match_history, key=lambda x: x["timestamp"]) # Re-sort by oldest first for consistent display

    # Save to file (can be optimized to save less frequently if needed for performance)
    _save_history_to_file(_history_cache)

def get_opportunity_history(match_id: str) -> List[Dict]:
    """Retrieves the historical profit data for a given match_id."""
    if not _cache_loaded:
        _load_history_from_file()
    
    now = time.time()
    # Filter out entries older than TTL before returning
    return [
        entry for entry in _history_cache.get(match_id, [])
        if now - entry["timestamp"] < _MAX_HISTORY_TTL
    ]

