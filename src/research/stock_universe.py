"""Stock universe — downloads full NASDAQ/NYSE/HKEX ticker lists."""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("research.universe")

DATA_DIR = Path(__file__).parent.parent / "data"
US_UNIVERSE_FILE = DATA_DIR / "us_stock_universe.json"
HKEX_UNIVERSE_FILE = DATA_DIR / "hkex_stock_universe.json"

# Refresh interval: 7 days
REFRESH_INTERVAL = 7 * 86400


# ============================================================
# SEC EDGAR — Full US Stock Universe
# ============================================================

def _fetch_sec_tickers() -> list[dict[str, Any]]:
    """Download full US stock list from SEC EDGAR company_tickers.json."""
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": "Arbitrout/1.0 research@arbitrout.app"}
    try:
        resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("SEC EDGAR fetch failed: %s", exc)
        return []

    tickers = []
    for entry in data.values():
        ticker = entry.get("ticker", "").strip().upper()
        name = entry.get("title", "").strip()
        cik = entry.get("cik_str", "")
        if not ticker or not name:
            continue
        # Skip tickers with special chars (warrants, units, etc.)
        if any(c in ticker for c in [".", "-", "/"]):
            continue
        if len(ticker) > 5:
            continue
        tickers.append({
            "ticker": ticker,
            "company_name": name,
            "cik": str(cik),
        })

    logger.info("SEC EDGAR: %d US tickers downloaded", len(tickers))
    return tickers


def _classify_exchange(ticker: str, name: str) -> str:
    """Heuristic exchange classification. SEC EDGAR doesn't provide exchange directly."""
    # Most US stocks are on NYSE or NASDAQ. Without a definitive source,
    # use common patterns.
    if len(ticker) <= 3:
        return "NYSE"  # Short tickers tend to be NYSE-listed
    return "NASDAQ"  # Longer tickers tend to be NASDAQ


def _classify_market_cap_tier(name: str) -> str:
    """Default tier — will be refined when we have actual market cap data."""
    # Without market cap data from SEC, default to "unknown"
    return "unknown"


