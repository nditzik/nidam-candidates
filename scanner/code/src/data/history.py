"""Fetch daily historical bars from IBKR, with a simple per-day disk cache.

Cache key is (symbol, today, duration, bar_size). A cached parquet/pickle for
today means re-runs are fast and avoid IBKR pacing limits. Bars older than
today are never refetched within the same day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path

import pandas as pd
from ib_async import IB, Stock, util

from src.config.settings import Settings

log = logging.getLogger(__name__)


def _cache_path(settings: Settings, symbol: str) -> Path:
    tag = f"{settings.hist_duration}_{settings.bar_size}".replace(" ", "")
    fname = f"{symbol}__{date.today():%Y%m%d}__{tag}.pkl"
    return settings.cache_dir / fname


def _load_cache(settings: Settings, symbol: str) -> pd.DataFrame | None:
    path = _cache_path(settings, symbol)
    if path.exists():
        try:
            return pd.read_pickle(path)
        except Exception:  # corrupt cache -> ignore and refetch
            return None
    return None


def _save_cache(settings: Settings, symbol: str, df: pd.DataFrame) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        df.to_pickle(_cache_path(settings, symbol))
    except Exception as exc:
        log.warning("Could not cache %s: %s", symbol, exc)


QUALIFY_TIMEOUT_S = 20  # seconds to wait for IBKR to identify a symbol

def fetch_daily_bars(
    ib: IB, symbol: str, settings: Settings, use_cache: bool = True
) -> pd.DataFrame | None:
    """Return a daily-bar DataFrame for ``symbol`` or None if unavailable."""
    if use_cache:
        cached = _load_cache(settings, symbol)
        if cached is not None:
            return cached

    contract = Stock(symbol, "SMART", "USD")
    # qualifyContracts has NO timeout: if the Gateway's data farm blips mid-request the
    # reply never comes and the whole scan hangs forever (stalled at 50 symbols on
    # 22.9 and 6.10). Bound it, skip the symbol, keep scanning.
    try:
        qualified = ib.run(asyncio.wait_for(ib.qualifyContractsAsync(contract),
                                            QUALIFY_TIMEOUT_S))
    except asyncio.TimeoutError:
        log.warning("Qualify timed out for %s after %ss; skipping.", symbol, QUALIFY_TIMEOUT_S)
        return None
    if not qualified:
        log.warning("Could not qualify %s; skipping.", symbol)
        return None

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=settings.hist_duration,
        barSizeSetting=settings.bar_size,
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    if not bars:
        log.warning("No bars returned for %s.", symbol)
        return None

    df = util.df(bars)
    if df is None or df.empty:
        return None

    if use_cache:
        _save_cache(settings, symbol, df)
    return df


def fetch_many(
    ib: IB, symbols: list[str], settings: Settings, use_cache: bool = True
) -> dict[str, pd.DataFrame]:
    """Fetch bars for many symbols, logging progress. Missing symbols omitted."""
    out: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, start=1):
        df = fetch_daily_bars(ib, sym, settings, use_cache=use_cache)
        if df is not None:
            out[sym] = df
        if i % 25 == 0 or i == total:
            log.info("Fetched %d/%d symbols (%d with data).", i, total, len(out))
    return out
