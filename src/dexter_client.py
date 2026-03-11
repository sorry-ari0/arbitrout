"""
Dexter Financial Data Client.

Python wrapper for the Financial Datasets API (api.financialdatasets.ai),
inspired by virattt/dexter. Provides institutional-grade financial data:
key ratios, financial statements, analyst estimates, insider trades,
stock prices, company news, and SEC filings.

Free tickers: AAPL, NVDA, MSFT (no key needed).
Full access: set FINANCIAL_DATASETS_API_KEY env var.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("dexter_client")

BASE_URL = "https://api.financialdatasets.ai"
API_KEY = os.environ.get("FINANCIAL_DATASETS_API_KEY", "")
TIMEOUT = 15.0


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    if API_KEY:
        h["x-api-key"] = API_KEY
    return h


def _get(endpoint: str, params: dict | None = None) -> dict | None:
    """Make GET request to Financial Datasets API."""
    url = f"{BASE_URL}{endpoint}"
    try:
        resp = httpx.get(url, params=params or {}, headers=_headers(), timeout=TIMEOUT, follow_redirects=True)
        if resp.status_code == 402:
            logger.debug("Financial Datasets %s requires paid plan", endpoint)
            return None
        if resp.status_code != 200:
            logger.debug("Financial Datasets %s returned %d", endpoint, resp.status_code)
            return None
        return resp.json()
    except Exception as e:
        logger.debug("Financial Datasets request failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Key Ratios — single snapshot with ALL major ratios
# ---------------------------------------------------------------------------

def get_key_ratios(ticker: str) -> dict[str, Any] | None:
    """Get key financial ratios snapshot for a ticker.

    Returns P/E, P/B, P/S, EV/EBITDA, PEG, margins, ROE, ROA, ROIC,
    current/quick ratios, debt/equity, EPS, FCF per share, growth rates.
    """
    data = _get("/financial-metrics/snapshot/", {"ticker": ticker.upper()})
    if data and "snapshot" in data:
        return data["snapshot"]
    return None


def get_historical_ratios(
    ticker: str,
    period: str = "ttm",
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Get historical financial metrics over time."""
    data = _get("/financial-metrics/", {
        "ticker": ticker.upper(),
        "period": period,
        "limit": limit,
    })
    if data and "financial_metrics" in data:
        return data["financial_metrics"]
    return []


# ---------------------------------------------------------------------------
# Financial Statements
# ---------------------------------------------------------------------------

