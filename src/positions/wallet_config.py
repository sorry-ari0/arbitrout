"""Wallet configuration — env var loading, .env file support, and platform availability.

Keys are loaded from environment variables. For local development, place keys in
src/.env (gitignored). NEVER commit keys to git.

Required for live trading:
  PAPER_TRADING=false
  POLYMARKET_PRIVATE_KEY=<your polygon private key>
  POLYMARKET_FUNDER_ADDRESS=<your funding wallet address>

Optional:
  KALSHI_API_KEY, KALSHI_RSA_PRIVATE_KEY
  COINBASE_ADV_API_KEY, COINBASE_ADV_API_SECRET
  PREDICTIT_SESSION
  ANTHROPIC_API_KEY (for AI advisor)
  PAPER_STARTING_BALANCE (default: 10000)

Live policy (real money):
  By default, only news-based packages may open (strategy_type=news_driven or _news_driven).
  Set LIVE_TRADE_ALL_STRATEGIES=true to allow every strategy to open live (not recommended).
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger("positions.wallet_config")

PLATFORM_CREDENTIALS = {
    "polymarket": ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS"],
    "kalshi": ["KALSHI_API_KEY", "KALSHI_RSA_PRIVATE_KEY"],
    "coinbase_spot": ["COINBASE_ADV_API_KEY", "COINBASE_ADV_API_SECRET"],
    "predictit": ["PREDICTIT_SESSION"],
    "limitless": [],  # Public API, no credentials needed
    "opinion_labs": ["OPINION_LABS_API_KEY"],
    "robinhood": [],  # Public scraping, no credentials needed
    "crypto_spot": ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"],  # First in CCXT priority chain
    "kraken": [],  # CLI auth managed in WSL, not via env vars
}

# Sensitive keys that must never be logged or exposed via API
_SENSITIVE_KEYS = {
    "POLYMARKET_PRIVATE_KEY", "KALSHI_RSA_PRIVATE_KEY",
    "COINBASE_ADV_API_SECRET", "PREDICTIT_SESSION", "ANTHROPIC_API_KEY",
    "OPINION_LABS_API_KEY", "KRAKEN_API_SECRET",
    "BINANCE_API_SECRET", "BYBIT_API_SECRET", "OKX_API_SECRET",
    "KUCOIN_API_SECRET", "BITGET_API_SECRET",
}


def load_env_file(env_path: str | Path | None = None):
    """Load .env file into os.environ. Does NOT override existing env vars."""
    if env_path is None:
        env_path = Path(__file__).parent.parent / ".env"
    env_path = Path(env_path)
    if not env_path.exists():
        return
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    logger.info("Loaded env from %s", env_path)


def is_paper_mode() -> bool:
    return os.environ.get("PAPER_TRADING", "true").lower() != "false"


def live_news_only_execution_active() -> bool:
    """True when live money is on and the operator has not opted into all strategies."""
    if is_paper_mode():
        return False
    if os.environ.get("LIVE_TRADE_ALL_STRATEGIES", "").lower() in ("1", "true", "yes"):
        return False
    return True


def live_package_open_allowed(pkg: dict) -> tuple[bool, str]:
    """Return (allowed, error_message). Paper mode always allows; live respects news-only policy."""
    if not live_news_only_execution_active():
        return True, ""
    if pkg.get("strategy_type") == "news_driven":
        return True, ""
    if pkg.get("_news_driven"):
        return True, ""
    return False, (
        "Live trading is restricted to news-based packages only "
        "(strategy_type=news_driven or _news_driven). "
        f"Refusing open for strategy_type={pkg.get('strategy_type')!r}. "
        "Use PAPER_TRADING=true to simulate other strategies, or set "
        "LIVE_TRADE_ALL_STRATEGIES=true to allow all live opens (not recommended)."
    )

def get_paper_balance() -> float:
    try: return float(os.environ.get("PAPER_STARTING_BALANCE", "10000"))
    except ValueError: return 10000.0

def get_configured_platforms() -> dict[str, bool]:
    return {p: True for p, keys in PLATFORM_CREDENTIALS.items()
            if keys and all(os.environ.get(k, "") for k in keys)}

def has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))

def has_any_ai_provider() -> bool:
    """Check if any AI provider has an API key configured."""
    return any(os.environ.get(k, "") for k in [
        "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
    ])

def validate_live_config() -> dict:
    """Validate configuration for live trading. Returns issues dict."""
    issues = {}
    if is_paper_mode():
        issues["mode"] = "Paper mode is ON — set PAPER_TRADING=false for live"
    platforms = get_configured_platforms()
    if not platforms:
        issues["platforms"] = "No platforms configured — need at least one set of API keys"
    # Check .env file permissions (should not be world-readable)
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        try:
            import stat
            mode = env_path.stat().st_mode
            if mode & stat.S_IROTH:
                issues["env_perms"] = ".env file is world-readable — restrict permissions"
        except Exception:
            pass
    # Check .gitignore includes .env
    gitignore = Path(__file__).parent.parent.parent / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".env" not in content:
            issues["gitignore"] = ".env not in .gitignore — risk of committing secrets"
    return issues

def get_safe_config() -> dict:
    """Return config info safe for API exposure (no secret values)."""
    platforms = {}
    for p, keys in PLATFORM_CREDENTIALS.items():
        platforms[p] = {
            "configured": all(os.environ.get(k, "") for k in keys),
            "keys": {k: "***set***" if os.environ.get(k, "") else "missing" for k in keys},
        }
    return {
        "paper_mode": is_paper_mode(),
        "paper_balance": get_paper_balance() if is_paper_mode() else None,
        "platforms": platforms,
        "ai_enabled": has_any_ai_provider(),
        "live_issues": validate_live_config() if not is_paper_mode() else {},
        "live_news_only_opens": live_news_only_execution_active() if not is_paper_mode() else None,
    }
