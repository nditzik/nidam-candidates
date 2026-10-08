# ---------------------------------------------------------------------------------
# PUBLIC REFERENCE COPY — for reading, not for running.
# Verbatim from the source system except: the account/holdings helper functions were
# removed (see the [REMOVED] marker). It imports modules that are not published here
# (src.ibkr, src.journal, src.dashboard). The runnable equivalent is ../offline_scan.py.
# ---------------------------------------------------------------------------------
"""Scan a CSV watchlist for EMA 20/40 BUY signals and build an HTML dashboard.

Flow: load symbols from CSV -> connect to Paper IBKR -> fetch daily bars
(cached per day) -> analyze each (EMA 20/40 crossover + ATR stop + 2R target)
-> keep only fresh BUY signals -> size positions off account equity ->
render a self-contained HTML dashboard.

Usage:
    python scan.py
    python scan.py --csv data/momentum_19.6.2026.csv --limit 40 --open
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path

from src.backtest import aggregate, backtest_symbol
from src.config import (
    SETTINGS,
    load_symbols_csv,
    load_watchlists,
    load_rvol_map,
    _rvol_from_file,
    load_sector_map,
    load_earnings_map,
    load_last_listed_map,
)
from src.dashboard import render_dashboard
from src.dashboard.charts import render_chart
from src.data import fetch_daily_bars, fetch_many
from ib_async import Stock
from src.data.snapshots import (
    days_in_list,
    load_snapshots,
    previous_snapshot,
    save_snapshot,
)
from src.ibkr import connect, disconnect
from src.journal import sync_executions
from src.signals import analyze
from src.signals.indicators import ema, atr


def _pct_rank(vals: list[float]) -> list[float]:
    """Map each value to its 0..1 percentile within the list (higher value -> higher
    percentile; a missing value -> 0.5, neutral). Ties keep their sorted position."""
    idx = [i for i, v in enumerate(vals) if v is not None]
    out = [0.5] * len(vals)
    if len(idx) <= 1:
        return out
    order = sorted(idx, key=lambda i: vals[i])
    for pos, i in enumerate(order):
        out[i] = pos / (len(idx) - 1)
    return out


def _rank_candidates(rows: list[dict]) -> None:
    """Set row['rank_score'] as a weighted blend of each factor's percentile across
    the candidate set, so no single factor dominates (see settings rank_w_*).
    EXTENSION leads (validated); hist_r is demoted. Writes rank_score in place."""
    if not rows:
        return
    ext_pct = _pct_rank([-(r["ext_atr"]) if r.get("ext_atr") is not None else None
                         for r in rows])                       # less extended -> higher
    vol_pct = _pct_rank([r.get("rvol_disp") for r in rows])
    cci_pct = _pct_rank([r.get("cci_score") for r in rows])
    hr_pct = _pct_rank([r.get("hist_r") for r in rows])
    w = SETTINGS
    tot = (w.rank_w_ext + w.rank_w_rvol + w.rank_w_cci + w.rank_w_hist) or 1.0
    for k, r in enumerate(rows):
        r["rank_score"] = round(
            (w.rank_w_ext * ext_pct[k] + w.rank_w_rvol * vol_pct[k]
             + w.rank_w_cci * cci_pct[k] + w.rank_w_hist * hr_pct[k]) / tot, 4)


def _bt_payload(trades, risk_per_trade) -> dict:
    """Slim per-symbol backtest summary + trade list for embedding in HTML."""
    st = aggregate(trades, risk_per_trade)
    if st.get("trades", 0) == 0:
        return {"trades": 0, "recent": []}
    pf = st["profit_factor"]
    shares = 100  # fixed lot for the $ P&L column
    recent = [
        {"in": t.entry_date, "out": t.exit_date, "r": round(t.r, 2),
         "outcome": t.outcome, "bars": t.bars_held,
         "pnl": round((t.exit - t.entry) * shares)}
        for t in sorted(trades, key=lambda x: x.exit_date, reverse=True)
    ]
    return {
        "trades": st["trades"],
        "win_rate": round(st["win_rate"], 1),
        "expectancy_r": round(st["expectancy_r"], 3),
        "profit_factor": None if pf == float("inf") else round(pf, 2),
        "total_r": round(st["total_r"], 1),
        "avg_win_r": round(st["avg_win_r"], 2),
        "avg_loss_r": round(st["avg_loss_r"], 2),
        "avg_bars_held": round(st["avg_bars_held"], 1),
        "max_dd_r": round(st["max_dd_r"], 1),
        "pnl100_total": round(sum(t["pnl"] for t in recent)),
        "recent": recent,
    }


# Re-price ("עדכן רמות"): only the previous day's Top-N, and the support EMA per setup
# (a pair's slower EMA is the support it bounced off).
REPRICE_TOP_N = 25


def _session_of(et_dt) -> str:
    """US session (YYYY-MM-DD) whose daily bar is the latest COMPLETED one at ET time
    et_dt: today after 16:00 ET on a weekday, else the previous weekday."""
    from datetime import timedelta
    d = et_dt.date()
    if not (et_dt.weekday() < 5 and (et_dt.hour, et_dt.minute) >= (16, 0)):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _et_now():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _snapshot_meta(path):
    """(mode, bar_date) of a saved snapshot. New snapshots store both; legacy ones get
    bar_date inferred from the file's write time (the session complete at that moment)
    and mode 'scan'."""
    from zoneinfo import ZoneInfo
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    bar = d.get("bar_date")
    if not bar:
        et = datetime.fromtimestamp(Path(path).stat().st_mtime, ZoneInfo("America/New_York"))
        bar = _session_of(et)
    return d, d.get("mode", "scan"), bar
SUPPORT_EMA = {"EMA20/40": 40, "EMA40/50": 50, "EMA50": 50, "EMA20": 20}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EMA 20/40 swing-signal dashboard.")
    p.add_argument("--csv", default=None,
                   help="Scan a single CSV instead of all files in data/watchlists/.")
    p.add_argument("--limit", type=int, default=0,
                   help="Only scan the first N symbols (0 = all). Useful for testing.")
    p.add_argument("--no-cache", action="store_true",
                   help="Ignore the per-day disk cache and refetch from IBKR.")
    p.add_argument("--open", action="store_true",
                   help="Open the dashboard in the default browser when done.")
    p.add_argument("--reprice", action="store_true",
                   help="RE-PRICE mode: keep the latest snapshot's candidate names and "
                        "recompute entry/stop/target on today's data (same 1.5xATR/rr model), "
                        "skipping the fresh-signal gate. Rebuilds the dashboard so a setup "
                        "identified on a prior close stays actionable at current levels.")
    return p.parse_args()


def setup_logging() -> None:
    SETTINGS.logs_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(SETTINGS.logs_dir / "scan.log", encoding="utf-8"),
        ],
    )


# [REMOVED for the public copy] account / holdings helpers:
#   account_value, _prior_close, _prev_closes, _day_change, build_holdings.
# They read balances and open positions for the local dashboard only and take no part
# in selecting, pricing or ranking candidates. main() below still calls them (equity,
# holdings, account) — those values feed the dashboard, never candidates.json.


def main() -> int:
    args = parse_args()
    setup_logging()
    log = logging.getLogger("scan")

    reprice_setups: dict = {}
    reprice_dropped: dict = {}
    reprice_L = reprice_sig = None
    if args.reprice:
        # RE-PRICE = continuation check. L = the latest COMPLETED session. Source = the
        # most recent FULL-SCAN snapshot whose signal session is BEFORE L (i.e. the green
        # bounce was on the session before L); its Top-N are re-checked on L's candle.
        # Reprice snapshots are never a source. Tue morning: L=Mon -> Friday's list.
        reprice_L = _session_of(_et_now())
        _src, _src_bar = None, None
        for _pth in sorted(Path(SETTINGS.history_dir).glob("scan_*.json"), reverse=True):
            try:
                _d, _mode, _bar = _snapshot_meta(_pth)
            except (ValueError, OSError):
                continue
            if _mode != "reprice" and _d.get("levels") and _bar < reprice_L:
                _src, _src_bar = _d, _bar
                break
        if not _src:
            log.error("No full-scan snapshot with a signal session before %s.", reprice_L)
            return 1
        reprice_sig = _src_bar
        log.info("Reprice: signal session %s (snapshot %s) -> checking candle of %s",
                 _src_bar, _src.get("date"), reprice_L)
        _lv = _src["levels"]
        _order = {s: i for i, s in enumerate(_src.get("symbols") or list(_lv))}
        _ranked = sorted(_lv, key=lambda s: (-(_lv[s].get("rank_score")
                                               if _lv[s].get("rank_score") is not None else -1e9),
                                             _order.get(s, 1e9)))
        symbols = _ranked[:REPRICE_TOP_N]
        _lv = {s: _lv[s] for s in symbols}
        sources = {s: [f"reprice<-{_src.get('date', '')}"] for s in symbols}
        reprice_setups = {s: v.get("setup") for s, v in _lv.items()}
        rvol_map = {s: v.get("rvol") for s, v in _lv.items() if v.get("rvol") is not None}
        source_desc = (f"continuation of {_src_bar} signals (Top {len(symbols)}) "
                       f"on the {reprice_L} candle")
    elif args.csv:
        symbols = load_symbols_csv(args.csv)
        sources = {s: [Path(args.csv).stem] for s in symbols}
        rvol_map = _rvol_from_file(Path(args.csv))
        source_desc = args.csv
    else:
        symbols, sources = load_watchlists(SETTINGS.watchlists_dir)
        rvol_map = load_rvol_map(SETTINGS.watchlists_dir)
        source_desc = f"{SETTINGS.watchlists_dir} ({len(list(SETTINGS.watchlists_dir.glob('*.csv')))} files)"
    sector_map = load_sector_map(SETTINGS.watchlists_dir)
    earnings_map = load_earnings_map(SETTINGS.watchlists_dir)  # empty until CSV adds the column
    last_listed_map = load_last_listed_map(SETTINGS.watchlists_dir)  # newest file date per symbol
    if args.limit > 0:
        symbols = symbols[: args.limit]
    log.info("Loaded %d symbols from %s", len(symbols), source_desc)

    # Backtest needs a longer history than the live screen.
    bt_settings = dataclasses.replace(SETTINGS, hist_duration=SETTINGS.bt_hist_duration)

    rows: list[dict] = []
    equity = None
    bars: dict = {}
    holdings: list[dict] = []
    account: dict = {}
    ib = None
    try:
        ib = connect(SETTINGS)
        equity = account_value(ib, "NetLiquidation")
        log.info("Account equity (NetLiquidation): %s", equity)

        # Capture today's executions into the journal so trade history (incl.
        # exits) is never lost — ib.fills() only returns the current day.
        synced = sync_executions(ib, SETTINGS.reports_dir / "trades_log.csv")
        log.info("Journal: %d executions in log.", len(synced))

        # Holdings first (incl. prior-close day-change) — before the heavy
        # watchlist fetch, so these few requests don't hit pacing limits.
        holdings, account = build_holdings(ib, set())

        # Re-price needs TODAY's completed bar, so bypass the (possibly pre-close) cache.
        bars = fetch_many(ib, symbols, SETTINGS,
                          use_cache=not (args.no_cache or args.reprice))

        # Keep only BUY signals; for each, run a per-symbol backtest over 2Y.
        for sym in symbols:
            df = bars.get(sym)
            if df is None:
                continue
            if args.reprice:
                # Only COMPLETED candles up to L (drop today's still-forming bar intraday),
                # and there must be a candle AFTER the signal session to judge continuation.
                _dates = df["date"].astype(str).str[:10]
                df = df[_dates <= reprice_L].reset_index(drop=True)
                _dates = df["date"].astype(str).str[:10]
                if df.empty or _dates.iloc[-1] <= reprice_sig:
                    reprice_dropped[sym] = f"no candle after the {reprice_sig} signal yet"
                    continue
                _sig = df[_dates <= reprice_sig]
                _sig_close = float(_sig["close"].iloc[-1]) if not _sig.empty else None
            res = analyze(sym, df, SETTINGS, force_levels=args.reprice)
            if not res.has_signal:
                continue
            row = res.to_row()
            if args.reprice:
                setup0 = reprice_setups.get(sym)
                if setup0:
                    row["setup"] = setup0  # keep the original setup label
                # Support check: close below the EMA the setup bounced off (the slower EMA
                # of a pair) = the bounce failed -> drop it from the re-priced list.
                sup_n = SUPPORT_EMA.get(setup0 or "", SETTINGS.ema_fast)
                sup_v = float(ema(df["close"], sup_n).iloc[-1])
                close_v = float(df["close"].iloc[-1])
                if close_v < sup_v:
                    reprice_dropped[sym] = (f"broke support: close {close_v:.2f} "
                                            f"< EMA{sup_n} {sup_v:.2f} ({setup0 or '-'})")
                    log.info("Reprice drop %s — %s", sym, reprice_dropped[sym])
                    continue
                # Next-day candle = the continuation the user reads: % move vs the signal
                # day's close (colored green/red in the table).
                if _sig_close:
                    row["nd_pct"] = (close_v / _sig_close - 1) * 100
            # Guard: drop wide-stop candidates (they'd risk disproportionately on a
            # fixed-$ position, and wide stops historically lose).
            if (row.get("risk_pct") or 0) > SETTINGS.max_risk_pct:
                continue
            # Equal-weight sizing: a fixed $ slice of the sizing capital per position.
            if res.risk_per_share > 0 and row.get("entry"):
                pos_dollars = SETTINGS.sizing_capital / max(1, SETTINGS.target_positions)
                shares = math.floor(pos_dollars / row["entry"])
                row["shares"] = shares
                row["dollar_risk"] = shares * res.risk_per_share
                row["cost"] = shares * row["entry"]  # $ to enter the position
            # Extension: how far the entry sits above EMA50, in ATR units. A big
            # value = the name ran without a real pullback (parabolic); the backtest
            # shows those lose. Feeds the ranking as a penalty (less extended = better).
            try:
                _atrv = float(atr(df, SETTINGS.atr_period).iloc[-1])
                _e50 = float(ema(df["close"], SETTINGS.ema_slowest).iloc[-1])
                _c = float(row.get("close") or df["close"].iloc[-1])
                row["ext_atr"] = round((_c - _e50) / _atrv, 2) if _atrv else None
            except Exception:
                row["ext_atr"] = None
            # Sector (broad group) from the momentum file; upside to target %.
            row["sector"] = sector_map.get(sym)
            row["earnings"] = earnings_map.get(sym)  # next report date, if the CSV has it
            row["last_listed"] = last_listed_map.get(sym)  # newest momentum-file date listing it
            tgt, lst = row.get("target"), row.get("close")
            if tgt and lst:
                row["tp_pct"] = (tgt / lst - 1) * 100
            # Candlestick chart for this candidate.
            chart_path = render_chart(
                sym, df, res, SETTINGS, SETTINGS.reports_dir / "charts" / f"{sym}.png"
            )
            if chart_path is not None:
                row["chart"] = f"charts/{sym}.png"
            # Show only the newest file that flagged the symbol (sources are
            # ordered newest-first), and just its date (drop the momentum_ prefix).
            _src = sources.get(sym) or [""]
            row["source"] = _src[0].replace("momentum_", "")
            # Per-symbol historical backtest (2Y) shown on ticker click.
            bt_df = fetch_daily_bars(ib, sym, bt_settings, use_cache=not args.no_cache)
            if bt_df is not None:
                trades = backtest_symbol(sym, bt_df, SETTINGS)
                row["bt"] = _bt_payload(trades, SETTINGS.risk_per_trade)
                row["hist_r"] = row["bt"].get("total_r")  # historical total R
            # Volume input: prefer the file's RVOL (consolidated, accurate);
            # fall back to IBKR's vol_ratio (which under-reports) only when the
            # watchlist has no RVOL for this symbol.
            rv_file = rvol_map.get(sym)
            row["rvol"] = rv_file
            eff_vol = rv_file if rv_file is not None else (row.get("vol_ratio") or 0.0)
            row["rvol_disp"] = eff_vol
            row["rvol_is_ibkr"] = rv_file is None
            # rank_score is computed AFTER the loop (a percentile blend needs the
            # whole candidate set), see _rank_candidates below.
            rows.append(row)

        # Mark which holdings are also candidates today (computed above).
        cand_syms = {r["symbol"] for r in rows}
        for h in holdings:
            h["is_candidate"] = h["symbol"] in cand_syms
        log.info("Account: %d holdings, unrealized %.0f",
                 len(holdings), account.get("total_unreal", 0))
    except (ValueError, ConnectionError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        if ib is not None:
            disconnect(ib)

    log.info("Rendered %d charts; backtested %d candidates.",
             sum(1 for r in rows if r.get("chart")),
             sum(1 for r in rows if r.get("bt")))

    # Composite ranking: blend each factor's PERCENTILE across the candidate set so
    # no single factor dominates (the old score was drowned by hist_r's huge spread).
    _rank_candidates(rows)
    rows.sort(key=lambda r: (r.get("rank_score") if r.get("rank_score") is not None
                             else -1e9),
              reverse=True)
    for _i, _r in enumerate(rows, 1):  # rank position after ranking
        _r["rank"] = _i

    # --- Day-over-day diff vs the previous trading day's snapshot ---
    today = date.today().strftime("%Y%m%d")
    snaps = load_snapshots(SETTINGS.history_dir)
    prev = previous_snapshot(snaps, today)
    prev_syms = set(prev["symbols"]) if prev else set()
    today_syms = {r["symbol"] for r in rows}

    for r in rows:
        sym = r["symbol"]
        if prev and sym in prev_syms:
            r["status"] = "carried"
            r["days_in_list"] = days_in_list(snaps, today, sym)
        else:
            r["status"] = "new"
            r["days_in_list"] = 1

    dropped = []
    if prev:
        for sym in sorted(prev_syms - today_syms):
            df = bars.get(sym)
            if sym in reprice_dropped:
                reason = reprice_dropped[sym]
            else:
                reason = analyze(sym, df, SETTINGS).reason if df is not None \
                    else "removed from watchlist"
            dropped.append({"symbol": sym, "reason": reason})

    _bar_dates = [str(v["date"].iloc[-1])[:10] for v in bars.values()
                  if v is not None and len(v)]
    save_snapshot(SETTINGS.history_dir, today, rows, extra=(
        {"mode": "reprice", "bar_date": reprice_L, "source_bar": reprice_sig}
        if args.reprice else
        {"mode": "scan", "bar_date": max(_bar_dates) if _bar_dates else None}))

    n_new = sum(1 for r in rows if r["status"] == "new")
    # Newest watchlist file (by mtime) — shown on the dashboard so it's obvious the
    # table reflects the latest dropped file, not a stale prior scan.
    try:
        _wl = sorted(SETTINGS.watchlists_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        latest_file = _wl[-1].name if _wl else None
    except OSError:
        latest_file = None
    meta = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "latest_file": latest_file,
        "source": source_desc,
        "scanned": len(symbols),
        "with_data": len(bars),
        "equity": equity,
        "risk_per_trade": SETTINGS.risk_per_trade,
        "prev_date": prev["date"] if prev else None,
        "dropped": dropped,
        "new_count": n_new,
        "holdings": holdings,
        "account": account,
    }

    latest = SETTINGS.reports_dir / "dashboard.html"
    render_dashboard(rows, meta, latest)

    log.info("Found %d BUY signals (%d new, %d dropped vs %s).",
             len(rows), n_new, len(dropped), prev["date"] if prev else "n/a")
    log.info("Dashboard: %s", latest)

    # שליחת המועמדים לאתר nidam-markets (מייל [IBKR-CANDIDATES]) — רק אם מוגדרת
    # [REDACTED: stale comment naming a mail credential variable]. Fully wrapped: never fails the scan.
    try:
        import export_candidates
        export_candidates.auto_send()
    except Exception as exc:
        log.warning("שליחת מועמדים לאתר דולגה: %s", exc)

    if args.open:
        webbrowser.open(latest.resolve().as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
