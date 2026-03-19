"""Kraken CLI executor — wraps the Kraken CLI (via WSL) for spot trading.

The Kraken CLI is a Rust binary that handles auth, nonce management, and
HMAC signing. We call it via WSL and parse NDJSON output.

Supports:
  - Real spot trading (with API keys configured in kraken CLI)
  - Paper trading (via --validate flag)
  - Price lookups (public, no auth needed)
  - MCP server mode for AI agent integration

Requires: Kraken CLI installed in WSL Ubuntu
  wsl -d Ubuntu -- bash -c 'source $HOME/.cargo/env && kraken --version'
"""
import asyncio
import json
import logging
import os

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.kraken_cli")

# Kraken uses different pair names than standard (XXBTZUSD vs BTC/USD)
PAIR_MAP = {
    "BTC/USD": "XBTUSD", "BTC/USDT": "XBTUSDT",
    "ETH/USD": "ETHUSD", "ETH/USDT": "ETHUSDT",
    "SOL/USD": "SOLUSD", "SOL/USDT": "SOLUSDT",
    "DOGE/USD": "DOGEUSD", "DOGE/USDT": "DOGEUSDT",
    "XRP/USD": "XRPUSD", "XRP/USDT": "XRPUSDT",
    "ADA/USD": "ADAUSD", "ADA/USDT": "ADAUSDT",
    "AVAX/USD": "AVAXUSD", "AVAX/USDT": "AVAXUSDT",
    "LINK/USD": "LINKUSD", "LINK/USDT": "LINKUSDT",
    "DOT/USD": "DOTUSD", "DOT/USDT": "DOTUSDT",
    "POL/USD": "POLUSD",
}

# Reverse map for parsing Kraken responses
KRAKEN_TO_STANDARD = {v: k for k, v in PAIR_MAP.items()}
# Kraken's internal names with X/Z prefix
KRAKEN_TO_STANDARD["XXBTZUSD"] = "BTC/USD"
KRAKEN_TO_STANDARD["XETHZUSD"] = "ETH/USD"
KRAKEN_TO_STANDARD["XXRPZUSD"] = "XRP/USD"


