"""
Valuation Engine — DCF valuation and fundamental scoring.

Implements Dexter's DCF workflow pattern:
  1. Gather financial data (FCF history, key ratios, balance sheet, estimates)
  2. Calculate FCF growth rate (CAGR with haircut, capped at 15%)
  3. Estimate WACC using sector-specific ranges
  4. Project 5-year cash flows with growth decay
  5. Terminal value via Gordon Growth Model
  6. Sensitivity analysis (WACC vs terminal growth)
  7. Validation checks

Uses Financial Datasets API (dexter_client) for institutional-grade data.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("valuation_engine")

# ---------------------------------------------------------------------------
# Sector WACC Ranges (from Dexter's sector-wacc.md)
# ---------------------------------------------------------------------------

SECTOR_WACC: dict[str, dict[str, float]] = {
    "Technology":              {"low": 8.0,  "mid": 10.0, "high": 12.0},
    "Healthcare":              {"low": 7.5,  "mid": 9.5,  "high": 11.5},
    "Financial Services":      {"low": 8.0,  "mid": 10.0, "high": 12.0},
    "Consumer Cyclical":       {"low": 8.5,  "mid": 10.5, "high": 12.5},
    "Consumer Defensive":      {"low": 6.0,  "mid": 8.0,  "high": 10.0},
    "Industrials":             {"low": 7.0,  "mid": 9.0,  "high": 11.0},
    "Energy":                  {"low": 8.0,  "mid": 10.0, "high": 12.0},
    "Communication Services":  {"low": 7.5,  "mid": 9.5,  "high": 11.5},
    "Real Estate":             {"low": 6.0,  "mid": 8.0,  "high": 10.0},
    "Utilities":               {"low": 5.0,  "mid": 7.0,  "high": 9.0},
    "Basic Materials":         {"low": 7.5,  "mid": 9.5,  "high": 11.5},
}

# Fallback for unknown sectors
DEFAULT_WACC = {"low": 8.0, "mid": 10.0, "high": 12.0}

# WACC adjustment factors
WACC_ADJUSTMENTS = {
    "high_debt":        +1.0,   # Debt/equity > 1.5
    "low_debt":         -0.5,   # Debt/equity < 0.3
    "small_cap":        +1.5,   # Market cap < $2B
    "mega_cap":         -0.5,   # Market cap > $200B
    "high_margin":      -0.5,   # Net margin > 20%
    "negative_margin":  +1.5,   # Net margin < 0
}

TERMINAL_GROWTH_RATE = 0.025  # 2.5% long-term GDP growth
GROWTH_DECAY_RATE = 0.05      # 5% annual decay in FCF growth
MAX_GROWTH_RATE = 0.15        # Cap FCF growth at 15%
HAIRCUT_PERCENT = 0.15        # 15% haircut on calculated CAGR
PROJECTION_YEARS = 5


def _estimate_wacc(sector: str, debt_to_equity: float, market_cap: float, net_margin: float) -> float:
    """Estimate WACC using sector ranges + adjustment factors."""
    wacc_range = SECTOR_WACC.get(sector, DEFAULT_WACC)
    wacc = wacc_range["mid"] / 100.0  # Start with mid-range

    # Apply adjustments
    if debt_to_equity > 1.5:
        wacc += WACC_ADJUSTMENTS["high_debt"] / 100.0
    elif debt_to_equity < 0.3:
        wacc += WACC_ADJUSTMENTS["low_debt"] / 100.0

    if market_cap < 2e9:
        wacc += WACC_ADJUSTMENTS["small_cap"] / 100.0
    elif market_cap > 200e9:
        wacc += WACC_ADJUSTMENTS["mega_cap"] / 100.0

    if net_margin > 0.20:
        wacc += WACC_ADJUSTMENTS["high_margin"] / 100.0
    elif net_margin < 0:
        wacc += WACC_ADJUSTMENTS["negative_margin"] / 100.0

    return max(0.04, min(0.18, wacc))  # Clamp to 4-18%


def _calculate_cagr(values: list[float]) -> float:
    """Calculate compound annual growth rate from a list of values."""
    if len(values) < 2:
        return 0.0
    start = values[0]
    end = values[-1]
    if start <= 0 or end <= 0:
        return 0.0
    years = len(values) - 1
    return (end / start) ** (1.0 / years) - 1.0


def run_dcf(
    ticker: str,
    fcf_history: list[float],
    shares_outstanding: float,
    total_debt: float,
    cash_and_equivalents: float,
    sector: str,
    debt_to_equity: float,
    market_cap: float,
    net_margin: float,
    current_price: float,
    analyst_growth: float | None = None,
) -> dict[str, Any]:
    """Run a full DCF valuation.

    Args:
        ticker: Stock ticker symbol.
        fcf_history: List of annual FCF values (oldest first), at least 2 years.
        shares_outstanding: Total shares outstanding.
        total_debt: Total debt on balance sheet.
        cash_and_equivalents: Cash and short-term investments.
        sector: Company sector for WACC estimation.
        debt_to_equity: Debt-to-equity ratio.
        market_cap: Current market capitalization.
        net_margin: Net profit margin (decimal, e.g., 0.25 for 25%).
        current_price: Current stock price.
        analyst_growth: Optional analyst-estimated growth rate (decimal).

    Returns:
        Dict with DCF results including fair value, upside, sensitivity matrix.
    """
    if len(fcf_history) < 2 or shares_outstanding <= 0:
        return {"error": "Insufficient data for DCF valuation", "ticker": ticker}

    latest_fcf = fcf_history[-1]
    if latest_fcf <= 0:
        return {"error": "Negative or zero FCF — DCF not applicable", "ticker": ticker}

    # Step 1: Calculate FCF growth rate
    raw_cagr = _calculate_cagr(fcf_history)
    # Apply haircut (10-20%)
    haircut_cagr = raw_cagr * (1.0 - HAIRCUT_PERCENT)
    # Cap at MAX_GROWTH_RATE
    fcf_growth = min(haircut_cagr, MAX_GROWTH_RATE)
    # If analyst growth available, blend (60% historical, 40% analyst)
    if analyst_growth is not None and analyst_growth > 0:
        fcf_growth = fcf_growth * 0.6 + min(analyst_growth, MAX_GROWTH_RATE) * 0.4
    fcf_growth = max(0.01, fcf_growth)  # Floor at 1%

    # Step 2: Estimate WACC
    wacc = _estimate_wacc(sector, debt_to_equity, market_cap, net_margin)

    # Step 3: Project 5-year cash flows with growth decay
    projected_fcf = []
    current_growth = fcf_growth
    cf = latest_fcf
    for year in range(1, PROJECTION_YEARS + 1):
        cf = cf * (1 + current_growth)
        projected_fcf.append({"year": year, "fcf": round(cf), "growth": round(current_growth * 100, 2)})
        current_growth = max(TERMINAL_GROWTH_RATE, current_growth * (1 - GROWTH_DECAY_RATE))

    # Step 4: Terminal value (Gordon Growth Model)
    terminal_fcf = projected_fcf[-1]["fcf"]
    if wacc <= TERMINAL_GROWTH_RATE:
        wacc = TERMINAL_GROWTH_RATE + 0.01
        warnings.append(f"WACC adjusted to {wacc*100:.1f}% (was <= terminal growth rate {TERMINAL_GROWTH_RATE*100:.1f}%)")
    terminal_value = terminal_fcf * (1 + TERMINAL_GROWTH_RATE) / (wacc - TERMINAL_GROWTH_RATE)

    # Step 5: Discount all cash flows to present value
    pv_fcfs = []
    total_pv_fcf = 0
    for p in projected_fcf:
        discount_factor = 1 / ((1 + wacc) ** p["year"])
        pv = p["fcf"] * discount_factor
        pv_fcfs.append(round(pv))
        total_pv_fcf += pv

    pv_terminal = terminal_value / ((1 + wacc) ** PROJECTION_YEARS)

    # Step 6: Enterprise value and equity value
    enterprise_value = total_pv_fcf + pv_terminal
    equity_value = enterprise_value - total_debt + cash_and_equivalents
    fair_value_per_share = equity_value / shares_outstanding

    # Step 7: Upside/downside
    upside_pct = ((fair_value_per_share / current_price) - 1) * 100 if current_price > 0 else 0

    # Step 8: Sensitivity analysis (3x3: WACC vs terminal growth)
    sensitivity = []
    for wacc_delta in [-0.01, 0, 0.01]:
        row = []
        for tg in [0.020, 0.025, 0.030]:
            adj_wacc = wacc + wacc_delta
            if adj_wacc <= tg:
                row.append(None)
                continue
            tv = terminal_fcf * (1 + tg) / (adj_wacc - tg)
            pv_tv = tv / ((1 + adj_wacc) ** PROJECTION_YEARS)
            adj_pv_fcf = sum(p["fcf"] / ((1 + adj_wacc) ** p["year"]) for p in projected_fcf)
            adj_ev = adj_pv_fcf + pv_tv
            adj_eq = adj_ev - total_debt + cash_and_equivalents
            adj_fv = adj_eq / shares_outstanding
            row.append(round(adj_fv, 2))
        sensitivity.append({
            "wacc": round((wacc + wacc_delta) * 100, 1),
            "tg_2_0": row[0],
            "tg_2_5": row[1],
            "tg_3_0": row[2],
        })

    # Step 9: Validation
    terminal_pct = (pv_terminal / enterprise_value * 100) if enterprise_value > 0 else 0
    warnings = []
    if terminal_pct > 80:
        warnings.append("Terminal value is >80% of EV — projections may be unreliable")
    if terminal_pct < 40:
        warnings.append("Terminal value is <40% of EV — unusual for DCF")
    if abs(enterprise_value / max(market_cap, 1) - 1) > 0.5:
        warnings.append("Calculated EV diverges >50% from market cap — review assumptions")

    return {
        "ticker": ticker,
        "fair_value": round(fair_value_per_share, 2),
        "current_price": round(current_price, 2),
        "upside_pct": round(upside_pct, 1),
        "verdict": "UNDERVALUED" if upside_pct > 15 else "OVERVALUED" if upside_pct < -15 else "FAIRLY VALUED",
        "wacc": round(wacc * 100, 2),
        "fcf_growth_rate": round(fcf_growth * 100, 2),
        "enterprise_value": round(enterprise_value),
        "equity_value": round(equity_value),
        "pv_projected_fcf": round(total_pv_fcf),
        "pv_terminal_value": round(pv_terminal),
        "terminal_value_pct": round(terminal_pct, 1),
        "projected_fcf": projected_fcf,
        "sensitivity": sensitivity,
        "inputs": {
            "latest_fcf": round(latest_fcf),
            "fcf_history_years": len(fcf_history),
            "shares_outstanding": round(shares_outstanding),
            "total_debt": round(total_debt),
            "cash": round(cash_and_equivalents),
            "sector": sector,
            "debt_to_equity": round(debt_to_equity, 2),
            "net_margin_pct": round(net_margin * 100, 2),
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Fundamental Score (0-100)
# ---------------------------------------------------------------------------

def score_fundamentals(ratios: dict[str, Any]) -> dict[str, Any]:
    """Score a stock's fundamentals on a 0-100 scale.

    Categories:
      - Profitability (25 pts): ROE, net margin, gross margin
      - Growth (25 pts): revenue growth, EPS growth, FCF growth
      - Valuation (25 pts): P/E, PEG, EV/EBITDA, P/S
      - Financial Health (25 pts): current ratio, D/E, quick ratio, FCF yield

    Returns dict with total score, category scores, and grade.
    """

    def _clamp(val, lo, hi):
        return max(lo, min(hi, val))

    def _safe(key, default=0):
        v = ratios.get(key, default)
        return v if v is not None else default

    # --- Profitability (25 pts) ---
    roe = _safe("return_on_equity", 0)
    net_margin = _safe("net_margin", 0)
    gross_margin = _safe("gross_margin", 0)

    # ROE: 0-10% = 0-3, 10-20% = 3-6, 20-40% = 6-9, >40% = 9-10
    roe_score = _clamp(roe * 100 / 4, 0, 10) if roe > 0 else 0
    # Net margin: 0-10% = 0-4, 10-25% = 4-7, >25% = 7-8
    margin_score = _clamp(net_margin * 100 / 3.5, 0, 8) if net_margin > 0 else 0
    # Gross margin: >50% = 7, 30-50% = 4-7, <30% = 0-4
    gm_score = _clamp(gross_margin * 100 / 7.5, 0, 7) if gross_margin > 0 else 0

    profitability = round(min(25, roe_score + margin_score + gm_score), 1)

    # --- Growth (25 pts) ---
    rev_growth = _safe("revenue_growth", 0)
    eps_growth = _safe("earnings_per_share_growth", 0)
    fcf_growth = _safe("free_cash_flow_growth", 0)

    # Revenue growth: >20% = 10, 10-20% = 5-10, 0-10% = 0-5, negative = 0
    rg_score = _clamp(rev_growth * 100 / 2, 0, 10) if rev_growth > 0 else 0
    # EPS growth: >25% = 8, 10-25% = 4-8, <10% = 0-4
    eg_score = _clamp(eps_growth * 100 / 3, 0, 8) if eps_growth > 0 else 0
    # FCF growth: >20% = 7, 0-20% = 0-7
    fg_score = _clamp(fcf_growth * 100 / 3, 0, 7) if fcf_growth > 0 else 0

    growth = round(min(25, rg_score + eg_score + fg_score), 1)

    # --- Valuation (25 pts, inverse — lower is better) ---
    pe = _safe("price_to_earnings_ratio", 0)
    peg = _safe("peg_ratio", 0)
    ev_ebitda = _safe("enterprise_value_to_ebitda_ratio", 0)
    ps = _safe("price_to_sales_ratio", 0)

    # P/E: <15 = 8, 15-25 = 4-8, 25-40 = 2-4, >40 = 0-2
    pe_score = _clamp(8 - max(0, pe - 15) / 3, 0, 8) if 0 < pe < 100 else 0
    # PEG: <1 = 7, 1-2 = 3-7, >2 = 0-3
    peg_score = _clamp(7 - max(0, peg - 1) * 4, 0, 7) if 0 < peg < 10 else 0
    # EV/EBITDA: <10 = 5, 10-20 = 2-5, >20 = 0-2
    eve_score = _clamp(5 - max(0, ev_ebitda - 10) / 2, 0, 5) if 0 < ev_ebitda < 50 else 0
    # P/S: <2 = 5, 2-5 = 2-5, >5 = 0-2
    ps_score = _clamp(5 - max(0, ps - 2), 0, 5) if 0 < ps < 30 else 0

    valuation = round(min(25, pe_score + peg_score + eve_score + ps_score), 1)

    # --- Financial Health (25 pts) ---
    current_ratio = _safe("current_ratio", 0)
    de = _safe("debt_to_equity", 0)
    quick_ratio = _safe("quick_ratio", 0)
    fcf_yield = _safe("free_cash_flow_yield", 0)

    # Current ratio: 1.5-3 = 8, 1-1.5 = 4-8, <1 = 0-4, >3 = 6
    if current_ratio >= 1.5:
        cr_score = min(8, 6 + (current_ratio - 1.5))
    elif current_ratio >= 1.0:
        cr_score = 4 + (current_ratio - 1.0) * 8
    else:
        cr_score = max(0, current_ratio * 4)

    # D/E: <0.5 = 7, 0.5-1 = 4-7, 1-2 = 2-4, >2 = 0-2
    de_score = _clamp(7 - max(0, de - 0.5) * 4, 0, 7) if de >= 0 else 3

    # Quick ratio: >1 = 5, 0.5-1 = 2-5, <0.5 = 0-2
    qr_score = min(5, quick_ratio * 5) if quick_ratio > 0 else 0

    # FCF yield: >8% = 5, 4-8% = 2-5, <4% = 0-2
    fy_score = _clamp(fcf_yield * 100 / 1.6, 0, 5) if fcf_yield > 0 else 0

    health = round(min(25, cr_score + de_score + qr_score + fy_score), 1)

    # --- Total ---
    total = round(profitability + growth + valuation + health, 1)

    # Grade
    if total >= 80:
        grade = "A"
    elif total >= 65:
        grade = "B"
    elif total >= 50:
        grade = "C"
    elif total >= 35:
        grade = "D"
    else:
        grade = "F"

    return {
        "total_score": total,
        "grade": grade,
        "profitability": profitability,
        "growth": growth,
        "valuation": valuation,
        "financial_health": health,
    }


# ---------------------------------------------------------------------------
# High-level DCF from ticker (uses dexter_client)
# ---------------------------------------------------------------------------

def dcf_from_ticker(ticker: str) -> dict[str, Any]:
    """Run full DCF valuation for a ticker using Financial Datasets API."""
    try:
        import dexter_client
    except ImportError:
        return {"error": "dexter_client not available", "ticker": ticker}

    # Gather data
    ratios = dexter_client.get_key_ratios(ticker)
    if not ratios:
        return {"error": "Could not fetch key ratios", "ticker": ticker}

    # Get FCF history from cash flow statements
    cashflows = dexter_client.get_cash_flow_statements(ticker, period="annual", limit=5)
    if len(cashflows) < 2:
        return {"error": "Insufficient cash flow history (need 2+ years)", "ticker": ticker}

    # Extract FCF values (oldest first)
    fcf_history = []
    for cf in reversed(cashflows):
        fcf = cf.get("free_cash_flow", 0) or 0
        if fcf == 0:
            # Calculate from operating CF - capex
            op_cf = cf.get("operating_cash_flow", 0) or 0
            capex = abs(cf.get("capital_expenditure", 0) or 0)
            fcf = op_cf - capex
        fcf_history.append(fcf)

    if not any(f > 0 for f in fcf_history):
        return {"error": "No positive FCF in history — DCF not applicable", "ticker": ticker}

    # Get balance sheet for debt/cash/shares
    balance = dexter_client.get_balance_sheets(ticker, period="annual", limit=1)
    total_debt = 0
    cash = 0
    shares = 0
    if balance:
        bs = balance[0]
        total_debt = (bs.get("total_debt", 0) or 0)
        if total_debt == 0:
            total_debt = (bs.get("non_current_debt", 0) or 0) + (bs.get("current_debt", 0) or 0)
        cash = (bs.get("cash_and_equivalents", 0) or bs.get("cash_and_short_term_investments", 0) or 0)
        shares = (bs.get("outstanding_shares", 0) or 0)

    # Get price snapshot for current price
    price_snap = dexter_client.get_price_snapshot(ticker)
    current_price = 0
    if price_snap:
        current_price = price_snap.get("price", 0) or 0

    # Fallback for shares: from ratios, or from income statement
    if shares == 0:
        shares = ratios.get("shares_outstanding", 0) or 0
    if shares == 0:
        income = dexter_client.get_income_statements(ticker, period="annual", limit=1)
        if income:
            shares = income[0].get("weighted_average_shares_diluted", 0) or income[0].get("weighted_average_shares", 0) or 0
    mcap_val = ratios.get("market_cap") or 0
    if shares == 0 and isinstance(mcap_val, (int, float)) and mcap_val > 0 and current_price > 0:
        shares = mcap_val / current_price

    # Get analyst estimates for growth blending
    estimates = dexter_client.get_analyst_estimates(ticker, period="annual")
    analyst_growth = None
    if estimates and len(estimates) >= 2:
        eps_vals = [e.get("earnings_per_share", 0) for e in estimates if e.get("earnings_per_share")]
        if len(eps_vals) >= 2:
            analyst_growth = (eps_vals[0] / eps_vals[-1]) ** (1.0 / max(1, len(eps_vals) - 1)) - 1

    # Extract ratios
    sector = ratios.get("sector") or ""
    if not sector:
        # Try FMP profile for sector
        try:
            import fmp_client
            if fmp_client._enabled():
                profile = fmp_client.get_profile(ticker)
                if profile:
                    sector = profile.get("sector", "") or ""
        except Exception:
            pass
    if not sector:
        sector = "Technology"  # Safe default
    de = ratios.get("debt_to_equity", 0) or 0
    mcap = ratios.get("market_cap") or (shares * current_price if shares and current_price else 0)
    nm = ratios.get("net_margin", 0) or 0

    return run_dcf(
        ticker=ticker,
        fcf_history=fcf_history,
        shares_outstanding=shares,
        total_debt=total_debt,
        cash_and_equivalents=cash,
        sector=sector,
        debt_to_equity=de,
        market_cap=mcap,
        net_margin=nm,
        current_price=current_price,
        analyst_growth=analyst_growth,
    )


def score_from_ticker(ticker: str) -> dict[str, Any]:
    """Score a stock's fundamentals using Financial Datasets API."""
    try:
        import dexter_client
    except ImportError:
        return {"error": "dexter_client not available", "ticker": ticker}

    ratios = dexter_client.get_key_ratios(ticker)
    if not ratios:
        return {"error": "Could not fetch key ratios", "ticker": ticker}

    result = score_fundamentals(ratios)
    result["ticker"] = ticker
    return result
