"""Event-driven backtest of the EMA-bounce strategy.

For each symbol the signal gating is computed *vectorised* (identical logic to
``src.signals.analysis.analyze``), then trades are simulated bar-by-bar:

    - Signal fires on the close of bar i (decision bar).
    - Entry at the OPEN of bar i+1 (no lookahead).
    - Stop  = entry - atr_stop_mult * ATR(at decision bar).
    - Target = entry + rr_target * (entry - stop).
    - Each later bar: a low<=stop is a loss (checked first, conservative),
      a high>=target is a win; gaps through a level exit at that bar's open.
    - Time-stop: exit at close after ``bt_max_hold`` bars if still open.
    - No pyramiding: a new signal is ignored while a trade is open.

Results are expressed in **R-multiples** (profit / initial risk), so they are
position-size independent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from src.config.settings import Settings
from src.signals.indicators import atr, ema


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry: float
    exit_date: str
    exit: float
    stop: float
    target: float
    bars_held: int
    outcome: str   # "win" | "loss" | "time"
    r: float       # realised R-multiple


def _signal_mask(df: pd.DataFrame, s: Settings) -> pd.Series:
    """Boolean per-bar mask: True where an EMA-bounce BUY signal fires.

    Four interchangeable setups are OR-ed: EMA20/EMA40, EMA40/EMA50, a bounce
    off EMA50 alone, and a bounce off EMA20 alone. Mirrors ``analysis.analyze``.
    (CCI is only a live-ranking input, not a trade rule, so it is not here.)
    """
    close, open_, low = df["close"], df["open"], df["low"]
    e20 = ema(close, s.ema_fast)
    e40 = ema(close, s.ema_slow)
    e50 = ema(close, s.ema_slowest)
    valid_atr = atr(df, s.atr_period) > 0
    green = close > open_

    def _bounce(fast_e, slow_e):
        uptrend = fast_e > slow_e
        rising = fast_e > fast_e.shift(s.ema20_rising_bars)
        touched = (low <= fast_e * (1 + s.pullback_proximity)) & \
                  (low <= slow_e * (1 + s.pullback_proximity))
        above = (close > fast_e) & (close > slow_e)
        held = (close >= fast_e).astype(int)
        shallow = held.shift(1).rolling(s.pullback_hold_bars).sum() == s.pullback_hold_bars
        return uptrend & rising & touched & above & shallow

    def _bounce_single(ema_e):
        rising = ema_e > ema_e.shift(s.ema20_rising_bars)
        touched = low <= ema_e * (1 + s.pullback_proximity)
        above = close > ema_e
        held = (close >= ema_e).astype(int)
        shallow = held.shift(1).rolling(s.pullback_hold_bars).sum() == s.pullback_hold_bars
        return rising & touched & above & shallow

    # General uptrend precondition (all setups): EMA20 > EMA40 > EMA50.
    stack = (e20 > e40) & (e40 > e50)
    fired = (_bounce(e20, e40) | _bounce(e40, e50)
             | _bounce_single(e50) | _bounce_single(e20))
    return fired & stack & green & valid_atr


def backtest_symbol(symbol: str, df: pd.DataFrame, s: Settings) -> list[Trade]:
    """Simulate all non-overlapping trades for one symbol."""
    n = len(df)
    warmup = max(s.ema_slowest, s.atr_period, s.ema20_rising_bars) + 2
    if n < warmup + 2:
        return []

    df = df.reset_index(drop=True)
    d = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    a = atr(df, s.atr_period)
    mask = _signal_mask(df, s).to_numpy()
    open_ = df["open"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    atr_v = a.to_numpy()

    trades: list[Trade] = []
    i = warmup
    while i < n - 1:  # need bar i+1 for the entry open
        if not mask[i]:
            i += 1
            continue

        entry = float(open_[i + 1])
        risk = s.atr_stop_mult * float(atr_v[i])
        if risk <= 0:
            i += 1
            continue
        stop = entry - risk
        target = entry + s.rr_target * risk

        exit_price = None
        outcome = ""
        exit_j = None
        last = min(i + 1 + s.bt_max_hold - 1, n - 1)
        for j in range(i + 1, last + 1):
            if open_[j] <= stop:               # gap down through stop
                exit_price, outcome, exit_j = float(open_[j]), "loss", j
                break
            if low[j] <= stop:                 # stop hit intrabar (conservative)
                exit_price, outcome, exit_j = stop, "loss", j
                break
            if open_[j] >= target:             # gap up through target
                exit_price, outcome, exit_j = float(open_[j]), "win", j
                break
            if high[j] >= target:              # target hit intrabar
                exit_price, outcome, exit_j = target, "win", j
                break
        if exit_price is None:                 # time-stop at close of last bar
            exit_j = last
            exit_price, outcome = float(close[last]), "time"

        r = (exit_price - entry) / risk
        trades.append(Trade(
            symbol=symbol,
            entry_date=d.iloc[i + 1],
            entry=round(entry, 4),
            exit_date=d.iloc[exit_j],
            exit=round(exit_price, 4),
            stop=round(stop, 4),
            target=round(target, 4),
            bars_held=exit_j - (i + 1),
            outcome=outcome,
            r=round(r, 4),
        ))
        i = exit_j + 1  # no overlapping trades on the same symbol

    return trades


def aggregate(trades: list[Trade], risk_per_trade: float) -> dict:
    """Compute summary statistics + an equity curve (in R and in %) ."""
    if not trades:
        return {"trades": 0}

    trades_sorted = sorted(trades, key=lambda t: t.exit_date)
    rs = [t.r for t in trades_sorted]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]

    cum, equity_curve = 0.0, []
    peak, max_dd = 0.0, 0.0
    for t in trades_sorted:
        cum += t.r
        equity_curve.append((t.exit_date, cum))
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)  # drawdown in R

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    n = len(rs)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100,
        "avg_r": sum(rs) / n,
        "avg_win_r": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_r": (-gross_loss / len(losses)) if losses else 0.0,
        "expectancy_r": sum(rs) / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "total_r": cum,
        "max_dd_r": max_dd,
        "avg_bars_held": sum(t.bars_held for t in trades_sorted) / n,
        "time_exits": sum(1 for t in trades_sorted if t.outcome == "time"),
        "equity_curve": equity_curve,
        # %-return view assuming `risk_per_trade` of equity risked per trade.
        "total_return_pct": cum * risk_per_trade * 100,
        "max_dd_pct": max_dd * risk_per_trade * 100,
        "r_values": rs,
    }


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame(asdict(t) for t in trades)
