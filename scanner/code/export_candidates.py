#!/usr/bin/env python3
"""
export_candidates.py — דוחף את מועמדי הסריקה האחרונה לריפו ציבורי nidam-candidates,
לצריכת אתר nidam-markets. בלי מייל ובלי סיסמה — דחיפת git רגילה (כמו מומנטום/מדדים).

קורא את ה-snapshot העדכני (reports/history/scan_YYYYMMDD.json) — שהוא כבר
CANDIDATE-ONLY — בונה candidates.json ודוחף אותו. **אין נתוני חשבון/פוזיציות/יתרות.**

שימוש:
  python export_candidates.py            # דוחף ל-GitHub
  python export_candidates.py --out P    # כותב מקומית בלבד (בדיקה)
  python export_candidates.py --max 50   # מגביל מספר מועמדים

נקרא אוטומטית בסוף כל scan.py דרך auto_send().
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "history")

# ריפו היעד (ציבורי) + מיקום הקלון המקומי
REPO_URL = "https://github.com/nditzik/nidam-candidates.git"
CLONE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nidam-candidates")  # [REDACTED] original was a personal local path
OUT_NAME = "candidates.json"
GIT_NAME = "nditzik"
GIT_EMAIL = "REDACTED@example.com"  # [REDACTED]

# רק שדות מועמד — שום דבר מהחשבון
FIELDS = ["setup", "entry", "stop", "target", "risk_pct", "rvol", "hist_r", "ext_atr", "rank_score"]


def israel_stamp():
    now = datetime.now(timezone.utc)
    off = 3 if 4 <= now.month <= 10 else 2
    return (now + timedelta(hours=off)).strftime("%d/%m/%Y %H:%M")


def latest_snapshot():
    files = sorted(glob.glob(os.path.join(HISTORY, "scan_*.json")))
    return json.loads(open(files[-1], encoding="utf-8").read()) if files else None


def build_payload(snap, max_n):
    date_raw = str(snap.get("date", ""))
    date_iso = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}" if len(date_raw) == 8 else date_raw
    symbols = snap.get("symbols", [])
    levels = snap.get("levels", {})
    cands = []
    for i, sym in enumerate(symbols[:max_n], 1):
        lv = levels.get(sym, {})
        row = {"rank": i, "symbol": sym}
        for f in FIELDS:
            row[f] = lv.get(f)
        entry, tgt = lv.get("entry"), lv.get("target")
        row["tp_pct"] = round((tgt / entry - 1) * 100, 2) if entry and tgt else None
        cands.append(row)
    return {
        "date": date_iso,
        "count": len(symbols),
        "shown": len(cands),
        "candidates": cands,
        "_meta": {"updatedAt": israel_stamp(), "source": "ibkr-swing-system"},
    }


def _git(args, **kw):
    return subprocess.run(["git"] + args, cwd=CLONE_DIR, capture_output=True, text=True, **kw)


def ensure_clone():
    """מוודא שקיים קלון מקומי של הריפו; אם לא — משכפל."""
    if os.path.isdir(os.path.join(CLONE_DIR, ".git")):
        return True
    print(f"[git] משכפל {REPO_URL} → {CLONE_DIR} ...")
    r = subprocess.run(["git", "clone", REPO_URL, CLONE_DIR], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[fail] clone נכשל: {r.stderr.strip()}")
        return False
    return True


def push_candidates(payload):
    if not ensure_clone():
        return 1
    # רענון מהרמוט (שלא נהיה מאחור) — התעלמות משגיאות
    _git(["pull", "--rebase", "--autostash"])
    path = os.path.join(CLONE_DIR, OUT_NAME)
    new = json.dumps(payload, ensure_ascii=False, indent=2)
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if new == old:
        print("[nochange] אין שינוי במועמדים — לא דוחף.")
        return 0
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    _git(["add", OUT_NAME])
    msg = f"candidates: {payload['date']} ({payload['shown']}/{payload['count']})"
    c = _git(["-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}", "commit", "-m", msg])
    if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
        print(f"[warn] commit: {c.stderr.strip() or c.stdout.strip()}")
    _git(["branch", "-M", "main"])
    p = _git(["push", "-u", "origin", "main"])
    if p.returncode != 0:
        print(f"[fail] push נכשל: {p.stderr.strip()}")
        return 1
    print(f"[ok] נדחפו {payload['shown']} מועמדים ({payload['date']}) → nidam-candidates")
    return 0


def auto_send(max_n=60):
    """נקראת בסוף scan.py. בטוחה לחלוטין — לא זורקת חריגה ולא מפילה את הסריקה."""
    try:
        snap = latest_snapshot()
        if snap is None:
            print("[skip] אין snapshot לדחיפה.")
            return
        push_candidates(build_payload(snap, max_n))
    except Exception as e:
        print(f"[warn] דחיפת מועמדים לאתר נכשלה (הסריקה לא נפגעה): {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="כתיבה מקומית לקובץ במקום דחיפה")
    ap.add_argument("--max", type=int, default=60, help="מקסימום מועמדים מובילים (ברירת מחדל 60)")
    args = ap.parse_args()

    snap = latest_snapshot()
    if snap is None:
        print("[fail] לא נמצא snapshot ב-reports/history.")
        return 1
    payload = build_payload(snap, args.max)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[done] נכתב {args.out} ({payload['shown']}/{payload['count']})")
        return 0
    return push_candidates(payload)


if __name__ == "__main__":
    sys.exit(main())
