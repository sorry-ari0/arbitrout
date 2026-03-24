import logging
import uuid
import time
import re
from typing import List, Dict, Any, Optional

from adapters.registry import AdapterRegistry

logger = logging.getLogger("crypto_hedger")

class HedgePackage:
    """Represents a potential hedged position combining crypto spot and a prediction market NO contract."""
    def __init__(self,
                 package_id: str,
                 crypto_symbol: str,
                 strike_price: float,
                 spot_price: float,
                 pm_market: Dict[str, Any], # Prediction market details
                 fees: float = 0.005 # Example combined fees for spot and PM
                 ):
        self.package_id = package_id
        self.crypto_symbol = crypto_symbol
        self.strike_price = strike_price
        self.spot_price = spot_price
        self.pm_market = pm_market
        self.fees = fees
        self.last_updated = time.time()

        self.pm_event_id = pm_market.get('event_id', 'unknown')
        self.pm_platform = pm_market.get('platform', 'unknown')
        self.pm_title = pm_market.get('canonical_title', pm_market.get('title', 'unknown'))
        self.pm_buy_no_price = pm_market.get('no_price', 0.0)
        self.pm_buy_yes_price = pm_market.get('yes_price', 0.0)

        # Calculate implied probabilities from prediction market
        self.pm_implied_prob_above_strike = self.pm_buy_yes_price # Probability of YES
        self.pm_implied_prob_below_strike = self.pm_buy_no_price  # Probability of NO

        self._calculate_package_metrics()

    def _calculate_package_metrics(self):
        # We assume a notional of 1 unit of spot crypto is bought, and 1 unit of a $1-payout NO contract.
        # Initial cost = self.spot_price (for 1 unit of crypto) + self.pm_buy_no_price (for 1 NO contract)
        capital_needed = self.spot_price + self.pm_buy_no_price

        # Scenario 1: Crypto price finishes *at* strike or below (PM NO wins)
        # Assuming final spot price is at the strike for calculation simplicity in this scenario
        self.scenario_no_wins = {
            "condition": f"{self.crypto_symbol} \u2264 ${self.strike_price:,.2f} (PM NO wins)",
            "spot_pnl": self.strike_price - self.spot_price,
            "pm_pnl": 1.0 - self.pm_buy_no_price, # NO contract pays $1
            "capital_needed": capital_needed
        }
        self.scenario_no_wins["gross_profit"] = self.scenario_no_wins["spot_pnl"] + self.scenario_no_wins["pm_pnl"]
        self.scenario_no_wins["net_profit"] = self.scenario_no_wins["gross_profit"] - capital_needed * self.fees
        self.scenario_no_wins["return_pct"] = (self.scenario_no_wins["net_profit"] / capital_needed) * 100 if capital_needed > 0 else 0

        # Scenario 2: Crypto price finishes *above* strike (PM YES wins)
        # Assuming final spot price is at strike + 10% for illustrative purposes
        illustrative_final_price_above_strike = self.strike_price * 1.1 if self.strike_price > 0 else self.spot_price * 1.05
        self.scenario_yes_wins = {
            "condition": f"{self.crypto_symbol} > ${self.strike_price:,.2f} (PM YES wins)",
            "spot_pnl": illustrative_final_price_above_strike - self.spot_price,
            "pm_pnl": -self.pm_buy_no_price, # Lose the premium paid for NO
            "capital_needed": capital_needed
        }
        self.scenario_yes_wins["gross_profit"] = self.scenario_yes_wins["spot_pnl"] + self.scenario_yes_wins["pm_pnl"]
        self.scenario_yes_wins["net_profit"] = self.scenario_yes_wins["gross_profit"] - capital_needed * self.fees
        self.scenario_yes_wins["return_pct"] = (self.scenario_yes_wins["net_profit"] / capital_needed) * 100 if capital_needed > 0 else 0

        # Determine overall "type" of profit if any (for display)
        self.overall_profit_type = "uncertain"
        if self.scenario_no_wins["net_profit"] > 0 and self.scenario_yes_wins["net_profit"] > 0:
            self.overall_profit_type = "guaranteed"
        elif self.scenario_no_wins["net_profit"] > 0 or self.scenario_yes_wins["net_profit"] > 0:
            self.overall_profit_type = "conditional" # At least one scenario profitable

        self.max_profit = max(self.scenario_no_wins["net_profit"], self.scenario_yes_wins["net_profit"])
        self.max_loss = min(self.scenario_no_wins["net_profit"], self.scenario_yes_wins["net_profit"])
        
        # Simplified breakeven price: the spot price where the package P&L (if NO wins) is zero.
        # This occurs if final_price - spot_price + (1 - pm_buy_no_price) - fees*capital_needed = 0
        # final_price = spot_price - (1 - pm_buy_no_price) + fees*capital_needed
        self.breakeven_price = self.spot_price - (1.0 - self.pm_buy_no_price) + capital_needed * self.fees


    def to_dict(self):
        return {
            "package_id": self.package_id,
            "crypto_symbol": self.crypto_symbol,
            "strike_price": self.strike_price,
            "spot_price": self.spot_price,
            "pm_event_id": self.pm_event_id,
            "pm_platform": self.pm_platform,
            "pm_title": self.pm_title,
            "pm_buy_no_price": self.pm_buy_no_price,
            "last_updated": self.last_updated,
            "overall_profit_type": self.overall_profit_type,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss,
            "breakeven_price": self.breakeven_price,
            "scenarios": {
                "no_wins": self.scenario_no_wins,
                "yes_wins": self.scenario_yes_wins,
            }
        }

