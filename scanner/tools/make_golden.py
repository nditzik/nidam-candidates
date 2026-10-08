"""make_golden.py — מפיק את תיקיית golden/ מתוך המטמון המקומי של סריקה אמיתית.

רץ רק במחשב המקור (צריך את תיקיית המטמון של נרות IBKR ואת קובצי המומנטום).
מצורף כתיעוד של איך נבנתה דוגמת האימות. הוא לא משנה את הסריקה: הוא קורא את אותם
נרות שהסריקה שמרה במטמון ומריץ עליהם את אותן פונקציות (analyze, backtest_symbol).

שימוש (מתוך שורש המערכת המקורית):
    python make_golden.py --date 20261008 --out <dir>/golden
"""
import argparse, csv, dataclasses, json, math, pickle, sys
from pathlib import Path

sys.path.insert(0, ".")
from src.config import SETTINGS, load_watchlists, load_rvol_map, load_sector_map, load_last_listed_map
from src.signals import analyze
from src.signals.indicators import ema, atr, cci
from src.backtest import backtest_symbol

ap = argparse.ArgumentParser()
ap.add_argument("--date", required=True)      # YYYYMMDD of the scan run (cache key)
ap.add_argument("--out", required=True)
ap.add_argument("--near", type=int, default=50)
a = ap.parse_args()
OUT = Path(a.out); (OUT / "bars").mkdir(parents=True, exist_ok=True); (OUT / "bars_2y").mkdir(exist_ok=True)
S = SETTINGS
BARCOLS = ["date", "open", "high", "low", "close", "volume"]

def load(sym, tag):
    p = S.cache_dir / f"{sym}__{a.date}__{tag}.pkl"
    return pickle.load(open(p, "rb")).reset_index(drop=True) if p.exists() else None

def dump(df, path):
    d = df[BARCOLS].copy(); d["date"] = d["date"].astype(str).str[:10]
    d.to_csv(path, index=False)          # pandas writes shortest round-trip floats

symbols, sources = load_watchlists(S.watchlists_dir)
rvol_map = load_rvol_map(S.watchlists_dir); sector_map = load_sector_map(S.watchlists_dir)
listed = load_last_listed_map(S.watchlists_dir)

rows, cand = [], []
for order, sym in enumerate(symbols, 1):
    r = {"order": order, "symbol": sym, "source_file": (sources.get(sym) or [""])[0],
         "rvol_file": rvol_map.get(sym), "sector": sector_map.get(sym), "last_listed": listed.get(sym)}
    df = load(sym, "6M_1day")
    if df is None:
        r.update(result="FAIL", fail_stage="0_no_data", reason="no bars returned by IBKR"); rows.append(r); continue
    res = analyze(sym, df, S)
    c, o, lo = float(df["close"].iloc[-1]), float(df["open"].iloc[-1]), float(df["low"].iloc[-1])
    r.update(n_bars=len(df), first_bar=str(df["date"].iloc[0])[:10], last_bar=str(df["date"].iloc[-1])[:10],
             open=o, low=lo, close=c)
    if len(df) >= 55:
        e20, e40, e50 = (float(ema(df["close"], n).iloc[-1]) for n in (S.ema_fast, S.ema_slow, S.ema_slowest))
        atr_v = float(atr(df, S.atr_period).iloc[-1])
        r.update(ema20=e20, ema40=e40, ema50=e50, atr14=atr_v, vol_ratio_ibkr=res.vol_ratio, cci5=res.cci,
                 cci_score=res.cci_score, stack_ok=bool(e20 > e40 > e50), green=bool(c > o),
                 ext_atr=round((c - e50) / atr_v, 2) if atr_v else None,
                 risk_pct_if_signal=(S.atr_stop_mult * atr_v / c * 100) if c else None)
    if not res.has_signal:
        stage = ("0_insufficient_history" if res.reason == "insufficient history" else
                 "1_no_uptrend_stack" if res.reason.startswith("not in general uptrend") else
                 "2_no_bounce" if res.reason != "invalid ATR" else "2_invalid_atr")
        r.update(result="FAIL", fail_stage=stage, reason=res.reason); rows.append(r); continue
    row = res.to_row()
    r.update(setup=row["setup"], entry=row["entry"], stop=row["stop"], target=row["target"], risk_pct=row["risk_pct"])
    if (row.get("risk_pct") or 0) > S.max_risk_pct:
        r.update(result="FAIL", fail_stage="3_risk_guard", reason=f"risk_pct {row['risk_pct']:.2f} > max_risk_pct {S.max_risk_pct}")
        rows.append(r); continue
    bt = load(sym, "2Y_1day")
    hist_r = None
    if bt is not None:
        tr = backtest_symbol(sym, bt, S)
        hist_r = round(sum(t.r for t in tr), 1) if tr else None
        dump(bt, OUT / "bars_2y" / f"{sym}.csv")
    rv = rvol_map.get(sym); eff = rv if rv is not None else (row.get("vol_ratio") or 0.0)
    r.update(result="PASS", fail_stage="", reason=row["reason"], hist_r=hist_r, rvol_used=eff,
             rvol_is_ibkr=rv is None)
    rows.append(r); cand.append(r); dump(df, OUT / "bars" / f"{sym}.csv")