def get_income_statements(
    ticker: str,
    period: str = "annual",
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Get income statements (revenue, net income, EPS, margins)."""
    data = _get("/financials/income-statements/", {
        "ticker": ticker.upper(),
        "period": period,
        "limit": limit,
    })
    if data and "income_statements" in data:
        return data["income_statements"]
    return []


def get_balance_sheets(
    ticker: str,
    period: str = "annual",
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Get balance sheets (assets, liabilities, equity, debt, cash)."""
    data = _get("/financials/balance-sheets/", {
        "ticker": ticker.upper(),
        "period": period,
        "limit": limit,
    })
    if data and "balance_sheets" in data:
        return data["balance_sheets"]
    return []


def get_cash_flow_statements(
    ticker: str,
    period: str = "annual",
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Get cash flow statements (operating, investing, financing, FCF)."""
    data = _get("/financials/cash-flow-statements/", {
        "ticker": ticker.upper(),
        "period": period,
        "limit": limit,
    })
    if data and "cash_flow_statements" in data:
        return data["cash_flow_statements"]
    return []


# ---------------------------------------------------------------------------
# Stock Prices
# ---------------------------------------------------------------------------

def get_price_snapshot(ticker: str) -> dict[str, Any] | None:
    """Get current stock price snapshot."""
    data = _get("/prices/snapshot/", {"ticker": ticker.upper()})
    if data and "snapshot" in data:
        return data["snapshot"]
    return None


def get_historical_prices(
    ticker: str,
    start_date: str,
    end_date: str,
    interval: str = "day",
) -> list[dict[str, Any]]:
    """Get historical OHLCV prices."""
    data = _get("/prices/", {
        "ticker": ticker.upper(),
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
    })
    if data and "prices" in data:
        return data["prices"]
    return []


def get_available_tickers() -> list[str]:
    """Get list of all available stock tickers."""
    data = _get("/prices/snapshot/tickers/")
    if data and "tickers" in data:
        return data["tickers"]
    return []


# ---------------------------------------------------------------------------
# Analyst Estimates
# ---------------------------------------------------------------------------

def get_analyst_estimates(
    ticker: str,
    period: str = "annual",
) -> list[dict[str, Any]]:
    """Get analyst estimates (EPS, revenue consensus)."""
    data = _get("/analyst-estimates/", {
        "ticker": ticker.upper(),
        "period": period,
    })
    if data and "analyst_estimates" in data:
        return data["analyst_estimates"]
    return []


# ---------------------------------------------------------------------------
# Insider Trades
# ---------------------------------------------------------------------------

def get_insider_trades(
    ticker: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get insider trades (Form 4 filings)."""
    data = _get("/insider-trades/", {
        "ticker": ticker.upper(),
        "limit": limit,
    })
    if data and "insider_trades" in data:
        return data["insider_trades"]
    return []


# ---------------------------------------------------------------------------
# Segmented Revenues
# ---------------------------------------------------------------------------

def get_segmented_revenues(
    ticker: str,
    period: str = "annual",
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Get revenue breakdown by business segments."""
    data = _get("/financials/segmented-revenues/", {
        "ticker": ticker.upper(),
        "period": period,
        "limit": limit,
    })
    if data and "segmented_revenues" in data:
        return data["segmented_revenues"]
    return []


# ---------------------------------------------------------------------------
# Company News
# ---------------------------------------------------------------------------

def get_company_news(
    ticker: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Get recent company news articles."""
    data = _get("/news/", {
        "ticker": ticker.upper(),
        "limit": limit,
    })
    if data and "news" in data:
        return data["news"]
    return []


# ---------------------------------------------------------------------------
# SEC Filings
# ---------------------------------------------------------------------------

def get_filings(
    ticker: str,
    filing_type: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get SEC filing metadata (10-K, 10-Q, 8-K)."""
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if filing_type:
        params["filing_type"] = ",".join(filing_type)
    data = _get("/filings/", params)
    if data and "filings" in data:
        return data["filings"]
    return []


# ---------------------------------------------------------------------------
# Conversion to our standard fundamentals format
# ---------------------------------------------------------------------------

def ratios_to_fundamentals(snapshot: dict) -> dict[str, Any]:
    """Convert a key-ratios snapshot into our standard fundamentals dict.

    This provides much richer data than yfinance or FMP free tier.
    Field names match the actual Financial Datasets API response.
    """
    return {
        "pe_ratio": snapshot.get("price_to_earnings_ratio", 0) or 0,
        "pb_ratio": snapshot.get("price_to_book_ratio", 0) or 0,
        "dividend_yield": round((snapshot.get("payout_ratio", 0) or 0) * 100, 2),
        "roe": round((snapshot.get("return_on_equity", 0) or 0) * 100, 2),
        "debt_to_equity": snapshot.get("debt_to_equity", 0) or 0,
        "profit_margin": round((snapshot.get("net_margin", 0) or 0) * 100, 2),
        "revenue_growth": round((snapshot.get("revenue_growth", 0) or 0) * 100, 2),
        "gross_margin": round((snapshot.get("gross_margin", 0) or 0) * 100, 2),
        "ebitda_margin": 0,  # Not directly in snapshot
        "operating_margin": round((snapshot.get("operating_margin", 0) or 0) * 100, 2),
        # Extra ratios from Financial Datasets
        "roic": round((snapshot.get("return_on_invested_capital", 0) or 0) * 100, 2),
        "roa": round((snapshot.get("return_on_assets", 0) or 0) * 100, 2),
        "current_ratio": round(snapshot.get("current_ratio", 0) or 0, 2),
        "quick_ratio": round(snapshot.get("quick_ratio", 0) or 0, 2),
        "eps": snapshot.get("earnings_per_share", 0) or 0,
        "eps_growth": round((snapshot.get("earnings_per_share_growth", 0) or 0) * 100, 2),
        "fcf_per_share": snapshot.get("free_cash_flow_per_share", 0) or 0,
        "fcf_growth": round((snapshot.get("free_cash_flow_growth", 0) or 0) * 100, 2),
        "book_value_per_share": snapshot.get("book_value_per_share", 0) or 0,
        "peg_ratio": round(snapshot.get("peg_ratio", 0) or 0, 2),
        "ev_to_ebitda": round(snapshot.get("enterprise_value_to_ebitda_ratio", 0) or 0, 2),
        "price_to_sales": round(snapshot.get("price_to_sales_ratio", 0) or 0, 2),
        "fcf_yield": round((snapshot.get("free_cash_flow_yield", 0) or 0) * 100, 2),
        "earnings_growth": round((snapshot.get("earnings_growth", 0) or 0) * 100, 2),
    }


def enrich_fundamentals(existing: dict, ticker: str) -> dict:
    """Enrich existing fundamentals dict with Financial Datasets data.

    Fills in missing or zero fields without overwriting good data.
    """
    snapshot = get_key_ratios(ticker)
    if not snapshot:
        return existing

    enriched = ratios_to_fundamentals(snapshot)

    for key, value in enriched.items():
        # Only fill if existing value is None or missing
        if key not in existing or existing[key] is None:
            existing[key] = value

    return existing
