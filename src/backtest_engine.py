"""Backtesting Engine — Fetch historical data, compute portfolio metrics, and score assets.

This FastAPI router provides a backtesting endpoint that:
  1. Downloads adjusted close prices from yfinance for user-supplied tickers + SPY benchmark.
  2. Calculates equal-weighted portfolio metrics (return, volatility, drawdown, Sharpe).
  3. Produces a 1-100 asset score reflecting risk-adjusted performance relative to SPY.
"""

import asyncio
import math
import logging
import os
import time
from datetime import datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BENCHMARK_TICKER = "SPY"
RISK_FREE_RATE = 0.05  # annualized
TRADING_DAYS_PER_YEAR = 252

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class BacktestRequest(BaseModel):
    """Request body for the backtest endpoint."""

    tickers: list[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="List of ticker symbols to backtest (max 50).",
    )
    period: str = Field(
        default="1y",
        description="Lookback period accepted by yfinance (e.g. '6mo', '1y', '2y', '5y').",
    )


class BacktestMetrics(BaseModel):
    """Computed portfolio metrics returned by the backtest."""

    total_return: float = Field(..., description="Portfolio total return as a percentage.")
    annualized_volatility: float = Field(
        ..., description="Annualized volatility (std dev of daily returns * sqrt(252))."
    )
    max_drawdown: float = Field(
        ..., description="Maximum peak-to-trough decline as a percentage."
    )
    sharpe_ratio: float = Field(
        ..., description="Annualized Sharpe ratio using 5% risk-free rate."
    )
    benchmark_return: float = Field(
        ..., description="SPY total return over the same period as a percentage."
    )


class BacktestResponse(BaseModel):
    """Full response from the backtest endpoint."""

    metrics: BacktestMetrics
    asset_score: int = Field(..., ge=1, le=100, description="Risk-adjusted score (1-100).")
    benchmark_return: float = Field(..., description="SPY total return (%) for convenience.")
    tickers_analyzed: int = Field(..., description="Number of valid tickers that were analyzed.")


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def _period_to_days(period: str) -> int:
    """Convert a yfinance-style period string to approximate calendar days."""
    p = period.lower().strip()
    if p.endswith("mo"):
        return int(p[:-2]) * 30
    if p.endswith("y"):
        return int(p[:-1]) * 365
    if p.endswith("d"):
        return int(p[:-1])
    return 365  # default 1y


def _yf_download(clean_tickers: list[str], period: str) -> pd.DataFrame:
    """Primary source: yfinance library."""
    try:
        df: pd.DataFrame = yf.download(
            tickers=clean_tickers,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.warning("yfinance download failed: %s", exc)
        return pd.DataFrame()

    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        if "Close" in df.columns.get_level_values(0):
            df = df["Close"]
        else:
            first_level = df.columns.get_level_values(0)[0]
            df = df[first_level]
    else:
        if "Close" in df.columns:
            symbol = clean_tickers[0]
            df = df[["Close"]].rename(columns={"Close": symbol})
        else:
            return pd.DataFrame()

    return df


def _yahoo_chart_api(tickers: list[str], period: str) -> pd.DataFrame:
    """Fallback 1: Yahoo Finance v8 chart API via direct HTTP.

    Bypasses yfinance library's rate-limit detection which is more
    aggressive than the raw API endpoint.
    """
    days = _period_to_days(period)
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    interval = "1d"

    frames: dict[str, pd.Series] = {}
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for ticker in tickers:
            try:
                url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                    f"?period1={start_ts}&period2={end_ts}&interval={interval}"
                )
                resp = client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if resp.status_code != 200:
                    logger.debug("Yahoo chart API %d for %s", resp.status_code, ticker)
                    continue
                data = resp.json()
                result = data.get("chart", {}).get("result", [])
                if not result:
                    continue
                timestamps = result[0].get("timestamp", [])
                closes = (
                    result[0]
                    .get("indicators", {})
                    .get("quote", [{}])[0]
                    .get("close", [])
                )
                if not timestamps or not closes or len(timestamps) != len(closes):
                    continue
                dates = pd.to_datetime(timestamps, unit="s").normalize()
                series = pd.Series(closes, index=dates, name=ticker, dtype=float)
                series = series.dropna()
                if len(series) > 5:
                    frames[ticker] = series
            except Exception as exc:
                logger.debug("Yahoo chart API error for %s: %s", ticker, exc)
                continue

    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    return df


