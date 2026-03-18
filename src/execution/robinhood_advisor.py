"""Robinhood advisor — stock price fetch + recommendations. NO execution. Not a BaseExecutor."""
import logging
logger = logging.getLogger("execution.robinhood")


class RobinhoodAdvisor:
    async def get_current_price(self, symbol: str) -> float:
        try:
            import asyncio, yfinance as yf
            t = await asyncio.get_running_loop().run_in_executor(None, lambda: yf.Ticker(symbol.upper()))
            return float(t.fast_info.last_price or 0)
        except Exception as e:
            logger.warning("Stock price failed for %s: %s", symbol, e); return 0.0

    def recommend(self, symbol: str, action: str, quantity: float, reason: str) -> dict:
        return {"type":"stock_advisory","symbol":symbol.upper(),"action":action,
                "quantity":quantity,"reason":reason,"manual_execution_required":True}
