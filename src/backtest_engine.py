"""Backtesting Engine — Fetch historical data, compute portfolio metrics, and score assets.

This FastAPI router provides a backtesting endpoint that:
  1. Downloads adjusted close prices from yfinance for user-supplied tickers + SPY benchmark.
  2. Calculates equal-weighted portfolio metrics (return, volatility, drawdown, Sharpe).
  3. Produces a 1-100 asset score reflecting risk-adjusted performance relative to SPY.
"""

import asyncio
import math
import logging

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


def fetch_historical_data(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    """Download adjusted close prices for *tickers* plus the SPY benchmark.

    Args:
        tickers: Equity ticker symbols (e.g. ``["AAPL", "MSFT"]``).
        period: Lookback window understood by ``yfinance.download``
                (e.g. ``"6mo"``, ``"1y"``, ``"2y"``, ``"5y"``).

    Returns:
        A ``pd.DataFrame`` indexed by date with one column per valid ticker
        and a ``SPY`` column.  Tickers that fail to download are silently
        dropped.  If *no* data can be retrieved the returned DataFrame is
        empty.

    Raises:
        No exceptions are raised; errors are logged and the function degrades
        gracefully.
    """
    # Normalise and deduplicate, always include SPY
    clean_tickers: list[str] = list(
        dict.fromkeys(t.strip().upper() for t in tickers if t.strip())
    )
    if BENCHMARK_TICKER not in clean_tickers:
        clean_tickers.append(BENCHMARK_TICKER)

    try:
        df: pd.DataFrame = yf.download(
            tickers=clean_tickers,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.error("yfinance download failed: %s", exc)
        return pd.DataFrame()

    if df.empty:
        logger.warning("yfinance returned empty DataFrame for tickers=%s", clean_tickers)
        return df

    # yf.download returns a MultiIndex (Price, Ticker) when len(tickers) > 1.
    # We want only the Close column for each ticker.
    if isinstance(df.columns, pd.MultiIndex):
        # Pick 'Close' level (auto_adjust=True already adjusts prices).
        if "Close" in df.columns.get_level_values(0):
            df = df["Close"]
        else:
            # Fallback: take the first price level available.
            first_level = df.columns.get_level_values(0)[0]
            df = df[first_level]
    else:
        # Single ticker — column names are price fields.  Keep Close only.
        if "Close" in df.columns:
            symbol = clean_tickers[0]
            df = df[["Close"]].rename(columns={"Close": symbol})
        else:
            return pd.DataFrame()

    # Drop any columns that are entirely NaN (invalid tickers).
    df = df.dropna(axis=1, how="all")

    # Forward-fill then back-fill small gaps (holidays that differ across
    # exchanges, etc.).
    df = df.ffill().bfill()

    return df


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