# ---- ranking: identical to scan._rank_candidates ----
def pct_rank(vals):
    idx = [i for i, v in enumerate(vals) if v is not None]; out = [0.5] * len(vals)
    if len(idx) <= 1: return out
    for pos, i in enumerate(sorted(idx, key=lambda i: vals[i])): out[i] = pos / (len(idx) - 1)
    return out
ext = pct_rank([-(r["ext_atr"]) if r.get("ext_atr") is not None else None for r in cand])
vol = pct_rank([r.get("rvol_used") for r in cand]); cc = pct_rank([r.get("cci_score") for r in cand])
hr = pct_rank([r.get("hist_r") for r in cand])
tot = S.rank_w_ext + S.rank_w_rvol + S.rank_w_cci + S.rank_w_hist
for k, r in enumerate(cand):
    r["rank_score"] = round((S.rank_w_ext*ext[k] + S.rank_w_rvol*vol[k] + S.rank_w_cci*cc[k] + S.rank_w_hist*hr[k]) / tot, 4)
for i, r in enumerate(sorted(cand, key=lambda r: r["rank_score"], reverse=True), 1): r["rank"] = i

# ---- near misses: the failures closest to becoming a candidate ----
def nearness(r):
    if r.get("fail_stage") == "3_risk_guard": return (0, r["risk_pct"])
    if r.get("fail_stage") != "2_no_bounce": return None
    prox = S.pullback_proximity
    gaps = [r["low"] / r[k] - (1 + prox) for k in ("ema20", "ema40", "ema50")]   # <=0 means "touched"
    touch_gap = max(0.0, min(gaps))
    red = max(0.0, (r["open"] - r["close"]) / r["open"])
    below = max(0.0, r["ema20"] / r["close"] - 1)
    return (1, touch_gap + red + below)
nm = sorted([(nearness(r), r) for r in rows if r["result"] == "FAIL" and nearness(r) is not None], key=lambda x: x[0])
for i, (_, r) in enumerate(nm[:a.near], 1):
    r["near_miss_rank"] = i; dump(load(r["symbol"], "6M_1day"), OUT / "bars" / f"{r['symbol']}.csv")

cols = ["order","symbol","result","fail_stage","reason","setup","rank","rank_score","entry","stop","target","risk_pct",
        "ext_atr","hist_r","rvol_used","rvol_is_ibkr","rvol_file","vol_ratio_ibkr","cci5","cci_score","near_miss_rank",
        "n_bars","first_bar","last_bar","open","low","close","ema20","ema40","ema50","atr14","stack_ok","green",
        "risk_pct_if_signal","sector","last_listed","source_file"]
with open(OUT / "universe.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
    for r in rows: w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
import collections
print("universe:", len(rows), dict(collections.Counter(r["fail_stage"] or "PASS" for r in rows)))
print("candidates:", len(cand), "| near-miss bars:", min(a.near, len(nm)), "| bars files:", len(list((OUT/'bars').glob('*.csv'))))
