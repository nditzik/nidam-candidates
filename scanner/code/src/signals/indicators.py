"""Technical indicators used by the dashboard analysis.

Pure pandas; no IBKR coupling so these are trivially unit-testable.
Expected input is a daily-bar DataFrame with columns: open, high, low, close.
"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average (standard, recursive — adjust=False)."""
    return series.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder smoothing == EWM with alpha = 1/period.
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def cci(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """Commodity Channel Index over ``period`` bars.

    CCI = (TP - SMA(TP)) / (0.015 * mean absolute deviation of TP),
    where TP (typical price) = (high + low + close) / 3.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: abs(x - x.mean()).mean(), raw=True)
    return (tp - sma) / (0.015 * mad)


def crossed_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """Boolean series: True on bars where ``fast`` crosses up through ``slow``."""
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def bars_since_last_true(mask: pd.Series) -> int | None:
    """Number of bars since the last True in ``mask`` (0 = on the last bar).

    Returns None if there is no True value in the series.
    """
    true_positions = mask.to_numpy().nonzero()[0]
    if len(true_positions) == 0:
        return None
    return int(len(mask) - 1 - true_positions[-1])
