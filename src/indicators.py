"""Technical indicators for the Lobsterminal."""
import numpy as np


def rsi(prices: list[float] | np.ndarray, period: int = 14) -> list[float]:
    """Calculate RSI using Wilder's smoothing method.

    Returns a list of RSI values (NaN for first `period` entries).
    """
    prices = np.asarray(prices, dtype=float)
    if len(prices) < period + 1:
        return [float("nan")] * len(prices)

    delta = np.diff(prices)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    # Wilder's smoothing: first value is SMA, rest use EMA
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])

    rsi_values = [float("nan")] * period
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(round(100.0 - (100.0 / (1.0 + rs)), 2))

    return rsi_values


def macd(
    prices: list[float] | np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float]]:
    """Calculate MACD line, signal line, and histogram.

    Returns dict with keys 'macd', 'signal', 'histogram'.
    """
    prices = np.asarray(prices, dtype=float)
    if len(prices) < slow + signal:
        n = len(prices)
        return {"macd": [float("nan")] * n, "signal": [float("nan")] * n, "histogram": [float("nan")] * n}

    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line

    return {
        "macd": [round(v, 4) for v in macd_line.tolist()],
        "signal": [round(v, 4) for v in signal_line.tolist()],
        "histogram": [round(v, 4) for v in histogram.tolist()],
    }


def _ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    alpha = 2.0 / (period + 1)
    result = np.empty_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result