def _finnhub_candles(tickers: list[str], period: str) -> pd.DataFrame:
    """Fallback 2: Finnhub stock candles API (requires FINNHUB_API_KEY)."""
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return pd.DataFrame()

    days = _period_to_days(period)
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400

    frames: dict[str, pd.Series] = {}
    with httpx.Client(timeout=15) as client:
        for ticker in tickers:
            try:
                url = (
                    f"https://finnhub.io/api/v1/stock/candle"
                    f"?symbol={ticker}&resolution=D"
                    f"&from={start_ts}&to={end_ts}"
                )
                resp = client.get(url, headers={"X-Finnhub-Token": api_key})
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if data.get("s") != "ok" or not data.get("t"):
                    continue
                dates = pd.to_datetime(data["t"], unit="s").normalize()
                closes = data.get("c", [])
                if len(dates) != len(closes):
                    continue
                series = pd.Series(closes, index=dates, name=ticker, dtype=float)
                series = series.dropna()
                if len(series) > 5:
                    frames[ticker] = series
                # Respect Finnhub free tier rate limit (30 calls/sec)
                time.sleep(0.05)
            except Exception as exc:
                logger.debug("Finnhub candle error for %s: %s", ticker, exc)
                continue

    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames)


def _alpha_vantage_daily(tickers: list[str], period: str) -> pd.DataFrame:
    """Fallback 3: Alpha Vantage TIME_SERIES_DAILY (requires ALPHA_VANTAGE_API_KEY).

    Free tier: 25 requests/day.  Used only as last resort.
    """
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        return pd.DataFrame()

    days = _period_to_days(period)
    outputsize = "full" if days > 100 else "compact"

    frames: dict[str, pd.Series] = {}
    with httpx.Client(timeout=20) as client:
        for ticker in tickers:
            try:
                url = (
                    f"https://www.alphavantage.co/query"
                    f"?function=TIME_SERIES_DAILY_ADJUSTED&symbol={ticker}"
                    f"&outputsize={outputsize}&apikey={api_key}"
                )
                resp = client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                ts = data.get("Time Series (Daily)", {})
                if not ts:
                    continue
                dates = []
                closes = []
                cutoff = datetime.now() - timedelta(days=days)
                for date_str, vals in sorted(ts.items()):
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    if dt >= cutoff:
                        dates.append(dt)
                        closes.append(float(vals.get("5. adjusted close", vals.get("4. close", 0))))
                if len(dates) > 5:
                    series = pd.Series(closes, index=pd.DatetimeIndex(dates), name=ticker, dtype=float)
                    frames[ticker] = series
                # Alpha Vantage: 5 calls/min on free tier
                time.sleep(0.5)
            except Exception as exc:
                logger.debug("Alpha Vantage error for %s: %s", ticker, exc)
                continue

    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames)


def _scrapling_yahoo_history(tickers: list[str], period: str) -> pd.DataFrame:
    """Fallback 4: Scrape Yahoo Finance historical data pages using Scrapling.

    Uses the download page URL which returns CSV data directly.
    """
    try:
        from scrapling import Fetcher
    except ImportError:
        logger.debug("Scrapling not available for history fallback")
        return pd.DataFrame()

    days = _period_to_days(period)
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400

    frames: dict[str, pd.Series] = {}
    fetcher = Fetcher(auto_match=False)
    for ticker in tickers:
        try:
            # Yahoo Finance download endpoint returns CSV
            url = (
                f"https://query1.finance.yahoo.com/v7/finance/download/{ticker}"
                f"?period1={start_ts}&period2={end_ts}&interval=1d&events=history"
            )
            resp = fetcher.get(url)
            if not resp or resp.status != 200:
                continue
            # Parse CSV text
            import io
            csv_text = resp.text
            if not csv_text or "Date" not in csv_text[:50]:
                continue
            csv_df = pd.read_csv(io.StringIO(csv_text), parse_dates=["Date"])
            if csv_df.empty or "Close" not in csv_df.columns:
                continue
            csv_df = csv_df.set_index("Date").sort_index()
            series = csv_df["Close"].dropna().rename(ticker)
            if len(series) > 5:
                frames[ticker] = series
        except Exception as exc:
            logger.debug("Scrapling Yahoo history error for %s: %s", ticker, exc)
            continue

    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames)


