"""Wallet configuration — env var loading and platform availability."""
import os

PLATFORM_CREDENTIALS = {
    "polymarket": ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS"],
    "kalshi": ["KALSHI_API_KEY", "KALSHI_RSA_PRIVATE_KEY"],
    "coinbase_spot": ["COINBASE_ADV_API_KEY", "COINBASE_ADV_API_SECRET"],
    "predictit": ["PREDICTIT_SESSION"],
}

def is_paper_mode() -> bool:
    return os.environ.get("PAPER_TRADING", "true").lower() != "false"

def get_paper_balance() -> float:
    try: return float(os.environ.get("PAPER_STARTING_BALANCE", "10000"))
    except ValueError: return 10000.0

def get_configured_platforms() -> dict[str, bool]:
    return {p: True for p, keys in PLATFORM_CREDENTIALS.items()
            if all(os.environ.get(k, "") for k in keys)}

def has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))
