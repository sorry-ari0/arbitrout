"""
Strategy Engine — natural-language strategy builder for S&P 500 custom ETFs.

Flow:
    1. User POSTs a strategy prompt + amount + period to /api/strategy
    2. parse_strategy() asks Ollama LLM to extract api_rules + research_rules
    3. get_sp500_list() fetches S&P 500 tickers from Wikipedia (cached weekly)
    4. get_sp500_fundamentals() batch-fetches fundamentals from yfinance (cached 24h)
    5. api_filter() filters cached fundamentals against api_rules
    6. research_filter() uses LLM + web scraping on survivors for research_rules
    7. Survivors are backtested via backtest_engine.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import yfinance as yf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    import fmp_client
except ImportError:
    fmp_client = None

try:
    import dexter_client
except ImportError:
    dexter_client = None

logger = logging.getLogger("strategy_engine")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama-agent:latest")
DATA_DIR = Path(__file__).parent / "data"
SP500_LIST_FILE = DATA_DIR / "sp500_list.json"
SP500_CACHE_FILE = DATA_DIR / "sp500_cache.json"
SP500_LIST_MAX_AGE = timedelta(days=7)
SP500_CACHE_MAX_AGE = timedelta(hours=24)
YFINANCE_BATCH_SIZE = 20
YFINANCE_BATCH_DELAY = 2.0
RESEARCH_CONCURRENCY = 2  # Single GPU — keep low to avoid LLM queue timeouts
SCRAPE_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# S&P 500 List (Wikipedia)
# ---------------------------------------------------------------------------

def get_sp500_list() -> list[dict[str, str]]:
    """Fetch S&P 500 tickers from Wikipedia, cached weekly in sp500_list.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if SP500_LIST_FILE.exists():
        cache = json.loads(SP500_LIST_FILE.read_text())
        updated = datetime.fromisoformat(cache["updated_at"])
        if datetime.now() - updated < SP500_LIST_MAX_AGE:
            return cache["stocks"]

    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    # Wikipedia blocks default urllib — use Scrapling to bypass
    from scrapling import Fetcher
    from io import StringIO
    fetcher = Fetcher()
    resp = fetcher.get(url)
    tables = pd.read_html(StringIO(resp.html_content))
    df = tables[0]

    stocks = []
    for _, row in df.iterrows():
        stocks.append({
            "symbol": str(row["Symbol"]).strip(),
            "name": str(row["Security"]).strip(),
            "sector": str(row.get("GICS Sector", "")).strip(),
            "sub_industry": str(row.get("GICS Sub-Industry", "")).strip(),
        })

    cache_data = {
        "updated_at": datetime.now().isoformat(),
        "stocks": stocks,
    }
    SP500_LIST_FILE.write_text(json.dumps(cache_data, indent=2))
    logger.info("Fetched %d S&P 500 tickers from Wikipedia", len(stocks))

    return stocks


# ---------------------------------------------------------------------------
# Fundamentals Cache (yfinance + Yahoo v8 API backup)
# ---------------------------------------------------------------------------