class CryptoHedger:
    def __init__(self, registry: AdapterRegistry):
        self.registry = registry
        # A simple map for known crypto symbols and their associated PM platforms/patterns
        # In a real system, this would be more dynamic
        self.crypto_pm_patterns = {
            "BTC": {
                "platforms": ["kalshi", "polymarket"], # Example platforms
                "keywords": ["bitcoin", "btc"],
                "strike_regex": r"btc > \$?([\d,]+\.?\d*)" # Matches "BTC > $30,000" or "Bitcoin > 30,000"
            },
            # Add more crypto assets here
        }

    async def find_hedge_packages(self) -> List[Dict[str, Any]]:
        """
        Scans for potential crypto hedge packages by combining spot prices with
        prediction market 'NO' contracts for 'Crypto > $X' events.
        """
        hedge_packages: List[HedgePackage] = []

        for symbol, config in self.crypto_pm_patterns.items():
            # 1. Get current spot price for the crypto
            # For this simple implementation, let's mock the spot price.
            # In a real system: `spot_price = await self.registry.get_crypto_spot_price(symbol)`
            
            if symbol == "BTC":
                spot_price = 30000.0  # Example current BTC spot price
            else:
                spot_price = 1000.0 # Placeholder for others
            logger.debug(f"MOCK: Current spot price for {symbol}: ${spot_price}")

            if spot_price <= 0:
                logger.warning(f"Could not get valid spot price for {symbol}, skipping.")
                continue

            # 2. Find relevant prediction markets for "Crypto > $X"
            all_events = self.registry.get_all_events()
            
            matching_pm_events = []
            for event in all_events:
                event_title_lower = event.get('canonical_title', event.get('title', '')).lower()
                event_platform = event.get('platform', '').lower()

                # Basic keyword and platform filter
                if any(kw in event_title_lower for kw in config["keywords"]) and \
                   event_platform in config["platforms"]:
                    # Try to parse strike price from title
                    match = re.search(config["strike_regex"], event_title_lower)
                    if match:
                        try:
                            strike_str = match.group(1).replace(',', '')
                            strike_price = float(strike_str)
                            # Ensure prediction market has 'no_price' and 'yes_price' data
                            if event.get('no_price') is not None and event.get('yes_price') is not None:
                                event_with_strike = event.copy()
                                event_with_strike['parsed_strike_price'] = strike_price
                                matching_pm_events.append(event_with_strike)
                        except ValueError:
                            logger.debug(f"Could not parse strike price from: {event_title_lower}")

            # 3. Create HedgePackages from matching events
            if not matching_pm_events:
                logger.debug(f"No matching prediction markets found for {symbol}.")
                continue

            for pm_event in matching_pm_events:
                pm_strike_price = pm_event['parsed_strike_price']

                # Filter out irrelevant strikes (e.g., strike too far from spot)
                if spot_price > 0 and abs(pm_strike_price - spot_price) / spot_price > 0.5: # e.g., strike not within 50% of spot
                    logger.debug(f"Skipping {pm_event.get('title')} - strike {pm_strike_price} too far from spot {spot_price}")
                    continue

                package_id = f"hedge_{symbol}_{pm_event['event_id']}_{uuid.uuid4().hex[:6]}"
                
                # Check for valid prices before creating package
                if pm_event.get('no_price', 0.0) <= 0 or pm_event.get('yes_price', 0.0) <= 0:
                    logger.debug(f"Skipping {pm_event.get('title')} - invalid prediction market prices (no_price={pm_event.get('no_price')}, yes_price={pm_event.get('yes_price')}).")
                    continue

                try:
                    hp = HedgePackage(
                        package_id=package_id,
                        crypto_symbol=symbol,
                        strike_price=pm_strike_price,
                        spot_price=spot_price,
                        pm_market=pm_event
                    )
                    hedge_packages.append(hp)
                except Exception as e:
                    logger.error(f"Error creating HedgePackage for {symbol}/{pm_event.get('title')}: {e}")

        # Sort packages by max_profit for display
        hedge_packages.sort(key=lambda x: x.max_profit, reverse=True)

        return [hp.to_dict() for hp in hedge_packages]

