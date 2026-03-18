"""Commodities adapter — STUB, needs full rewrite (task 38).

KNOWN ISSUES (from audit):
- Uses wrong NormalizedEvent (Pydantic BaseModel instead of dataclass from adapters.models)
- Wrong import path (adapters.registry instead of adapters.base)
- Uses random.uniform() for price noise instead of real implied probabilities
- Wrong field names (event_name vs title, market_end_time vs expiry)

DO NOT register this adapter in server.py until rewritten.
See: tasks.md task 38
"""

import yfinance as yf
from datetime import datetime
import logging
from adapters.base import BaseAdapter
from adapters.models import NormalizedEvent

logger = logging.getLogger(__name__)

# Commodity tickers and example price targets for hypothetical prediction events
COMMODITY_TARGETS = {
    "GC=F": {"name": "Gold", "targets": [(2300, 2500, datetime(2026, 12, 31)), (2000, 2200, datetime(2024, 12, 31))]},
    "SI=F": {"name": "Silver", "targets": [(28, 32, datetime(2026, 12, 31)), (24, 26, datetime(2024, 12, 31))]},
    "CL=F": {"name": "Crude Oil (WTI)", "targets": [(80, 90, datetime(2025, 12, 31)), (70, 75, datetime(2024, 12, 31))]},
    "NG=F": {"name": "Natural Gas", "targets": [(3, 4, datetime(2025, 12, 31)), (2.5, 3.0, datetime(2024, 12, 31))]},
    "HG=F": {"name": "Copper", "targets": [(4.5, 5.0, datetime(2025, 12, 31)), (4.0, 4.3, datetime(2024, 12, 31))]},
    "ZC=F": {"name": "Corn", "targets": [(500, 550, datetime(2025, 12, 31)), (450, 480, datetime(2024, 12, 31))]},
    "ZW=F": {"name": "Wheat", "targets": [(700, 750, datetime(2025, 12, 31)), (650, 680, datetime(2024, 12, 31))]},
    "ZS=F": {"name": "Soybeans", "targets": [(1300, 1350, datetime(2025, 12, 31)), (1250, 1280, datetime(2024, 12, 31))]},
}


class CommoditiesAdapter(BaseAdapter):
    platform_id: str = "commodities"
    platform_name: str = "Commodities"

    async def fetch_events(self) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        for ticker_symbol, data in COMMODITY_TARGETS.items():
            try:
                ticker = yf.Ticker(ticker_symbol)
                # Fetching latest price
                hist = ticker.history(period="1d", interval="1m")
                if not hist.empty:
                    current_price = hist["Close"].iloc[-1]
                else:
                    info = ticker.fast_info
                    current_price = info.last_price or info.previous_close or 0.0

                if current_price == 0.0:
                    logger.warning(f"Could not fetch current price for {data['name']} ({ticker_symbol}), skipping.")
                    continue

                for target_low, target_high, end_date in data["targets"]:
                    # Create two types of events: "Price > TargetHigh" and "Price < TargetLow"
                    # Simple heuristic for yes_price/no_price:
                    # The further the current price is from the target, the lower the probability for the "beyond target" event.
                    # This is a highly simplified model for demonstration.

                    # Event 1: "Price > TargetHigh by end_date"
                    title_high = f"{data['name']} Price > ${target_high} by {end_date.strftime('%b %Y')}"
                    
                    if current_price >= target_high:
                        yes_p_high = 0.9 
                    else:
                        # Scale based on difference, lower if far below
                        distance_ratio = (current_price - target_high) / target_high
                        yes_p_high = max(0.01, min(0.99, 0.5 + distance_ratio * 2))
                        yes_p_high = round(yes_p_high * 0.5 + 0.05, 2) # Make it generally low

                    yes_p_high = max(0.01, min(0.99, yes_p_high))
                    no_p_high = 1.0 - yes_p_high
                    events.append(
                        NormalizedEvent(
                            event_id=f"commodities_{ticker_symbol}_high_{target_high}_{end_date.year}{end_date.month}",
                            title=title_high,
                            platform=self.platform_name,
                            url=f"https://finance.yahoo.com/quote/{ticker_symbol}",
                            yes_price=yes_p_high,
                            no_price=no_p_high,
                            expiry=int(end_date.timestamp()),
                        )
                    )

                    # Event 2: "Price < TargetLow by end_date"
                    title_low = f"{data['name']} Price < ${target_low} by {end_date.strftime('%b %Y')}"
                    if current_price <= target_low:
                        yes_p_low = 0.9 
                    else:
                        # Scale based on difference, lower if far above
                        distance_ratio = (target_low - current_price) / target_low
                        yes_p_low = max(0.01, min(0.99, 0.5 + distance_ratio * 2))
                        yes_p_low = round(yes_p_low * 0.5 + 0.05, 2) # Make it generally low

                    yes_p_low = max(0.01, min(0.99, yes_p_low))
                    no_p_low = 1.0 - yes_p_low
                    events.append(
                        NormalizedEvent(
                            event_id=f"commodities_{ticker_symbol}_low_{target_low}_{end_date.year}{end_date.month}",
                            title=title_low,
                            platform=self.platform_name,
                            url=f"https://finance.yahoo.com/quote/{ticker_symbol}",
                            yes_price=yes_p_low,
                            no_price=no_p_low,
                            expiry=int(end_date.timestamp()),
                        )
                    )

            except Exception as e:
                logger.error(f"Error fetching data for {data['name']} ({ticker_symbol}): {e}")
        return events