YAHOO_V8_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?modules=defaultKeyStatistics,financialData,summaryProfile,summaryDetail"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _fetch_via_yahoo_v8(symbol: str) -> dict[str, Any] | None:
    """Backup: fetch fundamentals directly from Yahoo Finance v8 API."""
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=defaultKeyStatistics,financialData,summaryProfile,summaryDetail,assetProfile"
        resp = httpx.get(url, headers=YAHOO_HEADERS, timeout=10.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        result = data.get("quoteSummary", {}).get("result", [])
        if not result:
            return None
        modules = result[0]

        fin = modules.get("financialData", {})
        stats = modules.get("defaultKeyStatistics", {})
        detail = modules.get("summaryDetail", {})
        profile = modules.get("assetProfile", {})

        def raw_val(d, key, default=0):
            v = d.get(key, {})
            return v.get("raw", default) if isinstance(v, dict) else (v or default)

        return {
            "sector": profile.get("sector", ""),
            "industry": profile.get("industry", ""),
            "market_cap": raw_val(detail, "marketCap", 0),
            "revenue_growth": round(raw_val(fin, "revenueGrowth", 0) * 100, 2),
            "pe_ratio": raw_val(detail, "trailingPE", 0),
            "pb_ratio": raw_val(detail, "priceToBook", 0),
            "dividend_yield": round(raw_val(detail, "dividendYield", 0) * 100, 2),
            "roe": round(raw_val(fin, "returnOnEquity", 0) * 100, 2),
            "debt_to_equity": raw_val(fin, "debtToEquity", 0),
            "profit_margin": round(raw_val(fin, "profitMargins", 0) * 100, 2),
            "beta": raw_val(detail, "beta", 1.0),
            "fcf": raw_val(fin, "freeCashflow", 0),
            "summary": (profile.get("longBusinessSummary", "") or "")[:200],
            "ebitda": raw_val(fin, "ebitda", 0),
            "total_debt": raw_val(fin, "totalDebt", 0),
            "operating_cashflow": raw_val(fin, "operatingCashflow", 0),
            "capex": abs(raw_val(fin, "capitalExpenditures", 0)),
            "net_income": raw_val(fin, "netIncomeToCommon", 0) or raw_val(stats, "netIncomeToCommon", 0),
            "depreciation": raw_val(fin, "depreciation", 0),
            "total_revenue": raw_val(fin, "totalRevenue", 0),
            "gross_profit": raw_val(fin, "grossProfits", 0),
        }
    except Exception as e:
        logger.debug("Yahoo v8 API failed for %s: %s", symbol, e)
        return None


def _extract_fundamentals(ticker_obj: yf.Ticker) -> dict[str, Any] | None:
    """Extract key fundamentals from a yfinance Ticker object."""
    try:
        info = ticker_obj.info
        if not info or "symbol" not in info:
            return None
        return {
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "market_cap": info.get("marketCap", 0),
            "revenue_growth": round((info.get("revenueGrowth", 0) or 0) * 100, 2),
            "pe_ratio": info.get("trailingPE", 0) or 0,
            "pb_ratio": info.get("priceToBook", 0) or 0,
            "dividend_yield": round((info.get("dividendYield", 0) or 0) * 100, 2),
            "roe": round((info.get("returnOnEquity", 0) or 0) * 100, 2),
            "debt_to_equity": info.get("debtToEquity", 0) or 0,
            "profit_margin": round((info.get("profitMargins", 0) or 0) * 100, 2),
            "beta": info.get("beta", 1.0) or 1.0,
            "fcf": info.get("freeCashflow", 0) or 0,
            "summary": (info.get("longBusinessSummary", "") or "")[:200],
            "ebitda": info.get("ebitda", 0) or 0,
            "total_debt": info.get("totalDebt", 0) or 0,
            "operating_cashflow": info.get("operatingCashflow", 0) or 0,
            "capex": abs(info.get("capitalExpenditures", 0) or 0),
            "net_income": info.get("netIncomeToCommon", 0) or 0,
            "depreciation": info.get("depreciation", 0) or 0,
            "total_revenue": info.get("totalRevenue", 0) or 0,
            "gross_profit": info.get("grossProfits", 0) or 0,
        }
    except Exception as e:
        logger.warning("Failed to extract fundamentals: %s", e)
        return None


def _compute_ratios(data: dict[str, Any]) -> dict[str, Any]:
    """Compute derived financial ratios from raw cached fields.

    Adds computed ratio fields to the data dict in-place and returns it.
    Ratios that can't be computed (division by zero, missing data) are set to None.
    """
    total_debt = data.get("total_debt", 0) or 0
    ebitda = data.get("ebitda", 0) or 0
    net_income = data.get("net_income", 0) or 0
    depreciation = data.get("depreciation", 0) or 0
    operating_cf = data.get("operating_cashflow", 0) or 0
    capex = data.get("capex", 0) or 0
    total_revenue = data.get("total_revenue", 0) or 0
    gross_profit = data.get("gross_profit", 0) or 0

    # Debt / EBITDA (lower is better, < 3x healthy)
    data["debt_to_ebitda"] = round(total_debt / ebitda, 2) if ebitda > 0 else None

    # FFO / Total Debt (higher is better, > 0.3 strong)
    ffo = net_income + depreciation
    data["ffo_to_debt"] = round(ffo / total_debt, 2) if total_debt > 0 else None

    # FOCF / Total Debt (higher is better, > 0.2 healthy)
    focf = operating_cf - capex
    data["focf_to_debt"] = round(focf / total_debt, 2) if total_debt > 0 else None

    # EBITDA Margin (%)
    data["ebitda_margin"] = round((ebitda / total_revenue) * 100, 2) if total_revenue > 0 else None

    # Gross Margin (%)
    data["gross_margin"] = round((gross_profit / total_revenue) * 100, 2) if total_revenue > 0 else None

    return data


def _fetch_fundamentals_fmp(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch fundamentals from FMP API for given symbols."""
    if fmp_client is None or not fmp_client._enabled():
        return {}
    profiles = fmp_client.batch_profile(symbols)
    if not profiles:
        return {}
    ratio_symbols = list(profiles.keys())[:50]
    ratios = fmp_client.batch_ratios(ratio_symbols)
    result = {}
    for sym, profile in profiles.items():
        ratio = ratios.get(sym)
        data = fmp_client.fmp_to_fundamentals(profile, ratio)
        result[sym] = data
    return result


LOOKUP_PROMPT = """Given this financial data about {symbol} ({company_name}):

{text}

What is the {field_name} for this company? Look for the most recent annual value.
Reply with ONLY a JSON object: {{"value": <number>, "unit": "dollars|percent|ratio", "source": "where you found it"}}
If you cannot find the value, reply: {{"value": null, "unit": null, "source": "not found"}}"""


def _lookup_missing_field(symbol: str, company_name: str, field_name: str) -> float | None:
    """Use Scrapling + AI to look up a missing financial field."""
    from scrapling import Fetcher
    fetcher = Fetcher()

    # Try financial data sites
    name_slug = company_name.lower().replace(" ", "-").replace(".", "").replace(",", "")
    urls = [
        f"https://www.macrotrends.net/stocks/charts/{symbol}/{name_slug}/financial-ratios",
        f"https://stockanalysis.com/stocks/{symbol.lower()}/financials/",
    ]

    page_text = None
    for url in urls:
        try:
            resp = fetcher.get(url)
            if resp.status == 200:
                text = resp.get_all_text()[:3000]
                if text and len(text.strip()) > 100:
                    page_text = text
                    break
        except Exception as e:
            logger.debug("Scrape failed for %s at %s: %s", symbol, url, e)

    if not page_text:
        return None

    # Ask LLM to extract the value
    prompt_text = LOOKUP_PROMPT.format(
        symbol=symbol,
        company_name=company_name,
        text=page_text,
        field_name=field_name,
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt_text}],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=30.0)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        result = json.loads(raw)

        value = result.get("value")
        if value is not None:
            logger.info("AI lookup found %s for %s: %s", field_name, symbol, value)
            return float(value)
    except Exception as e:
        logger.warning("AI lookup failed for %s %s: %s", symbol, field_name, e)

    return None


def _ensure_ratio_fields(symbol: str, data: dict[str, Any], sp500_list: list[dict]) -> None:
    """Ensure ratio fields are populated, using AI lookup if needed."""
    name_map = {s["symbol"]: s["name"] for s in sp500_list}
    company_name = name_map.get(symbol, symbol)

    fields_to_check = {
        "ebitda": "EBITDA (Earnings Before Interest Taxes Depreciation Amortization)",
        "total_debt": "total debt (long-term plus short-term debt)",
        "operating_cashflow": "operating cash flow (cash flow from operations)",
        "capex": "capital expenditures (CapEx)",
        "net_income": "net income",
        "depreciation": "depreciation and amortization expense",
        "total_revenue": "total revenue",
        "gross_profit": "gross profit (revenue minus cost of goods sold)",
    }

    updated = False
    for field, description in fields_to_check.items():
        if not data.get(field):
            value = _lookup_missing_field(symbol, company_name, description)
            if value is not None:
                data[field] = value
                updated = True

    if updated:
        _compute_ratios(data)


def get_sp500_fundamentals(symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Batch-fetch fundamentals from yfinance, cached 24h in sp500_cache.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if SP500_CACHE_FILE.exists():
        cache = json.loads(SP500_CACHE_FILE.read_text())
        updated = datetime.fromisoformat(cache["updated_at"])
        if datetime.now() - updated < SP500_CACHE_MAX_AGE:
            cached_stocks = cache["stocks"]
            # Compute ratios for cached data (handles caches from before ratios were added)
            for sym_data in cached_stocks.values():
                if "debt_to_ebitda" not in sym_data:
                    _compute_ratios(sym_data)
            if symbols is None:
                return cached_stocks
            return {s: cached_stocks[s] for s in symbols if s in cached_stocks}

    if symbols is None:
        sp500 = get_sp500_list()
        symbols = [s["symbol"] for s in sp500]

    all_data: dict[str, dict[str, Any]] = {}

    # Try FMP first (faster, no rate limits for batch profile)
    fmp_data = _fetch_fundamentals_fmp(symbols)
    if fmp_data:
        # Enrich FMP data with Dexter ratios
        if dexter_client:
            for sym, data in fmp_data.items():
                try:
                    fmp_data[sym] = dexter_client.enrich_fundamentals(data, sym)
                except Exception as e:
                    logger.debug("Dexter enrichment failed for %s: %s", sym, e)
        all_data.update(fmp_data)
        logger.info("FMP provided fundamentals for %d/%d symbols", len(fmp_data), len(symbols))
        symbols = [s for s in symbols if s not in fmp_data]
        if not symbols:
            cache_data = {
                "updated_at": datetime.now().isoformat(),
                "stocks": all_data,
            }
            SP500_CACHE_FILE.write_text(json.dumps(cache_data, indent=2))
            return all_data

    total_batches = (len(symbols) + YFINANCE_BATCH_SIZE - 1) // YFINANCE_BATCH_SIZE
    delay = YFINANCE_BATCH_DELAY

    for i in range(0, len(symbols), YFINANCE_BATCH_SIZE):
        batch = symbols[i:i + YFINANCE_BATCH_SIZE]
        batch_num = i // YFINANCE_BATCH_SIZE + 1
        logger.info("Fetching fundamentals batch %d/%d (%d tickers)",
                     batch_num, total_batches, len(batch))

        rate_limited = False
        for sym in batch:
            data = None
            # Try yfinance first
            for attempt in range(2):
                try:
                    ticker = yf.Ticker(sym)
                    data = _extract_fundamentals(ticker)
                    if data:
                        break
                except Exception as e:
                    err_str = str(e)
                    if "Rate" in err_str or "429" in err_str or "Too Many" in err_str:
                        rate_limited = True
                        if attempt == 0:
                            time.sleep(delay)
                    else:
                        logger.debug("yfinance failed for %s: %s", sym, e)
                        break

            # Fallback to Yahoo v8 API if yfinance failed
            if not data:
                data = _fetch_via_yahoo_v8(sym)
                if data:
                    logger.debug("Used Yahoo v8 backup for %s", sym)

            if data:
                # Enrich with Dexter institutional-grade ratios
                if dexter_client:
                    try:
                        data = dexter_client.enrich_fundamentals(data, sym)
                    except Exception as e:
                        logger.debug("Dexter enrichment failed for %s: %s", sym, e)
                all_data[sym] = data
                _compute_ratios(data)

        # Increase delay if rate limited
        if rate_limited:
            delay = min(delay * 1.5, 30.0)
        elif delay > YFINANCE_BATCH_DELAY:
            delay = max(delay * 0.8, YFINANCE_BATCH_DELAY)

        if i + YFINANCE_BATCH_SIZE < len(symbols):
            time.sleep(delay)

    cache_data = {
        "updated_at": datetime.now().isoformat(),
        "stocks": all_data,
    }
    SP500_CACHE_FILE.write_text(json.dumps(cache_data, indent=2))
    logger.info("Cached fundamentals for %d stocks", len(all_data))

    return all_data


# ---------------------------------------------------------------------------
# Strategy Templates
# ---------------------------------------------------------------------------

STRATEGY_TEMPLATES: dict[str, dict[str, Any]] = {
    "value": {
        "name": "Deep Value",
        "description": "Low P/E, high free cash flow yield stocks",
        "prompt": "S&P 500 stocks with P/E ratio below 15, profit margin above 10%, and low debt to equity below 100",
    },
    "momentum": {
        "name": "Momentum Leaders",
        "description": "Stocks with strong recent price performance",
        "prompt": "S&P 500 stocks with revenue growth above 15% and return on equity above 20%",
    },
    "quality": {
        "name": "Quality Compounders",
        "description": "High ROE, stable earnings, wide moat companies",
        "prompt": "S&P 500 stocks with ROE above 25%, profit margin above 15%, and debt to equity below 80",
    },
    "dividend": {
        "name": "Dividend Aristocrats",
        "description": "High-yield, stable dividend payers",
        "prompt": "S&P 500 stocks with dividend yield above 3% and profit margin above 10% and debt to equity below 150",
    },
    "low_vol": {
        "name": "Low Volatility",
        "description": "Defensive, low-beta stocks for downside protection",
        "prompt": "S&P 500 stocks with beta below 0.8 and profit margin above 10% and dividend yield above 1%",
    },
    "growth": {
        "name": "High Growth Tech",
        "description": "Technology sector with aggressive revenue growth",
        "prompt": "S&P 500 technology stocks with revenue growth above 20% and market cap above 50 billion",
    },
    "small_value": {
        "name": "Small-Cap Value",
        "description": "Smaller S&P 500 companies trading at a discount",
        "prompt": "S&P 500 stocks with market cap below 20 billion and P/E ratio below 18 and revenue growth above 5%",
    },
}


# ---------------------------------------------------------------------------
# Ratio Explanations (for UI tooltips)
# ---------------------------------------------------------------------------

RATIO_EXPLANATIONS: dict[str, dict[str, str]] = {
    "pe_ratio": {
        "name": "Price / Earnings",
        "formula": "Stock Price / Earnings Per Share",
        "explanation": "How much investors pay per dollar of earnings. Lower may indicate undervaluation. Compare within sectors — tech P/Es are naturally higher than utilities.",
        "good_range": "10-25 (varies by sector)",
    },
    "debt_to_equity": {
        "name": "Debt / Equity",
        "formula": "Total Debt / Shareholders Equity",
        "explanation": "How much the company is financed by debt vs equity. Higher means more leverage risk but potentially higher returns.",
        "good_range": "< 100",
    },
    "roe": {
        "name": "Return on Equity",
        "formula": "(Net Income / Shareholders Equity) x 100",
        "explanation": "How efficiently a company uses shareholder money to generate profit. Higher is better, but extremely high ROE with high debt can be misleading.",
        "good_range": "> 15%",
    },
    "profit_margin": {
        "name": "Net Profit Margin",
        "formula": "(Net Income / Revenue) x 100",
        "explanation": "Percentage of revenue that becomes actual profit after all expenses. The bottom line.",
        "good_range": "> 10%",
    },
    "dividend_yield": {
        "name": "Dividend Yield",
        "formula": "(Annual Dividends / Stock Price) x 100",
        "explanation": "Annual return from dividends alone. Higher yield means more income but extremely high yields can signal a stock price crash.",
        "good_range": "2-5%",
    },
    "beta": {
        "name": "Beta",
        "formula": "Covariance(stock, market) / Variance(market)",
        "explanation": "How much a stock moves relative to the overall market. Beta 1.0 = moves with market. Below 1.0 = defensive. Above 1.0 = aggressive.",
        "good_range": "0.5-1.5",
    },
    "debt_to_ebitda": {
        "name": "Debt / EBITDA",
        "formula": "Total Debt / EBITDA",
        "explanation": "How many years it would take to pay off all debt using earnings before interest, taxes, depreciation and amortization. Lower is better.",
        "good_range": "< 3.0x",
    },
    "ffo_to_debt": {
        "name": "FFO / Total Debt",
        "formula": "(Net Income + Depreciation & Amortization) / Total Debt",
        "explanation": "Funds From Operations relative to total debt. Measures ability to cover debt from cash-generating operations. Used by credit rating agencies like S&P and Moody's.",
        "good_range": "> 0.30",
    },
    "focf_to_debt": {
        "name": "Free Operating Cash Flow / Total Debt",
        "formula": "(Operating Cash Flow - Capital Expenditures) / Total Debt",
        "explanation": "Cash left after maintaining and growing the business, relative to debt. Shows if a company generates enough free cash to service its debt.",
        "good_range": "> 0.20",
    },
    "operating_cashflow": {
        "name": "Cash Flow from Operations",
        "formula": "Net Income + Depreciation + Non-Cash Adjustments + Working Capital Changes",
        "explanation": "Actual cash generated by core business operations. Unlike net income, this strips out accounting tricks. Companies can report profits but still run out of cash.",
        "good_range": "Positive and growing",
    },
    "ebitda_margin": {
        "name": "EBITDA Margin",
        "formula": "(EBITDA / Total Revenue) x 100",
        "explanation": "Operational profitability before financing decisions and accounting choices. Useful for comparing companies across different tax jurisdictions and capital structures.",
        "good_range": "> 20%",
    },
    "gross_margin": {
        "name": "Gross Margin",
        "formula": "((Revenue - COGS) / Revenue) x 100",
        "explanation": "Percentage of revenue remaining after subtracting direct production costs (Cost of Goods Sold). Higher gross margins mean better pricing power or lower production costs.",
        "good_range": "> 40%",
    },
    "revenue_growth": {
        "name": "Revenue Growth",
        "formula": "(Current Revenue - Previous Revenue) / Previous Revenue x 100",
        "explanation": "How fast the company is growing its top line. Consistent growth signals a company gaining market share.",
        "good_range": "> 10%",
    },
    "market_cap": {
        "name": "Market Capitalization",
        "formula": "Share Price x Total Shares Outstanding",
        "explanation": "Total market value of the company. Small cap under $2B offers higher growth potential. Large cap over $10B offers stability.",
        "good_range": "Depends on strategy",
    },
    "fcf": {
        "name": "Free Cash Flow",
        "formula": "Operating Cash Flow - Capital Expenditures",
        "explanation": "Cash a company generates after maintaining its assets. Positive FCF means the company can fund growth, pay dividends, or reduce debt without borrowing.",
        "good_range": "Positive and growing",
    },
    "current_ratio": {
        "name": "Current Ratio",
        "formula": "Current Assets / Current Liabilities",
        "explanation": "Can the company pay its bills due within the next year? Above 1.0 means yes. Below 1.0 means potential liquidity trouble.",
        "good_range": "1.5 - 3.0",
    },
    "peg_ratio": {
        "name": "PEG Ratio",
        "formula": "P/E Ratio / Earnings Growth Rate",
        "explanation": "P/E ratio adjusted for growth. A PEG of 1.0 means the stock is fairly valued for its growth. Under 1.0 is potentially undervalued.",
        "good_range": "< 1.5",
    },
    "eps_growth": {
        "name": "EPS Growth",
        "formula": "(Current EPS - Previous EPS) / Previous EPS x 100",
        "explanation": "Growth in earnings per share. Rising EPS means the company is becoming more profitable on a per-share basis.",
        "good_range": "> 10%",
    },
    "fcf_yield": {
        "name": "FCF Yield",
        "formula": "(Free Cash Flow / Market Cap) x 100",
        "explanation": "Free cash flow relative to the company price. Like dividend yield but for all free cash. Higher means more cash generated per dollar invested.",
        "good_range": "> 5%",
    },
    "ev_to_ebitda": {
        "name": "EV/EBITDA",
        "formula": "Enterprise Value / EBITDA",
        "explanation": "Enterprise value relative to operating earnings. Lower means cheaper. More reliable than P/E because it accounts for debt and is harder to manipulate.",
        "good_range": "< 15",
    },
    "wacc": {
        "name": "WACC",
        "formula": "Weighted Average Cost of Capital",
        "explanation": "The blended cost of all capital (debt + equity). Used as the discount rate in DCF models. Lower WACC means cheaper financing.",
        "good_range": "6-12%",
    },
    "dcf": {
        "name": "DCF Fair Value",
        "formula": "Sum of Discounted Future Free Cash Flows + Terminal Value",
        "explanation": "Estimates what a company is truly worth based on projected future cash flows discounted back to today. If DCF value is above current price, the stock may be undervalued.",
        "good_range": "Above current price = undervalued",
    },
}


# ---------------------------------------------------------------------------
# LLM Intent Parser
# ---------------------------------------------------------------------------

PARSE_SYSTEM_PROMPT = """You are a stock strategy parser. Given a user's investment strategy,
output ONLY a JSON object with these fields:

api_rules (filterable from financial data):
  min_market_cap / max_market_cap — billions
  min_revenue_growth / max_revenue_growth — percentage
  sectors — list of GICS sectors: Technology, Healthcare, Financials,
            Consumer Discretionary, Consumer Staples, Energy, Industrials,
            Materials, Real Estate, Utilities, Communication Services
  industries — list of keywords to match against GICS sub-industry
  min_pe_ratio / max_pe_ratio
  min_dividend_yield / max_dividend_yield
  max_debt_to_equity
  min_roe — minimum return on equity percentage
  min_profit_margin — minimum profit margin percentage
  max_beta / min_beta
  max_debt_to_ebitda — maximum Debt/EBITDA ratio (years to pay off debt)
  min_ffo_to_debt / max_ffo_to_debt — FFO/Total Debt ratio (funds from operations / debt)
  min_focf_to_debt / max_focf_to_debt — Free Operating Cash Flow / Total Debt
  min_operating_cashflow — minimum operating cash flow in dollars
  min_ebitda — minimum EBITDA in dollars
  min_ebitda_margin / max_ebitda_margin — EBITDA margin percentage
  min_gross_margin / max_gross_margin — gross margin percentage (revenue minus COGS / revenue)

research_rules (requires web research, list of strings):
  ONLY include criteria the user EXPLICITLY mentioned that cannot be filtered from financial data.
  Examples: "CEO does not have an MBA", "company manufactures physical products",
  "founded before 1950", "headquarters in the Midwest"
  DO NOT invent or add criteria the user did not ask for.
  If the user only mentions financial metrics, research_rules should be an empty list [].

strategy_name — short descriptive name (3-5 words)

Omit fields not mentioned. Always respond with valid JSON only."""


async def parse_strategy(prompt: str) -> dict[str, Any]:
    """Ask Ollama LLM to split a strategy prompt into api_rules + research_rules."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(OLLAMA_URL, json=payload)
        resp.raise_for_status()

    raw = resp.json()["message"]["content"].strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    # Fix trailing commas (common with local models)
    raw = re.sub(r',\s*([}\]])', r'\1', raw)

    parsed = json.loads(raw)

    # Unwrap single-key wrapper if LLM wrapped the result
    if "api_rules" not in parsed and "research_rules" not in parsed:
        if len(parsed) == 1:
            inner = next(iter(parsed.values()))
            if isinstance(inner, dict):
                parsed = inner

    return {
        "api_rules": parsed.get("api_rules", {}),
        "research_rules": parsed.get("research_rules", []),
        "strategy_name": parsed.get("strategy_name", "Custom Strategy"),
    }


# ---------------------------------------------------------------------------
# Pass 1: API Filter (cached fundamentals)
# ---------------------------------------------------------------------------

def api_filter(universe: dict[str, dict[str, Any]], rules: dict[str, Any]) -> list[str]:
    """Filter cached fundamentals against structured api_rules."""
    matches = []

    sectors = [s.lower() for s in rules.get("sectors", [])]
    industries = [k.lower() for k in rules.get("industries", [])]

    for symbol, data in universe.items():
        # Sector filter
        if sectors and data.get("sector", "").lower() not in sectors:
            continue

        # Industry keyword filter (substring match)
        if industries:
            stock_industry = data.get("industry", "").lower()
            if not any(kw in stock_industry for kw in industries):
                continue

        # Market cap (rules in billions, data in raw)
        min_cap = rules.get("min_market_cap")
        if min_cap is not None and data.get("market_cap", 0) < min_cap * 1e9:
            continue
        max_cap = rules.get("max_market_cap")
        if max_cap is not None and data.get("market_cap", 0) > max_cap * 1e9:
            continue

        # Revenue growth (percentage)
        min_rg = rules.get("min_revenue_growth")
        if min_rg is not None and data.get("revenue_growth", 0) < min_rg:
            continue
        max_rg = rules.get("max_revenue_growth")
        if max_rg is not None and data.get("revenue_growth", 0) > max_rg:
            continue

        # P/E ratio
        min_pe = rules.get("min_pe_ratio")
        if min_pe is not None and data.get("pe_ratio", 0) < min_pe:
            continue
        max_pe = rules.get("max_pe_ratio")
        if max_pe is not None and data.get("pe_ratio", 0) > max_pe:
            continue

        # Dividend yield
        min_dy = rules.get("min_dividend_yield")
        if min_dy is not None and data.get("dividend_yield", 0) < min_dy:
            continue
        max_dy = rules.get("max_dividend_yield")
        if max_dy is not None and data.get("dividend_yield", 0) > max_dy:
            continue

        # Debt to equity (max only)
        max_de = rules.get("max_debt_to_equity")
        if max_de is not None and data.get("debt_to_equity", 0) > max_de:
            continue

        # ROE (min only)
        min_roe = rules.get("min_roe")
        if min_roe is not None and data.get("roe", 0) < min_roe:
            continue

        # Profit margin (min only)
        min_pm = rules.get("min_profit_margin")
        if min_pm is not None and data.get("profit_margin", 0) < min_pm:
            continue

        # Beta range
        min_beta = rules.get("min_beta")
        if min_beta is not None and data.get("beta", 1.0) < min_beta:
            continue
        max_beta = rules.get("max_beta")
        if max_beta is not None and data.get("beta", 1.0) > max_beta:
            continue

        # Debt / EBITDA (max only — lower is better)
        max_dte = rules.get("max_debt_to_ebitda")
        if max_dte is not None:
            val = data.get("debt_to_ebitda")
            if val is None or val > max_dte:
                continue

        # FFO / Total Debt
        min_ffo = rules.get("min_ffo_to_debt")
        if min_ffo is not None:
            val = data.get("ffo_to_debt")
            if val is None or val < min_ffo:
                continue
        max_ffo = rules.get("max_ffo_to_debt")
        if max_ffo is not None:
            val = data.get("ffo_to_debt")
            if val is None or val > max_ffo:
                continue

        # FOCF / Total Debt
        min_focf = rules.get("min_focf_to_debt")
        if min_focf is not None:
            val = data.get("focf_to_debt")
            if val is None or val < min_focf:
                continue
        max_focf = rules.get("max_focf_to_debt")
        if max_focf is not None:
            val = data.get("focf_to_debt")
            if val is None or val > max_focf:
                continue

        # Operating Cash Flow (min, raw dollars)
        min_ocf = rules.get("min_operating_cashflow")
        if min_ocf is not None and data.get("operating_cashflow", 0) < min_ocf:
            continue

        # EBITDA (min, raw dollars)
        min_ebitda = rules.get("min_ebitda")
        if min_ebitda is not None and data.get("ebitda", 0) < min_ebitda:
            continue

        # EBITDA Margin
        min_em = rules.get("min_ebitda_margin")
        if min_em is not None:
            val = data.get("ebitda_margin")
            if val is None or val < min_em:
                continue
        max_em = rules.get("max_ebitda_margin")
        if max_em is not None:
            val = data.get("ebitda_margin")
            if val is None or val > max_em:
                continue

        # Gross Margin
        min_gm = rules.get("min_gross_margin")
        if min_gm is not None:
            val = data.get("gross_margin")
            if val is None or val < min_gm:
                continue
        max_gm = rules.get("max_gross_margin")
        if max_gm is not None:
            val = data.get("gross_margin")
            if val is None or val > max_gm:
                continue

        matches.append(symbol)

    matches.sort()
    return matches


# ---------------------------------------------------------------------------
# Pass 2: Research Filter (web scrape + LLM)
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM_PROMPT = """Company: {ticker} ({company_name})

{structured_facts}
Additional context:
{text}

The user wants stocks matching ALL of these criteria:
{rules}

Check each criterion against the KNOWN FACTS and context. Examples of how to interpret:
- "founder CEO" = CEO appears in the founder list
- "non-founder CEO" or "never founded" = CEO does NOT appear in the founder list
- "MBA CEO" = evidence the CEO holds an MBA degree
- "headquartered in X" = HQ matches X

If ALL criteria are satisfied, match is true. If any single criterion fails, match is false.

Reply ONLY with valid JSON, nothing else:
{{"match": true, "reason": "brief explanation"}}
or
{{"match": false, "reason": "brief explanation"}}"""


def _query_wikidata(ticker: str, company_name: str = "") -> dict[str, str]:
    """Query Wikidata SPARQL for structured company facts by company name."""
    import requests as _req
    if not company_name or company_name == ticker:
        return {}
    # Escape quotes in company name
    safe_name = company_name.replace('"', '\\"')
    query = f"""SELECT ?companyLabel ?ceoLabel ?founderLabel ?hqLabel ?industryLabel ?inception ?ceoBirthDate
    WHERE {{
      ?company rdfs:label "{safe_name}"@en .
      ?company wdt:P31/wdt:P279* wd:Q4830453 .
      OPTIONAL {{ ?company wdt:P169 ?ceo }}
      OPTIONAL {{ ?company wdt:P169 ?ceo . ?ceo wdt:P569 ?ceoBirthDate }}
      OPTIONAL {{ ?company wdt:P112 ?founder }}
      OPTIONAL {{ ?company wdt:P159 ?hq }}
      OPTIONAL {{ ?company wdt:P452 ?industry }}
      OPTIONAL {{ ?company wdt:P571 ?inception }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
    }}"""
    try:
        r = _req.get(
            "https://query.wikidata.org/sparql",
            params={"query": query, "format": "json"},
            headers={"User-Agent": "Lobsterminal/1.0 (stock research)"},
            timeout=10,
        )
        r.raise_for_status()
        bindings = r.json()["results"]["bindings"]
        if not bindings:
            return {}
        # Merge all rows (multiple founders, etc.)
        facts: dict[str, str] = {}
        founders = set()
        for row in bindings:
            for k, v in row.items():
                val = v.get("value", "")
                if k == "founderLabel":
                    founders.add(val)
                elif k not in facts or not facts[k]:
                    facts[k] = val
        if founders:
            facts["founderLabel"] = ", ".join(sorted(founders))
        return facts
    except Exception as e:
        logger.debug("Wikidata query failed for %s: %s", ticker, e)
        return {}


def _fetch_wikipedia_api(company_name: str) -> str | None:
    """Fetch clean Wikipedia article text via MediaWiki API (no scraping needed)."""
    import requests as _req
    try:
        r = _req.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "prop": "extracts",
                "exintro": False,
                "explaintext": True,
                "titles": company_name.replace(" ", "_"),
                "format": "json",
                "exsectionformat": "plain",
            },
            headers={"User-Agent": "Lobsterminal/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            text = page.get("extract", "")
            if text and len(text.strip()) > 100:
                return text[:4000]
        return None
    except Exception as e:
        logger.debug("MediaWiki API failed for %s: %s", company_name, e)
        return None


def _search_ddg(company_name: str, ticker: str, criteria: list[str]) -> str | None:
    """Search DuckDuckGo for company info related to the research criteria."""
    try:
        from ddgs import DDGS
        ddgs = DDGS()
        snippets = []

        # Query 1: CEO biography/education (most common qualitative ask)
        ceo_query = f"{company_name} CEO biography education background age"
        results = ddgs.text(ceo_query, max_results=3)
        for r in results or []:
            snippets.append(f"{r.get('title', '')}: {r.get('body', '')}")

        # Query 2: Criteria-specific search
        criteria_text = " ".join(criteria[:3])
        criteria_query = f"{company_name} {criteria_text}"
        results = ddgs.text(criteria_query, max_results=3)
        for r in results or []:
            snippets.append(f"{r.get('title', '')}: {r.get('body', '')}")

        # Query 3: CEO personal life / divorce / legal issues (if criteria mention it)
        criteria_lower = " ".join(criteria).lower()
        if any(kw in criteria_lower for kw in ["divorce", "personal", "legal", "scandal", "lawsuit"]):
            personal_query = f"{company_name} CEO divorce personal life legal issues"
            results = ddgs.text(personal_query, max_results=3)
            for r in results or []:
                snippets.append(f"{r.get('title', '')}: {r.get('body', '')}")

        if not snippets:
            return None
        text = "\n".join(snippets)
        return text[:2500] if text.strip() else None
    except Exception as e:
        logger.debug("DuckDuckGo search failed for %s: %s", company_name, e)
        return None


def _scrape_company_info(company_name: str, ticker: str, criteria: list[str] | None = None) -> tuple[dict[str, str], str | None]:
    """Gather company info from multiple sources. Returns (structured_facts, text_context).

    Sources (priority order):
    1. Wikidata SPARQL — structured CEO/founder/HQ/industry/founding date
    2. MediaWiki API — clean Wikipedia article text
    3. DuckDuckGo search — targeted search for criteria-specific info
    """
    # Source 1: Wikidata structured data
    wikidata_facts = _query_wikidata(ticker, company_name)

    # Source 2: Wikipedia clean text via API
    wiki_text = _fetch_wikipedia_api(company_name)

    # Source 3: DuckDuckGo search for criteria-specific info
    ddg_text = _search_ddg(company_name, ticker, criteria or []) if criteria else None

    # Combine text sources
    parts = []
    if wiki_text:
        parts.append(wiki_text)
    if ddg_text:
        parts.append(ddg_text)

    text = "\n\n---\n\n".join(parts) if parts else None
    return wikidata_facts, text


async def _research_one(
    client: httpx.AsyncClient,
    symbol: str,
    company_name: str,
    research_rules: list[str],
) -> dict[str, Any] | None:
    """Gather structured facts + web context, then ask LLM if company matches criteria."""
    loop = asyncio.get_event_loop()
    try:
        wikidata_facts, page_text = await loop.run_in_executor(
            None, _scrape_company_info, company_name, symbol, research_rules
        )
    except Exception as e:
        logger.warning("Failed to research %s: %s", symbol, e)
        return None

    # Build structured facts section for the prompt
    fact_lines = []
    field_labels = {
        "ceoLabel": "CEO",
        "ceoBirthDate": "CEO Birth Date",
        "founderLabel": "Founder(s)",
        "hqLabel": "Headquarters",
        "industryLabel": "Industry",
        "inception": "Founded",
        "companyLabel": "Official Name",
    }
    for key, label in field_labels.items():
        val = wikidata_facts.get(key)
        if val:
            if key == "inception" and "T" in val:
                val = val.split("T")[0]
            fact_lines.append(f"{label}: {val}")

    structured_section = ""
    if fact_lines:
        structured_section = "KNOWN FACTS (from Wikidata):\n" + "\n".join(fact_lines) + "\n"

    if not page_text and not fact_lines:
        return None

    # ---- Programmatic pre-check for common patterns ----
    # These are patterns where Wikidata structured data gives a definitive answer,
    # so we don't need to ask the 8B LLM (which hallucates on comparison tasks).
    ceo = wikidata_facts.get("ceoLabel", "").lower()
    founders = wikidata_facts.get("founderLabel", "").lower()
    hq = wikidata_facts.get("hqLabel", "").lower()
    remaining_rules = []
    pre_check_reasons = []
    pre_check_fail = False

    for rule in research_rules:
        rule_lower = rule.lower().strip()
        # Pattern: "founder CEO" / "ceo is founder" / "ceo founded"
        if any(kw in rule_lower for kw in ["founder ceo", "ceo is founder", "ceo founded"]):
            if ceo and founders:
                # Check if CEO name appears in founders list
                ceo_parts = [p.strip() for p in ceo.split() if len(p) > 2]
                if any(part in founders for part in ceo_parts):
                    pre_check_reasons.append(f"CEO '{ceo}' IS in founders list")
                else:
                    pre_check_fail = True
                    pre_check_reasons.append(f"CEO '{ceo}' is NOT in founders list: {founders}")
            else:
                remaining_rules.append(rule)
        # Pattern: "non-founder CEO" / "never founded" / "not a founder" / "was never a founder"
        elif any(kw in rule_lower for kw in ["non-founder", "never found", "not a founder", "never a founder", "wasn't a founder", "not founder"]):
            if ceo and founders:
                ceo_parts = [p.strip() for p in ceo.split() if len(p) > 2]
                if any(part in founders for part in ceo_parts):
                    pre_check_fail = True
                    pre_check_reasons.append(f"CEO '{ceo}' IS a founder (wanted non-founder)")
                else:
                    pre_check_reasons.append(f"CEO '{ceo}' is NOT a founder — matches non-founder criterion")
            else:
                remaining_rules.append(rule)
        # Pattern: "headquartered in X"
        elif "headquartered" in rule_lower or "based in" in rule_lower:
            if hq:
                # Extract the location from the rule
                loc = rule_lower.replace("headquartered in", "").replace("based in", "").strip()
                if loc and loc in hq:
                    pre_check_reasons.append(f"HQ '{hq}' matches '{loc}'")
                elif loc and loc not in hq:
                    pre_check_fail = True
                    pre_check_reasons.append(f"HQ '{hq}' does not match '{loc}'")
                else:
                    remaining_rules.append(rule)
            else:
                remaining_rules.append(rule)
        # Pattern: "MBA CEO" / "CEO with MBA" / "CEO has MBA"
        elif "mba" in rule_lower:
            # Search the text context for MBA evidence
            all_text = (page_text or "").lower()
            mba_indicators = ["mba", "m.b.a", "master of business", "business school",
                              "booth school", "wharton", "harvard business", "kellogg",
                              "sloan", "haas", "tuck", "darden", "ross school",
                              "stern school", "columbia business", "insead"]
            found_mba = any(ind in all_text for ind in mba_indicators)
            if found_mba:
                pre_check_reasons.append(f"MBA evidence found in text for CEO '{ceo}'")
            else:
                remaining_rules.append(rule)  # Let LLM try
        # Pattern: "CEO divorce" / "CEO going through divorce" / "personal crisis"
        elif any(kw in rule_lower for kw in ["divorce", "divorc", "personal crisis", "personal life"]):
            all_text = (page_text or "").lower()
            divorce_indicators = ["divorce", "divorced", "divorcing", "separation", "separated",
                                  "custody battle", "marital", "split from", "filed for divorce"]
            found_divorce = any(ind in all_text for ind in divorce_indicators)
            if found_divorce:
                pre_check_reasons.append(f"Divorce/personal crisis evidence found for CEO '{ceo}'")
            else:
                remaining_rules.append(rule)  # Let LLM try with web context
        # Pattern: "CEO under/over X years old" / "CEO age under X" / "young CEO"
        elif ("age" in rule_lower or "years old" in rule_lower or "under 4" in rule_lower
              or "under 5" in rule_lower or "over 5" in rule_lower or "over 6" in rule_lower
              or "young ceo" in rule_lower):
            ceo_birth = wikidata_facts.get("ceoBirthDate", "")
            if ceo_birth:
                try:
                    birth_year = int(ceo_birth[:4])
                    ceo_age = datetime.now().year - birth_year
                    # Extract target age from rule
                    age_match = re.search(r'(?:under|below|younger than|less than)\s+(\d+)', rule_lower)
                    over_match = re.search(r'(?:over|above|older than|more than)\s+(\d+)', rule_lower)
                    if age_match:
                        target = int(age_match.group(1))
                        if ceo_age < target:
                            pre_check_reasons.append(f"CEO '{ceo}' age {ceo_age} < {target}")
                        else:
                            pre_check_fail = True
                            pre_check_reasons.append(f"CEO '{ceo}' age {ceo_age} >= {target} (wanted under {target})")
                    elif over_match:
                        target = int(over_match.group(1))
                        if ceo_age > target:
                            pre_check_reasons.append(f"CEO '{ceo}' age {ceo_age} > {target}")
                        else:
                            pre_check_fail = True
                            pre_check_reasons.append(f"CEO '{ceo}' age {ceo_age} <= {target} (wanted over {target})")
                    elif "young" in rule_lower:
                        if ceo_age < 50:
                            pre_check_reasons.append(f"CEO '{ceo}' age {ceo_age} — considered young")
                        else:
                            pre_check_fail = True
                            pre_check_reasons.append(f"CEO '{ceo}' age {ceo_age} — not young")
                    else:
                        remaining_rules.append(rule)
                except (ValueError, IndexError):
                    remaining_rules.append(rule)
            else:
                # No birth date from Wikidata — try text search fallback
                all_text = (page_text or "").lower()
                # Look for birth year patterns like "born 1985" or "(born January 15, 1985)"
                birth_pat = re.search(r'born[^)]*?(\d{4})', all_text)
                if birth_pat:
                    try:
                        birth_year = int(birth_pat.group(1))
                        if 1940 < birth_year < 2010:
                            ceo_age = datetime.now().year - birth_year
                            age_match = re.search(r'(?:under|below|younger than|less than)\s+(\d+)', rule_lower)
                            if age_match:
                                target = int(age_match.group(1))
                                if ceo_age < target:
                                    pre_check_reasons.append(f"CEO born ~{birth_year}, age ~{ceo_age} < {target} (from text)")
                                else:
                                    pre_check_fail = True
                                    pre_check_reasons.append(f"CEO born ~{birth_year}, age ~{ceo_age} >= {target} (from text)")
                            else:
                                remaining_rules.append(rule)
                        else:
                            remaining_rules.append(rule)
                    except ValueError:
                        remaining_rules.append(rule)
                else:
                    remaining_rules.append(rule)
        else:
            remaining_rules.append(rule)

    # If pre-check definitively failed, return immediately without LLM call
    if pre_check_fail:
        return {
            "symbol": symbol,
            "match": False,
            "reason": "; ".join(pre_check_reasons),
        }

    # If all rules were resolved by pre-check (none remaining), return match
    if not remaining_rules and pre_check_reasons:
        return {
            "symbol": symbol,
            "match": True,
            "reason": "; ".join(pre_check_reasons),
        }

    # ---- LLM evaluation for remaining rules ----
    if page_text and len(page_text) > 3000:
        page_text = page_text[:3000]

    # Include pre-check results as context for the LLM
    llm_rules = remaining_rules
    pre_context = ""
    if pre_check_reasons:
        pre_context = "\nAlready verified: " + "; ".join(pre_check_reasons) + "\n"

    rules_text = "\n".join(f"- {r}" for r in llm_rules)
    prompt_text = RESEARCH_SYSTEM_PROMPT.format(
        ticker=symbol,
        company_name=company_name,
        structured_facts=structured_section + pre_context,
        text=page_text or "(no additional context available)",
        rules=rules_text,
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt_text}],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        resp = await client.post(OLLAMA_URL, json=payload, timeout=90.0)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        result = json.loads(raw)

        if "match" not in result and len(result) == 1:
            inner = next(iter(result.values()))
            if isinstance(inner, dict):
                result = inner

        return {
            "symbol": symbol,
            "match": result.get("match", False),
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        logger.warning("LLM research failed for %s: %s", symbol, e)
        return None


async def research_filter(
    tickers: list[str],
    sp500_list: list[dict[str, str]],
    research_rules: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Filter tickers by research_rules using web scraping + LLM."""
    if not research_rules:
        return tickers, {}

    name_map = {s["symbol"]: s["name"] for s in sp500_list}

    matched = []
    results = {}
    semaphore = asyncio.Semaphore(RESEARCH_CONCURRENCY)

    async def bounded_research(client, sym):
        async with semaphore:
            name = name_map.get(sym, sym)
            return await _research_one(client, sym, name, research_rules)

    async with httpx.AsyncClient() as client:
        tasks = [bounded_research(client, sym) for sym in tickers]
        outcomes = await asyncio.gather(*tasks)

    for outcome in outcomes:
        if outcome is None:
            continue
        results[outcome["symbol"]] = outcome["reason"]
        if outcome["match"]:
            matched.append(outcome["symbol"])

    matched.sort()
    return matched, results


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class StrategyRequest(BaseModel):
    prompt: str = Field(default="", description="Natural language strategy description")
    template: str = Field(default="", description="Template key (value, momentum, quality, dividend, low_vol, growth, small_value)")
    amount: float = Field(default=10000.0, gt=0, description="Investment amount in USD")
    period: str = Field(default="1y", description="Backtest period (6mo, 1y, 2y, 5y)")


class StrategyResponse(BaseModel):
    tickers: list[str]
    rules: dict[str, Any]
    research_results: dict[str, str]
    backtest: dict[str, Any]
    count: int
    universe_size: int
    filtered_count: int
    strategy_name: str


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def create_strategy(prompt: str, amount: float = 10000.0, period: str = "1y") -> dict[str, Any]:
    """Full strategy pipeline: parse -> filter -> research -> backtest."""
    # Step 1: Parse strategy with LLM
    parsed = await parse_strategy(prompt)
    api_rules = parsed["api_rules"]
    research_rules = parsed["research_rules"]
    strategy_name = parsed["strategy_name"]

    # Step 2: Get S&P 500 universe
    sp500_list = get_sp500_list()
    universe = get_sp500_fundamentals()
    universe_size = len(universe)

    # Step 3: Pass 1 — API filter
    filtered = api_filter(universe, api_rules)
    filtered_count = len(filtered)

    # Step 3.5: If ratio filters were used, ensure data is populated via AI lookup
    ratio_keys = ["max_debt_to_ebitda", "min_ffo_to_debt", "max_ffo_to_debt",
                   "min_focf_to_debt", "max_focf_to_debt", "min_ebitda_margin",
                   "max_ebitda_margin", "min_gross_margin", "max_gross_margin"]
    has_ratio_filter = any(api_rules.get(k) is not None for k in ratio_keys)

    if has_ratio_filter and filtered:
        for sym in list(filtered):
            stock_data = universe.get(sym, {})
            needs_lookup = any(stock_data.get(f) is None for f in
                             ["debt_to_ebitda", "ffo_to_debt", "focf_to_debt", "ebitda_margin", "gross_margin"])
            if needs_lookup:
                _ensure_ratio_fields(sym, stock_data, sp500_list)
        # Re-filter after AI lookup filled gaps
        filtered = api_filter(universe, api_rules)
        filtered_count = len(filtered)

    # Step 4: Pass 2 — Research filter (if research_rules exist)
    research_results = {}
    if research_rules and filtered:
        filtered, research_results = await research_filter(
            filtered, sp500_list, research_rules
        )

    if not filtered:
        return {
            "tickers": [],
            "rules": parsed,
            "research_results": research_results,
            "backtest": {"score": 0, "metrics": {}},
            "count": 0,
            "universe_size": universe_size,
            "filtered_count": filtered_count,
            "strategy_name": strategy_name,
        }

    # Step 5: Backtest survivors using existing backtest_engine
    from backtest_engine import fetch_historical_data, calculate_metrics, calculate_asset_score, BENCHMARK_TICKER

    try:
        loop = asyncio.get_event_loop()
        prices_df = await loop.run_in_executor(None, fetch_historical_data, filtered, period)

        if prices_df.empty or BENCHMARK_TICKER not in prices_df.columns:
            raise ValueError("Insufficient price data for backtest")

        benchmark_prices = prices_df[BENCHMARK_TICKER]
        portfolio_cols = [c for c in prices_df.columns if c != BENCHMARK_TICKER]
        if not portfolio_cols:
            raise ValueError("No valid portfolio tickers in price data")

        portfolio_prices = prices_df[portfolio_cols]
        metrics = calculate_metrics(portfolio_prices, benchmark_prices)
        score = calculate_asset_score(metrics)
    except Exception as e:
        logger.warning("Backtest failed: %s — returning tickers without backtest", e)
        return {
            "tickers": filtered,
            "rules": parsed,
            "research_results": research_results,
            "backtest": {"score": 0, "metrics": {}, "error": str(e)},
            "count": len(filtered),
            "universe_size": universe_size,
            "filtered_count": filtered_count,
            "strategy_name": strategy_name,
        }

    return {
        "tickers": [c for c in portfolio_cols],
        "rules": parsed,
        "research_results": research_results,
        "backtest": {
            "score": score,
            "metrics": metrics,
        },
        "count": len(portfolio_cols),
        "universe_size": universe_size,
        "filtered_count": filtered_count,
        "strategy_name": strategy_name,
    }


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["Strategy Engine"])


@router.get("/api/strategy/templates")
async def list_templates():
    """Return available strategy templates."""
    return {
        key: {"name": t["name"], "description": t["description"]}
        for key, t in STRATEGY_TEMPLATES.items()
    }


@router.get("/api/strategy/ratios")
async def list_ratios():
    """Return ratio explanations for UI tooltips."""
    return RATIO_EXPLANATIONS


@router.post("/api/strategy", response_model=StrategyResponse)
async def strategy_endpoint(body: StrategyRequest) -> StrategyResponse:
    """Build a custom ETF strategy from a natural language prompt or template."""
    # Resolve prompt: template overrides, but user prompt can extend/override template
    prompt = body.prompt
    if body.template and body.template in STRATEGY_TEMPLATES:
        template = STRATEGY_TEMPLATES[body.template]
        if prompt:
            # User provided both template and custom prompt — combine them
            prompt = f"{template['prompt']}. Additionally: {prompt}"
        else:
            prompt = template["prompt"]

    if not prompt or len(prompt.strip()) < 5:
        raise HTTPException(status_code=422, detail="Provide a prompt or select a template")

    try:
        result = await create_strategy(prompt, body.amount, body.period)
        return StrategyResponse(**result)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"LLM returned invalid JSON: {e}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ollama connection error: {e}")
    except Exception as e:
        logger.exception("Strategy creation failed")
        raise HTTPException(status_code=500, detail=str(e))