def fetch_historical_data(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    """Download adjusted close prices for *tickers* plus the SPY benchmark.

    Tries multiple data sources in order:
      1. yfinance library (primary)
      2. Yahoo Finance v8 chart API (direct HTTP, bypasses yfinance rate limiter)
      3. Finnhub candles API (if FINNHUB_API_KEY set)
      4. Alpha Vantage daily (if ALPHA_VANTAGE_API_KEY set, 25 req/day limit)
      5. Scrapling Yahoo Finance scrape (last resort web scraper)

    Returns:
        A ``pd.DataFrame`` indexed by date with one column per valid ticker
        and a ``SPY`` column.
    """
    clean_tickers: list[str] = list(
        dict.fromkeys(t.strip().upper() for t in tickers if t.strip())
    )
    if BENCHMARK_TICKER not in clean_tickers:
        clean_tickers.append(BENCHMARK_TICKER)

    sources = [
        ("yfinance", _yf_download),
        ("yahoo_chart_api", _yahoo_chart_api),
        ("finnhub", _finnhub_candles),
        ("alpha_vantage", _alpha_vantage_daily),
        ("scrapling_yahoo", _scrapling_yahoo_history),
    ]

    for source_name, fetch_fn in sources:
        try:
            df = fetch_fn(clean_tickers, period)
        except Exception as exc:
            logger.warning("%s fetch failed: %s", source_name, exc)
            continue

        if df.empty:
            logger.info("%s returned no data, trying next source...", source_name)
            continue

        # Drop columns that are entirely NaN
        df = df.dropna(axis=1, how="all")

        if df.empty or BENCHMARK_TICKER not in df.columns:
            logger.info("%s missing benchmark, trying next source...", source_name)
            continue

        # Forward-fill then back-fill small gaps
        df = df.ffill().bfill()

        found_tickers = [c for c in df.columns if c != BENCHMARK_TICKER]
        logger.info(
            "Backtest data from %s: %d tickers, %d days",
            source_name, len(found_tickers), len(df),
        )
        return df

    logger.warning("All data sources exhausted for tickers=%s", clean_tickers)
    return pd.DataFrame()


def calculate_metrics(portfolio_prices: pd.DataFrame, benchmark_prices: pd.Series) -> dict:
    """Compute equal-weighted portfolio metrics versus the SPY benchmark.

    An *equal-weighted* portfolio is constructed by normalising each asset's
    price series to start at 1.0 and then averaging across all assets on every
    trading day.

    Args:
        portfolio_prices: DataFrame of daily prices for the portfolio's
            constituent tickers (columns = ticker symbols).
        benchmark_prices: Series of daily SPY prices aligned to the same date
            index.

    Returns:
        Dictionary with keys ``total_return``, ``annualized_volatility``,
        ``max_drawdown``, ``sharpe_ratio``, and ``benchmark_return``, each
        expressed as a percentage (or ratio for Sharpe).
    """
    # --- Equal-weighted portfolio value series ---
    normalised = portfolio_prices.div(portfolio_prices.iloc[0])
    portfolio_value = normalised.mean(axis=1)

    daily_returns = portfolio_value.pct_change().dropna()

    # Total return (%)
    total_return = float((portfolio_value.iloc[-1] / portfolio_value.iloc[0] - 1) * 100)

    # Annualised volatility (%)
    annualized_volatility = float(daily_returns.std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100)

    # Maximum drawdown (%)
    cumulative_max = portfolio_value.cummax()
    drawdowns = (portfolio_value - cumulative_max) / cumulative_max
    max_drawdown = float(drawdowns.min() * 100)

    # Sharpe ratio (annualised)
    n_days = len(daily_returns)
    if n_days > 1 and annualized_volatility > 0:
        annualized_return = total_return / 100  # as a decimal
        # Scale to annualised if period < 1 year
        years = n_days / TRADING_DAYS_PER_YEAR
        annualized_return_rate = (1 + annualized_return) ** (1 / years) - 1 if years > 0 else 0
        sharpe_ratio = float(
            (annualized_return_rate - RISK_FREE_RATE)
            / (annualized_volatility / 100)
        )
    else:
        sharpe_ratio = 0.0

    # Benchmark return (%)
    benchmark_return = float(
        (benchmark_prices.iloc[-1] / benchmark_prices.iloc[0] - 1) * 100
    )

    return {
        "total_return": round(total_return, 2),
        "annualized_volatility": round(annualized_volatility, 2),
        "max_drawdown": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "benchmark_return": round(benchmark_return, 2),
    }


def calculate_asset_score(metrics: dict) -> int:
    """Derive a 1-100 score reflecting risk-adjusted performance vs SPY.

    The score blends three signals:

    * **Excess return** (portfolio return minus benchmark return): mapped to a
      0-40 band.
    * **Sharpe ratio**: mapped to a 0-40 band (Sharpe >= 2.0 saturates).
    * **Drawdown penalty**: mapped to a 0-20 deduction (deeper drawdown =>
      lower score).

    The raw composite is clamped to [1, 100].

    Args:
        metrics: Dictionary produced by :func:`calculate_metrics`.

    Returns:
        Integer score between 1 and 100 inclusive.
    """
    excess_return = metrics["total_return"] - metrics["benchmark_return"]
    sharpe = metrics["sharpe_ratio"]
    max_dd = abs(metrics["max_drawdown"])  # positive magnitude

    # 1. Excess return component (0-40).  ±20% excess maps linearly to 0-40.
    excess_score = 20 + (excess_return / 20) * 20  # centre at 20
    excess_score = max(0.0, min(40.0, excess_score))

    # 2. Sharpe component (0-40).  Sharpe 0 -> 10, Sharpe 1 -> 25, Sharpe >= 2 -> 40.
    sharpe_score = 10 + (sharpe / 2.0) * 30
    sharpe_score = max(0.0, min(40.0, sharpe_score))

    # 3. Drawdown penalty (0-20).  0% dd -> 20, >=40% dd -> 0.
    dd_score = max(0.0, 20 - (max_dd / 40) * 20)

    raw = excess_score + sharpe_score + dd_score
    return max(1, min(100, round(raw)))


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.post(
    "/api/generate-asset/backtest",
    response_model=BacktestResponse,
    summary="Run a backtest on the supplied tickers",
    tags=["Backtesting"],
)
async def run_backtest(request: BacktestRequest) -> BacktestResponse:
    """Execute an equal-weighted backtest for the requested tickers.

    Downloads historical adjusted-close prices from Yahoo Finance, computes
    portfolio-level risk/return metrics, and returns a composite asset score
    (1-100) that captures risk-adjusted performance relative to SPY.

    Raises:
        HTTPException 400: If no valid price data could be retrieved for any
            of the supplied tickers.
        HTTPException 422: If the request body is malformed (handled
            automatically by FastAPI / Pydantic).
    """
    tickers_upper = [t.strip().upper() for t in request.tickers if t.strip()]
    if not tickers_upper:
        raise HTTPException(status_code=400, detail="No valid ticker symbols provided.")

    # I2 fix: run blocking yfinance in thread executor
    try:
        df = await asyncio.get_event_loop().run_in_executor(
            None, fetch_historical_data, tickers_upper, request.period
        )
    except Exception as exc:
        logger.exception("Unexpected error fetching historical data")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to retrieve market data: {exc}",
        )

    if df.empty or BENCHMARK_TICKER not in df.columns:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not retrieve sufficient price data. "
                "Check that the ticker symbols are valid and try again."
            ),
        )

    # Separate benchmark from portfolio assets
    benchmark_prices: pd.Series = df[BENCHMARK_TICKER]
    portfolio_columns = [c for c in df.columns if c != BENCHMARK_TICKER]

    if not portfolio_columns:
        raise HTTPException(
            status_code=400,
            detail="None of the supplied tickers returned valid data.",
        )

    portfolio_prices: pd.DataFrame = df[portfolio_columns]

    # --- Calculate ---
    metrics = calculate_metrics(portfolio_prices, benchmark_prices)
    score = calculate_asset_score(metrics)

    return BacktestResponse(
        metrics=BacktestMetrics(**metrics),
        asset_score=score,
        benchmark_return=metrics["benchmark_return"],
        tickers_analyzed=len(portfolio_columns),
    )