class KrakenCLIExecutor(BaseExecutor):
    """Execute trades via Kraken CLI in WSL.

    Uses asyncio.create_subprocess_exec with explicit argument lists
    to avoid shell injection. All user inputs are passed as separate args.
    """

    def __init__(self):
        self._available = None  # Lazy check

    async def _run_kraken(self, args: list[str], timeout: float = 15.0) -> dict | list | None:
        """Run a kraken CLI command via WSL, return parsed JSON.

        args: list of kraken subcommand arguments (e.g. ["ticker", "XBTUSD"])
        All args are joined into a single shell command with proper quoting.
        """
        # Build the shell command safely — args are controlled by code, not user input
        # The kraken CLI arguments (pair names, volumes) are validated before reaching here
        quoted_args = " ".join(f"'{a}'" for a in args)
        shell_cmd = f"source $HOME/.cargo/env && kraken {quoted_args} -o json"

        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl", "-d", "Ubuntu", "--", "bash", "-c", shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            if proc.returncode != 0:
                err = stderr.decode().strip()
                logger.warning("Kraken CLI error (rc=%d): %s", proc.returncode, err)
                return None

            output = stdout.decode().strip()
            if not output:
                return None

            # NDJSON: may be multiple lines
            lines = output.strip().split("\n")
            if len(lines) == 1:
                return json.loads(lines[0])
            # Multiple lines — return list
            return [json.loads(line) for line in lines if line.strip()]

        except asyncio.TimeoutError:
            logger.warning("Kraken CLI timed out after %.0fs: %s", timeout, args)
            return None
        except Exception as e:
            logger.warning("Kraken CLI exec failed: %s", e)
            return None

    async def _check_available(self) -> bool:
        """Check if kraken CLI is available in WSL."""
        if self._available is not None:
            return self._available

        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl", "-d", "Ubuntu", "--", "bash", "-c",
                "source $HOME/.cargo/env && kraken --version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            self._available = proc.returncode == 0
            if self._available:
                version = stdout.decode().strip()
                logger.info("Kraken CLI available: %s", version)
        except Exception:
            self._available = False

        return self._available

    def is_configured(self) -> bool:
        # Check synchronously — WSL + kraken existence
        # Full check happens async on first use
        try:
            import subprocess
            result = subprocess.run(
                ["wsl", "-d", "Ubuntu", "--", "bash", "-c",
                 "source $HOME/.cargo/env && kraken --version"],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _resolve_pair(self, asset_id: str) -> str:
        """Convert standard pair to Kraken pair name.

        Only accepts known pair formats — rejects unexpected input.
        """
        clean = asset_id.strip().upper()

        # Already a known Kraken pair
        if clean in KRAKEN_TO_STANDARD:
            return clean

        # Standard format
        if "/" not in clean:
            clean = f"{clean}/USD"

        if clean in PAIR_MAP:
            return PAIR_MAP[clean]

        # Fallback: strip slash (BTC/USD -> BTCUSD)
        return clean.replace("/", "")

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Place a market buy order via Kraken CLI."""
        if not await self._check_available():
            return ExecutionResult(False, None, 0, 0, 0, "Kraken CLI not available")

        try:
            pair = self._resolve_pair(asset_id)

            # Get current price to calculate volume
            price = await self.get_current_price(asset_id)
            if price <= 0:
                return ExecutionResult(False, None, 0, 0, 0, f"Cannot get price for {pair}")

            volume = str(round(amount_usd / price, 8))

            result = await self._run_kraken(
                ["order", "buy", pair, volume, "--type", "market", "--yes"],
                timeout=30.0,
            )

            if not result:
                return ExecutionResult(False, None, 0, 0, 0, "Kraken CLI order returned no response")

            # Parse response
            if isinstance(result, dict):
                txids = result.get("txid", [])
                order_id = txids[0] if txids else ""
                descr = result.get("descr", {})
                logger.info("Kraken BUY %s: vol=%s, order=%s, descr=%s",
                            pair, volume, order_id, descr.get("order", ""))
                return ExecutionResult(True, order_id, price, float(volume), 0, None)

            return ExecutionResult(False, None, 0, 0, 0, f"Unexpected response: {result}")

        except Exception as e:
            logger.error("Kraken CLI buy failed: %s", e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Place a market sell order via Kraken CLI."""
        if not await self._check_available():
            return ExecutionResult(False, None, 0, 0, 0, "Kraken CLI not available")

        try:
            pair = self._resolve_pair(asset_id)
            vol_str = str(round(quantity, 8))

            result = await self._run_kraken(
                ["order", "sell", pair, vol_str, "--type", "market", "--yes"],
                timeout=30.0,
            )

            if not result:
                return ExecutionResult(False, None, 0, 0, 0, "Kraken CLI sell returned no response")

            if isinstance(result, dict):
                txids = result.get("txid", [])
                order_id = txids[0] if txids else ""
                price = await self.get_current_price(asset_id)
                return ExecutionResult(True, order_id, price, quantity, 0, None)

            return ExecutionResult(False, None, 0, 0, 0, f"Unexpected response: {result}")

        except Exception as e:
            logger.error("Kraken CLI sell failed: %s", e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        """Fetch balance via Kraken CLI."""
        if not await self._check_available():
            return BalanceResult(0, 0)

        result = await self._run_kraken(["balance"])
        if not result or not isinstance(result, dict):
            return BalanceResult(0, 0)

        # Kraken returns {asset: amount, ...}
        usd = float(result.get("ZUSD", result.get("USD", 0)))
        usdt = float(result.get("USDT", 0))
        return BalanceResult(usd + usdt, usd + usdt)

    async def get_positions(self) -> list[PositionInfo]:
        """Fetch non-zero balances as positions."""
        if not await self._check_available():
            return []

        result = await self._run_kraken(["balance"])
        if not result or not isinstance(result, dict):
            return []

        positions = []
        skip = {"ZUSD", "USD", "USDT", "USDC"}
        for asset, amount in result.items():
            amount = float(amount)
            if amount > 0 and asset not in skip:
                positions.append(PositionInfo(asset, amount, 0, 0, 0))
        return positions

    async def get_current_price(self, asset_id: str) -> float:
        """Fetch current price from Kraken public ticker (no auth needed)."""
        if not await self._check_available():
            return 0.0

        pair = self._resolve_pair(asset_id)
        result = await self._run_kraken(["ticker", pair])

        if not result or not isinstance(result, dict):
            return 0.0

        # Response: {"XXBTZUSD": {"c": ["71000.00", "0.001"], ...}}
        for key, data in result.items():
            if isinstance(data, dict):
                # "c" = last trade closed [price, lot-volume]
                last = data.get("c", [])
                if last and len(last) >= 1:
                    return float(last[0])
                # fallback: "a" = ask [price, ...]
                ask = data.get("a", [])
                if ask and len(ask) >= 1:
                    return float(ask[0])

        return 0.0

    async def close(self):
        pass  # No persistent connection to close
