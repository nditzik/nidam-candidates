#!/usr/bin/env python3
"""offline_scan.py — מריץ את לוגיקת הסריקה על נרות מקובצי CSV, בלי IBKR.

זו נקודת הכניסה למי שמעביר את הסריקה לענן: מחליפים רק את מקור הנרות
(load_bars / load_bars_2y) ואת מקור היקום (load_universe), והשאר זהה לסריקה המקורית.
הסקריפט משתמש בפונקציות המקוריות מתוך code/ (analyze, backtest_symbol, build_payload)
ומשכפל מ-scan.py רק את לולאת הסריקה ואת הדירוג (_rank_candidates), שורה מול שורה.

שימוש:
    python offline_scan.py --check          # מריץ על golden/ ומשווה לפלט האמיתי
    python offline_scan.py --out out.json   # כותב candidates.json
"""
import argparse, csv, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "code"))

import pandas as pd
from src.config.settings import SETTINGS          # פרמטרים וספים
from src.signals import analyze                   # זיהוי ה-setup + entry/stop/target
from src.signals.indicators import atr, ema
from src.backtest import backtest_symbol          # hist_r
import export_candidates                          # build_payload -> candidates.json

S = SETTINGS


# ---------------------------------------------------------------- מקורות נתונים
def load_universe(golden):
    """רשימת המניות לסריקה, בסדר הסריקה המקורי (חשוב לשבירת שוויון בדירוג).
    מחזיר [(symbol, rvol_from_file_or_None)]."""
    out = []
    with open(os.path.join(golden, "universe.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append((r["symbol"], float(r["rvol_file"]) if r["rvol_file"] != "" else None))
    return out


def _read(path):
    if not os.path.exists(path):
        return None
    # float_precision="round_trip" -> אותם ערכים בדיוק כמו במקור
    return pd.read_csv(path, float_precision="round_trip")


def load_bars(golden, sym):       # נרות הסריקה (חלון "6 M")
    return _read(os.path.join(golden, "bars", sym + ".csv"))


def load_bars_2y(golden, sym):    # נרות ה-backtest (חלון "2 Y") — רק למועמדים
    return _read(os.path.join(golden, "bars_2y", sym + ".csv"))


# ---------------------------------------------------------------- לוגיקת הסריקה
def _pct_rank(vals):
    """זהה ל-scan._pct_rank."""
    idx = [i for i, v in enumerate(vals) if v is not None]
    out = [0.5] * len(vals)
    if len(idx) <= 1:
        return out
    order = sorted(idx, key=lambda i: vals[i])
    for pos, i in enumerate(order):
        out[i] = pos / (len(idx) - 1)
    return out


def _rank_candidates(rows):
    """זהה ל-scan._rank_candidates."""
    if not rows:
        return
    ext_pct = _pct_rank([-(r["ext_atr"]) if r.get("ext_atr") is not None else None for r in rows])
    vol_pct = _pct_rank([r.get("rvol_disp") for r in rows])
    cci_pct = _pct_rank([r.get("cci_score") for r in rows])
    hr_pct = _pct_rank([r.get("hist_r") for r in rows])
    tot = (S.rank_w_ext + S.rank_w_rvol + S.rank_w_cci + S.rank_w_hist) or 1.0
    for k, r in enumerate(rows):
        r["rank_score"] = round((S.rank_w_ext * ext_pct[k] + S.rank_w_rvol * vol_pct[k]
                                 + S.rank_w_cci * cci_pct[k] + S.rank_w_hist * hr_pct[k]) / tot, 4)


def scan(golden):
    """מחזיר (rows ממוינים לפי דירוג, results לכל מניה שיש לה נרות)."""
    rows, results = [], {}
    for sym, rv_file in load_universe(golden):
        df = load_bars(golden, sym)
        if df is None:
            continue                                   # אין נרות ב-golden למניה הזו
        res = analyze(sym, df, S)
        if not res.has_signal:
            results[sym] = ("FAIL", res.reason)
            continue
        row = res.to_row()
        if (row.get("risk_pct") or 0) > S.max_risk_pct:  # סינון סטופ רחב מדי
            results[sym] = ("FAIL", "risk guard")
            continue
        try:
            atr_v = float(atr(df, S.atr_period).iloc[-1])
            e50 = float(ema(df["close"], S.ema_slowest).iloc[-1])
            c = float(row.get("close") or df["close"].iloc[-1])
            row["ext_atr"] = round((c - e50) / atr_v, 2) if atr_v else None
        except Exception:
            row["ext_atr"] = None
        bt = load_bars_2y(golden, sym)
        if bt is not None:
            trades = backtest_symbol(sym, bt, S)
            row["hist_r"] = round(sum(t.r for t in trades), 1) if trades else None
        row["rvol_disp"] = rv_file if rv_file is not None else (row.get("vol_ratio") or 0.0)
        results[sym] = ("PASS", row["reason"])
        rows.append(row)
    _rank_candidates(rows)
    rows.sort(key=lambda r: (r.get("rank_score") if r.get("rank_score") is not None else -1e9), reverse=True)
    return rows, results


def to_snapshot(rows, date_yyyymmdd):
    """אותו מבנה ש-snapshots.save_snapshot שומר, ממנו export_candidates בונה את הפלט."""
    return {"date": date_yyyymmdd, "symbols": [r["symbol"] for r in rows],
            "levels": {r["symbol"]: {"entry": r.get("entry"), "stop": r.get("stop"), "target": r.get("target"),
                                     "setup": r.get("setup"), "rvol": r.get("rvol_disp"), "cci": r.get("cci"),
                                     "risk_pct": r.get("risk_pct"), "hist_r": r.get("hist_r"),
                                     "ext_atr": r.get("ext_atr"), "rank_score": r.get("rank_score")} for r in rows}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=os.path.join(HERE, "golden"))
    ap.add_argument("--date", default=None, help="YYYYMMDD של הריצה (ברירת מחדל: מה-golden)")
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    gold = json.load(open(os.path.join(a.golden, "candidates.json"), encoding="utf-8"))
    date = a.date or gold["date"].replace("-", "")
    rows, results = scan(a.golden)
    payload = export_candidates.build_payload(to_snapshot(rows, date), a.max)
    if a.out:
        json.dump(payload, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"wrote {a.out} ({payload['shown']}/{payload['count']})")
    if a.check:
        bad = 0
        strip = lambda p: {k: v for k, v in p.items() if k != "_meta"}   # _meta = חותמת זמן בלבד
        if strip(payload) != strip(gold):
            bad += 1
            for g, n in zip(gold["candidates"], payload["candidates"]):
                if g != n:
                    print("DIFF", g["symbol"], {k: (g[k], n.get(k)) for k in g if g[k] != n.get(k)}); break
        exp = {r["symbol"]: r["result"] for r in csv.DictReader(open(os.path.join(a.golden, "universe.csv"), encoding="utf-8"))}
        wrong = [s for s, (res, _) in results.items() if exp[s] != res]
        print(f"candidates.json identical to golden: {strip(payload) == strip(gold)}  ({payload['shown']}/{payload['count']})")
        print(f"pass/fail matches universe.csv for {len(results) - len(wrong)}/{len(results)} symbols with bars")
        return 1 if (bad or wrong) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
