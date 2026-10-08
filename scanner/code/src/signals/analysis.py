"""Per-symbol swing analysis: EMA-bounce entry + trade levels.

Long-only. A general-uptrend precondition applies to all setups: the EMA stack
must be ordered bullishly (EMA20 > EMA40 > EMA50). Then four interchangeable
bounce setups of varying pullback depth: (A) EMA20/EMA40, (B) EMA40/EMA50,
(C) EMA50 alone, and (D) EMA20 alone. A/B fire when, in an uptrend (fast EMA >
slow EMA and the fast EMA rising), the *latest* candle is green, its low touched
the fast/slow zone, it closed back above both, and the pullback into it was
shallow (prior bars closed above the fast EMA). C is the deepest (low touched
the rising EMA50, closed back above it, possibly still below EMA40); D is the
shallowest (low only reached the EMA20 and closed back above it, never dipping
to the 40). A symbol is a BUY if any setup fires. CCI and volume are
computed too but only as ranking inputs (oversold-reversal / conviction bonus),
not gates. Trade levels:

    entry  = last close
    stop   = entry - atr_stop_mult * ATR(atr_period)
    target = entry + rr_target * (entry - stop)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from src.config.settings import Settings
from src.signals.indicators import atr, bars_since_last_true, cci, crossed_above, ema


@dataclass
class Analysis:
    symbol: str
    has_signal: bool
    reason: str = ""           # why there is no signal (when has_signal is False)
    setup: str = ""            # which EMA pair fired: "EMA20/40" or "EMA40/50"

    # Snapshot
    close: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    atr: float = 0.0
    days_since_cross: int | None = None
    dist_ema20_pct: float = 0.0   # close distance above EMA20 (%)
    pullback_days_ago: int | None = None  # bars since the EMA20 touch
    vol: float = 0.0              # volume of the signal (bounce) candle
    avg_vol: float = 0.0         # average volume over the prior vol_avg_period bars
    vol_ratio: float = 0.0       # vol / avg_vol  (>1 = above-average conviction)
    cci: float = 0.0             # CCI value on the signal candle
    cci_score: float = 0.0       # oversold-reversal ranking score (0 = none)

    # Trade plan (only meaningful when has_signal)
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    risk_per_share: float = 0.0
    risk_pct: float = 0.0       # risk as % of entry
    reward_per_share: float = 0.0
    rr: float = 0.0

    def to_row(self) -> dict:
        return asdict(self)


def _min_bars(s: Settings) -> int:
    # Need enough history for the slowest EMA + ATR to stabilise.
    return max(s.ema_slowest, s.atr_period) + 5


def analyze(symbol: str, df: pd.DataFrame, settings: Settings,
            force_levels: bool = False) -> Analysis:
    """Analyze one symbol's daily bars and return an :class:`Analysis`.

    ``force_levels=True`` is the RE-PRICE path: skip the fresh-signal gates
    (uptrend stack + bounce) and just recompute entry/stop/target on today's close
    with the same model (entry=close, stop=entry-1.5*ATR, target=entry+rr*risk).
    Used to keep a setup identified on a prior close actionable at CURRENT levels —
    the signal is a one-bar event, so a fresh scan would drop it. Backtest
    (entry_delay): a 1-day-late entry with re-priced levels keeps ~90% of the edge.
    A valid ATR is still required (can't price without it)."""
    if df is None or len(df) < _min_bars(settings):
        return Analysis(symbol, False, reason="insufficient history")

    df = df.reset_index(drop=True)
    fast = ema(df["close"], settings.ema_fast)        # EMA20
    slow = ema(df["close"], settings.ema_slow)        # EMA40
    slowest = ema(df["close"], settings.ema_slowest)  # EMA50
    atr_series = atr(df, settings.atr_period)

    close = float(df["close"].iloc[-1])
    ema_fast_v = float(fast.iloc[-1])
    ema_slow_v = float(slow.iloc[-1])
    atr_v = float(atr_series.iloc[-1])

    days_since = bars_since_last_true(crossed_above(fast, slow))
    dist_ema20_pct = (close / ema_fast_v - 1.0) * 100 if ema_fast_v else 0.0

    # Volume confirmation: the signal candle's volume vs the prior-N-bar average.
    vol_v = avg_vol_v = vol_ratio_v = 0.0
    if "volume" in df.columns:
        n_vol = settings.vol_avg_period
        vol_v = float(df["volume"].iloc[-1])
        if len(df) > n_vol:
            avg_vol_v = float(df["volume"].iloc[-(n_vol + 1):-1].mean())
        vol_ratio_v = (vol_v / avg_vol_v) if avg_vol_v > 0 else 0.0

    # CCI oversold-reversal score (ranking input, not a gate): reward a dip
    # toward the oversold level that is now turning back up.
    cci_series = cci(df, settings.cci_period)
    cci_now = float(cci_series.iloc[-1]) if pd.notna(cci_series.iloc[-1]) else 0.0
    cci_prev = float(cci_series.iloc[-2]) if (len(cci_series) > 1 and pd.notna(cci_series.iloc[-2])) else cci_now
    recent_min = float(cci_series.iloc[-settings.cci_lookback:].min())
    cci_score_v = 0.0
    # No bonus once CCI is already overbought (the reversal is "done", price extended).
    if (cci_now > cci_prev and cci_now < settings.cci_overbought
            and settings.cci_oversold and recent_min == recent_min):
        cci_score_v = min(2.0, max(0.0, recent_min / settings.cci_oversold))

    base = Analysis(
        symbol=symbol,
        has_signal=False,
        close=close,
        ema_fast=ema_fast_v,
        ema_slow=ema_slow_v,
        atr=atr_v,
        days_since_cross=days_since,
        dist_ema20_pct=dist_ema20_pct,
        vol=vol_v,
        avg_vol=avg_vol_v,
        vol_ratio=vol_ratio_v,
        cci=cci_now,
        cci_score=cci_score_v,
    )

    # --- Pullback-to-EMA gating (long-only) ---
    # Four interchangeable entry setups (varying pullback depth):
    #   A) EMA20/EMA40 bounce  (low touched both, closed above both)
    #   B) EMA40/EMA50 bounce  (low touched both, closed above both)
    #   C) EMA50 bounce        (low touched the 50, closed above it — may still
    #                           be below EMA40: an early recovery off the 50)
    #   D) EMA20 bounce        (shallowest: low only reached the 20, closed
    #                           above it — never dipped to the 40)
    # A symbol is a BUY if ANY setup fires. Volume and CCI apply to all
    # (they are ranking inputs computed once, above).
    open_v = float(df["open"].iloc[-1])
    low_v = float(df["low"].iloc[-1])
    prox = settings.pullback_proximity
    hold = settings.pullback_hold_bars
    look = settings.ema20_rising_bars

    def _bounce(fast_s, slow_s, fname, sname):
        """Check the EMA-bounce gates for one (fast, slow) EMA pair."""
        fv, sv = float(fast_s.iloc[-1]), float(slow_s.iloc[-1])
        if not (fv > sv):
            return False, f"not in uptrend ({fname} <= {sname})"
        if len(fast_s) > look and fast_s.iloc[-1] <= fast_s.iloc[-1 - look]:
            return False, f"{fname} not rising"
        if close <= open_v:
            return False, "latest candle not green"
        if not (low_v <= fv * (1.0 + prox) and low_v <= sv * (1.0 + prox)):
            return False, f"candle did not touch {fname} & {sname}"
        if close <= fv:
            return False, f"close below {fname}"
        if close <= sv:
            return False, f"close below {sname}"
        if hold > 0 and len(df) > hold:
            pc = df["close"].iloc[-(hold + 1):-1].to_numpy()
            pe = fast_s.iloc[-(hold + 1):-1].to_numpy()
            if (pc < pe).any():
                return False, f"pullback closed below {fname} (not a shallow pullback)"
        return True, f"green candle bounced off {fname} & {sname}"

    def _bounce_single(ema_s, ename):
        """Bounce off a single rising EMA: low touched it, closed above it,
        green candle, and the prior bars held above it on a closing basis."""
        ev = float(ema_s.iloc[-1])
        if len(ema_s) > look and ema_s.iloc[-1] <= ema_s.iloc[-1 - look]:
            return False, f"{ename} not rising"
        if close <= open_v:
            return False, "latest candle not green"
        if not (low_v <= ev * (1.0 + prox)):
            return False, f"candle did not touch {ename}"
        if close <= ev:
            return False, f"close below {ename}"
        if hold > 0 and len(df) > hold:
            pc = df["close"].iloc[-(hold + 1):-1].to_numpy()
            pe = ema_s.iloc[-(hold + 1):-1].to_numpy()
            if (pc < pe).any():
                return False, f"pullback closed below {ename} (not held)"
        return True, f"green candle bounced off {ename}"

    # General uptrend precondition (both setups): the EMA stack is ordered
    # bullishly, EMA20 > EMA40 > EMA50.
    if not (float(fast.iloc[-1]) > float(slow.iloc[-1]) > float(slowest.iloc[-1])):
        if not force_levels:
            base.reason = "not in general uptrend (need EMA20 > EMA40 > EMA50)"
            return base

    ok, reason = _bounce(fast, slow, "EMA20", "EMA40")
    setup = fsel = ssel = None
    if ok:
        setup, fsel, ssel = "EMA20/40", fast, slow
    if not ok:
        ok_b, reason_b = _bounce(slow, slowest, "EMA40", "EMA50")
        if ok_b:
            ok, reason, setup, fsel, ssel = True, reason_b, "EMA40/50", slow, slowest
    if not ok:
        ok_c, reason_c = _bounce_single(slowest, "EMA50")
        if ok_c:
            ok, reason, setup, fsel, ssel = True, reason_c, "EMA50", slowest, slowest
    if not ok:
        # Shallowest: a pullback that only reached the EMA20 (never the 40).
        # Tried last so deeper touches keep their more specific label.
        ok_d, reason_d = _bounce_single(fast, "EMA20")
        if ok_d:
            ok, reason, setup, fsel, ssel = True, reason_d, "EMA20", fast, fast

    if not ok:
        if not force_levels:
            base.reason = reason  # the EMA20/40 (primary) failure reason
            return base
        # Re-price: no fresh bounce today — keep the name, price off today's close.
        setup, fsel, ssel, reason = None, fast, slow, "repriced (no fresh signal)"
    if atr_v <= 0:
        base.reason = "invalid ATR"
        return base

    # Reflect the EMA pair that actually fired (for display + distance).
    base.setup = setup
    base.ema_fast = float(fsel.iloc[-1])
    base.ema_slow = float(ssel.iloc[-1])
    base.dist_ema20_pct = (close / base.ema_fast - 1.0) * 100 if base.ema_fast else 0.0
    base.pullback_days_ago = 0  # the bounce is the current bar

    entry = close
    stop = entry - settings.atr_stop_mult * atr_v
    risk_per_share = entry - stop
    target = entry + settings.rr_target * risk_per_share
    reward_per_share = target - entry

    base.has_signal = True
    base.reason = reason
    base.entry = entry
    base.stop = stop
    base.target = target
    base.risk_per_share = risk_per_share
    base.risk_pct = (risk_per_share / entry * 100) if entry else 0.0
    base.reward_per_share = reward_per_share
    base.rr = (reward_per_share / risk_per_share) if risk_per_share else 0.0
    return base