def refresh_us_universe(force: bool = False) -> list[dict[str, Any]]:
    """Download and cache the full US stock universe.

    Returns list of dicts: ticker, company_name, exchange, market_cap_tier, cik
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if cache is fresh
    if not force and US_UNIVERSE_FILE.exists():
        try:
            stat = US_UNIVERSE_FILE.stat()
            if time.time() - stat.st_mtime < REFRESH_INTERVAL:
                return json.loads(US_UNIVERSE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    raw = _fetch_sec_tickers()
    if not raw:
        # Return existing cache if download failed
        if US_UNIVERSE_FILE.exists():
            return json.loads(US_UNIVERSE_FILE.read_text(encoding="utf-8"))
        return []

    universe = []
    for entry in raw:
        universe.append({
            "ticker": entry["ticker"],
            "company_name": entry["company_name"],
            "exchange": _classify_exchange(entry["ticker"], entry["company_name"]),
            "market_cap_tier": _classify_market_cap_tier(entry["company_name"]),
            "cik": entry.get("cik", ""),
        })

    # Atomic write
    tmp = str(US_UNIVERSE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(universe, f, ensure_ascii=False)
    os.replace(tmp, str(US_UNIVERSE_FILE))
    logger.info("US universe cached: %d tickers", len(universe))
    return universe


# ============================================================
# HKEX — Hong Kong Stock Exchange
# ============================================================

# Hang Seng Index + Hang Seng Composite major constituents
# Format: (code, company_name)
_HKEX_CONSTITUENTS: list[tuple[str, str]] = [
    # Hang Seng Index (~80 stocks)
    ("0001", "CK Hutchison"), ("0002", "CLP Holdings"), ("0003", "Hong Kong and China Gas"),
    ("0005", "HSBC Holdings"), ("0006", "Power Assets Holdings"), ("0011", "Hang Seng Bank"),
    ("0012", "Henderson Land"), ("0016", "Sun Hung Kai Properties"), ("0017", "New World Development"),
    ("0027", "Galaxy Entertainment"), ("0066", "MTR Corporation"), ("0101", "Hang Lung Properties"),
    ("0175", "Geely Automobile"), ("0241", "Alibaba Health"), ("0267", "CITIC"),
    ("0288", "WH Group"), ("0291", "China Resources Beer"), ("0316", "Orient Overseas"),
    ("0386", "China Petroleum & Chemical"), ("0388", "Hong Kong Exchanges and Clearing"),
    ("0669", "Techtronic Industries"), ("0688", "China Overseas Land"), ("0700", "Tencent Holdings"),
    ("0762", "China Unicom"), ("0823", "Link REIT"), ("0857", "PetroChina"),
    ("0868", "Xinyi Glass"), ("0883", "CNOOC"), ("0939", "China Construction Bank"),
    ("0941", "China Mobile"), ("0960", "Longfor Group"), ("0968", "Xinyi Solar"),
    ("0981", "Semiconductor Manufacturing International"), ("1038", "CK Infrastructure"),
    ("1044", "Hengan International"), ("1093", "CSPC Pharmaceutical"),
    ("1109", "China Resources Land"), ("1113", "CK Asset Holdings"),
    ("1177", "Sino Biopharmaceutical"), ("1211", "BYD Company"),
    ("1299", "AIA Group"), ("1378", "China Hongqiao Group"),
    ("1398", "Industrial and Commercial Bank of China"), ("1810", "Xiaomi Corporation"),
    ("1876", "Budweiser Brewing"), ("1928", "Sands China"),
    ("1997", "Wharf Real Estate Investment"), ("2007", "Country Garden"),
    ("2018", "AAC Technologies"), ("2020", "ANTA Sports"),
    ("2269", "WuXi Biologics"), ("2313", "Shenzhou International"),
    ("2318", "Ping An Insurance"), ("2319", "China Mengniu Dairy"),
    ("2331", "Li Ning"), ("2382", "Sunny Optical"),
    ("2388", "BOC Hong Kong"), ("2628", "China Life Insurance"),
    ("2688", "ENN Energy"), ("3328", "Bank of Communications"),
    ("3690", "Meituan"), ("3968", "China Merchants Bank"),
    ("3988", "Bank of China"), ("6098", "Country Garden Services"),
    ("6862", "Haidilao International"), ("9618", "JD.com"),
    ("9626", "Bilibili"), ("9633", "Nongfu Spring"),
    ("9888", "Baidu"), ("9961", "Trip.com Group"),
    ("9988", "Alibaba Group"), ("9999", "NetEase"),
    # Additional Hang Seng Composite constituents
    ("0019", "Swire Pacific"), ("0023", "Bank of East Asia"),
    ("0083", "Sino Land"), ("0144", "China Merchants Port"),
    ("0151", "Want Want China"), ("0168", "Tsingtao Brewery"),
    ("0270", "Guangdong Investment"), ("0285", "BYD Electronic"),
    ("0293", "Cathay Pacific"), ("0322", "Tingyi"),
    ("0354", "Chinasoft International"), ("0371", "Beijing Enterprises Water"),
    ("0384", "China Gas Holdings"), ("0489", "Dongfeng Motor"),
    ("0522", "ASM Pacific Technology"), ("0551", "Yue Yuen Industrial"),
    ("0604", "Shenzhen Investment"), ("0656", "Fosun International"),
    ("0694", "Beijing Capital International Airport"), ("0728", "China Telecom"),
    ("0753", "Air China"), ("0772", "China Literature"),
    ("0799", "IGG"), ("0836", "China Resources Power"),
    ("0853", "Microport Scientific"), ("0914", "Anhui Conch Cement"),
    ("0916", "China Longyuan Power"), ("0992", "Lenovo Group"),
    ("1024", "Kuaishou Technology"), ("1066", "Weigao Group"),
    ("1088", "China Shenhua Energy"), ("1171", "Yankuang Energy"),
    ("1179", "China Youzan"), ("1193", "China Resources Gas"),
    ("1208", "MMG Limited"), ("1268", "Meituan Select"),
    ("1339", "PICC Group"), ("1347", "Hua Hong Semiconductor"),
    ("1359", "China Cinda Asset Management"), ("1658", "Postal Savings Bank"),
    ("1772", "GanFeng Lithium"), ("1787", "Shandong Gold Mining"),
    ("1801", "Innovent Biologics"), ("1818", "Zhaojin Mining"),
    ("1833", "Ping An Healthcare"), ("1898", "China Coal Energy"),
    ("1919", "China COSCO Shipping"), ("1929", "Chow Tai Fook Jewellery"),
    ("2015", "Li Auto"), ("2196", "Shanghai Fosun Pharma"),
    ("2238", "GAC Group"), ("2328", "PICC Property and Casualty"),
    ("2333", "Great Wall Motor"), ("2338", "Weichai Power"),
    ("2359", "WuXi AppTec"), ("2378", "Prudential"),
    ("2600", "Aluminum Corporation of China"), ("2601", "China Pacific Insurance"),
    ("2638", "HK Electric Investments"), ("2669", "China Overseas Property"),
    ("2689", "Nine Dragons Paper"), ("3311", "China State Construction"),
    ("3323", "China National Building Material"), ("3331", "Vinda International"),
    ("3692", "Hansoh Pharmaceutical"), ("3759", "SinoPharm Group"),
    ("3799", "Dali Foods Group"), ("3888", "Kingsoft"),
    ("3993", "China Molybdenum"), ("6030", "CITIC Securities"),
    ("6060", "ZhongAn Online P&C Insurance"), ("6110", "TopSports International"),
    ("6186", "China Feihe"), ("6618", "JD Health International"),
    ("6690", "Haier Smart Home"), ("6818", "China Everbright Bank"),
    ("6969", "Smoore International"), ("9698", "GreenTown Service Group"),
    ("9901", "New Oriental Education"), ("9987", "Yum China"),
    ("9992", "Pop Mart International"),
]


def refresh_hkex_universe(force: bool = False) -> list[dict[str, Any]]:
    """Build HKEX universe from known constituents.

    Returns list of dicts: ticker, company_name, exchange, market_cap_tier
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not force and HKEX_UNIVERSE_FILE.exists():
        try:
            stat = HKEX_UNIVERSE_FILE.stat()
            if time.time() - stat.st_mtime < REFRESH_INTERVAL:
                return json.loads(HKEX_UNIVERSE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    universe = []
    for code, name in _HKEX_CONSTITUENTS:
        universe.append({
            "ticker": f"{code}.HK",
            "company_name": name,
            "exchange": "HKEX",
            "market_cap_tier": "unknown",
        })

    tmp = str(HKEX_UNIVERSE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(universe, f, ensure_ascii=False)
    os.replace(tmp, str(HKEX_UNIVERSE_FILE))
    logger.info("HKEX universe cached: %d tickers", len(universe))
    return universe


# ============================================================
# UNIFIED API
# ============================================================

def get_universe(
    exchange: str | None = None,
    cap_tier: str | None = None,
    include_hkex: bool = True,
) -> list[dict[str, Any]]:
    """Get filtered stock universe.

    Args:
        exchange: Filter by exchange (NASDAQ, NYSE, HKEX, or None for all)
        cap_tier: Filter by market cap tier (large, mid, small, micro, unknown, or None for all)
        include_hkex: Whether to include Hong Kong stocks

    Returns:
        List of stock dicts: ticker, company_name, exchange, market_cap_tier
    """
    stocks: list[dict[str, Any]] = []

    # US stocks
    if exchange is None or exchange.upper() in ("NASDAQ", "NYSE", "AMEX", "US"):
        us = refresh_us_universe()
        stocks.extend(us)

    # HKEX stocks
    if include_hkex and (exchange is None or exchange.upper() == "HKEX"):
        hkex = refresh_hkex_universe()
        stocks.extend(hkex)

    # Apply filters
    if exchange and exchange.upper() not in ("US",):
        stocks = [s for s in stocks if s["exchange"].upper() == exchange.upper()]

    if cap_tier:
        stocks = [s for s in stocks if s["market_cap_tier"] == cap_tier.lower()]

    return stocks


def get_ticker_count() -> dict[str, int]:
    """Return count of tickers by exchange."""
    us = refresh_us_universe()
    hkex = refresh_hkex_universe()
    nasdaq = sum(1 for s in us if s["exchange"] == "NASDAQ")
    nyse = sum(1 for s in us if s["exchange"] == "NYSE")
    return {
        "nasdaq": nasdaq,
        "nyse": nyse,
        "hkex": len(hkex),
        "total": len(us) + len(hkex),
    }
