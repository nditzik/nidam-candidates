"""Default configuration parameters for the IBKR Swing System.

Phase 0 keeps configuration as a plain Python module (no YAML/extra deps).
Values here are the documented defaults; later phases may layer overrides on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Project layout anchors -----------------------------------------------------
# settings.py lives at <root>/src/config/settings.py -> parents[2] == <root>.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
WATCHLISTS_DIR = DATA_DIR / "watchlists"  # drop .csv files here; all are scanned
REPORTS_DIR = PROJECT_ROOT / "reports"
HISTORY_DIR = REPORTS_DIR / "history"     # daily candidate snapshots for diffing
WATCHLIST_PATH = CONFIG_DIR / "watchlist.txt"

# [REDACTED for the public copy] broker connection guard (port numbers removed).
LIVE_PORTS = frozenset()


@dataclass(frozen=True)
class Settings:
    """Immutable bundle of default runtime parameters."""

    # Risk / sizing
    risk_per_trade: float = 0.01  # legacy risk-fraction (kept for backtest % views)
    # Equal-weight sizing: split a fixed capital base into N equal $ slices, so
    # many small trades fit (more data, less leverage) and the R-multiple — which
    # is size-independent — is unaffected. Position $ = sizing_capital / target_positions.
    sizing_capital: float = 100_000.0  # [REDACTED placeholder] dashboard share-count only; NOT used by candidates.json
    target_positions: int = 10         # equal slices -> ~$40k per position
    max_risk_pct: float = 8.0          # skip candidates whose stop is wider than this % of price
    # "System Return %" = cumulative $ P&L / strategy_capital, counting ONLY trades
    # entered on/after strategy_start (the new equal-weight model), so the old
    # oversized trades don't distort it. Decoupled from sizing_capital on purpose.
    strategy_capital: float = 100_000.0  # [REDACTED placeholder] not used by the scan
    strategy_start: str = "2026-07-10"  # first day of the new sizing model (anchor)

    # Data
    timeframe: str = "1 day"

    # [REDACTED for the public copy] broker connection details (host / port / client id).
    ibkr_host: str = "REDACTED"
    ibkr_port: int = 0
    client_id: int = 0

    # --- Signal / analysis parameters (Phase 1-lite dashboard) ---
    ema_fast: int = 20
    ema_slow: int = 40
    ema_slowest: int = 50   # for the secondary EMA40/50 bounce setup
    atr_period: int = 14
    # stop = entry - atr_stop_mult * ATR.
    # 2026-07-05 this was tightened 1.5 -> 1.2 on a partial backtest read. That was a
    # MISTAKE: a full 2Y run (574 symbols, scripts/stop_mult_backtest.py) shows every
    # metric improves monotonically as the stop widens —
    #   1.2: 44.1% win, +0.099R, +0.462%/trade, R/DD 2.38
    #   1.5: 45.2% win, +0.122R, +0.752%/trade, R/DD 2.42   <- reverted here 2026-07-17
    #   2.0: 48.5% win, +0.175R, +1.396%/trade, R/DD 2.77
    # i.e. 1.2 cost ~39% of the per-trade return vs 1.5. Not going past 1.5: the trend
    # runs monotonically to 2.5+, which smells like "no stop at all" (trades exit on the
    # 40-bar time-stop instead) and is regime-dependent on an in-sample 2Y bull run.
    atr_stop_mult: float = 1.5
    rr_target: float = 1.5      # target = entry + rr_target * (entry - stop)
    vol_avg_period: int = 20    # bars for the average-volume baseline (volume confirmation)
    vol_rank_weight: float = 1.0  # R-equivalent bonus per 1.0 of (RVOL - 1) in ranking
    signal_lookback: int = 5    # bars: a crossover counts as "fresh" within this many days

    # Pullback-to-EMA entry (the primary signal for momentum/trending names):
    # uptrend that recently dipped to EMA20 and reclaimed it.
    pullback_lookback: int = 7        # bars to look back for a touch of EMA20
    pullback_proximity: float = 0.02  # "touch" = bar low within 2% of EMA20
    ema20_rising_bars: int = 5        # EMA20 must be higher than this many bars ago
    pullback_hold_bars: int = 3       # bars before the bounce must close >= EMA20 (shallow pullback)

    # CCI oversold-reversal RANKING input (not a gate): a candidate whose CCI
    # dipped toward the oversold level within the lookback and is now turning
    # back up earns a ranking bonus. Full credit when the recent low reached
    # cci_oversold; scaled below that; zero if it never dipped or isn't rising.
    cci_period: int = 5
    cci_oversold: float = -100.0
    cci_overbought: float = 100.0  # no oversold-reversal bonus once CCI is above this
    cci_lookback: int = 5
    cci_rank_weight: float = 1.0  # R-equivalent bonus per 1.0 of CCI oversold-reversal score

    # --- Composite ranking (2026-07-20 redesign) ---
    # The old score was hist_r + small nudges, but hist_r (spread ~26) drowned the
    # nudges AND is proven non-predictive live. New score = weighted blend of each
    # factor's PERCENTILE across the day's candidates (0..1), so no single factor
    # dominates. EXTENSION (how far above EMA50 in ATRs; LESS is better) is the one a
    # 574-symbol / 8k-trade backtest validated (+0.025R vs random selection), so it
    # leads. hist_r kept only as a small tiebreaker until it earns its weight live.
    rank_w_ext: float = 0.45   # extension (primary, validated)
    rank_w_rvol: float = 0.20  # relative volume
    rank_w_cci: float = 0.15   # CCI oversold-reversal
    rank_w_hist: float = 0.20  # 2Y backtest score (demoted — doesn't predict live)

    # --- Backtest parameters ---
    bt_hist_duration: str = "2 Y"  # longer window pulled for backtesting
    bt_max_hold: int = 40          # time-stop: exit after this many bars if open

    # --- Execution (Phase 3) ---
    exec_max_orders: int = 10      # safety cap on number of bracket orders per run
    hist_duration: str = "6 M"  # historical window pulled from IBKR
    bar_size: str = "1 day"

    # Paths
    watchlist_path: Path = WATCHLIST_PATH
    logs_dir: Path = LOGS_DIR
    data_dir: Path = DATA_DIR
    cache_dir: Path = CACHE_DIR
    watchlists_dir: Path = WATCHLISTS_DIR
    reports_dir: Path = REPORTS_DIR
    history_dir: Path = HISTORY_DIR


# Module-level singleton for convenient import: `from src.config import SETTINGS`.
SETTINGS = Settings()
