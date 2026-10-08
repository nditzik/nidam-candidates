"""Daily candidate snapshots for day-over-day diffing.

Each scan writes one JSON snapshot per date (``scan_YYYYMMDD.json``). Diffing a
new run against the previous trading day's snapshot yields which candidates are
new, carried over, or have dropped out of the list.
"""

from __future__ import annotations

import json
from pathlib import Path


def _path(history_dir: Path, date_str: str) -> Path:
    return Path(history_dir) / f"scan_{date_str}.json"


def save_snapshot(history_dir: Path, date_str: str, rows: list[dict],
                  extra: dict | None = None) -> Path:
    """Persist the candidate symbols (+ levels) for ``date_str``."""
    history_dir = Path(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "date": date_str,
        "symbols": [r["symbol"] for r in rows],
        "levels": {
            r["symbol"]: {
                "entry": r.get("entry"),
                "stop": r.get("stop"),
                "target": r.get("target"),
                "setup": r.get("setup"),
                "rvol": r.get("rvol_disp") if r.get("rvol_disp") is not None else r.get("vol_ratio"),
                "cci": r.get("cci"),
                "risk_pct": r.get("risk_pct"),
                "hist_r": r.get("hist_r"),
                "ext_atr": r.get("ext_atr"),
                "rank_score": r.get("rank_score"),
            }
            for r in rows
        },
    }
    if extra:
        data.update(extra)  # e.g. mode ("scan"/"reprice"), bar_date (signal session), source
    path = _path(history_dir, date_str)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def load_snapshots(history_dir: Path) -> list[dict]:
    """Return all snapshots sorted ascending by date (by filename)."""
    history_dir = Path(history_dir)
    if not history_dir.exists():
        return []
    out: list[dict] = []
    for p in sorted(history_dir.glob("scan_*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return out


def previous_snapshot(snaps: list[dict], today: str) -> dict | None:
    """The most recent snapshot strictly before ``today`` (previous run)."""
    prev = None
    for s in snaps:
        if s.get("date", "") < today:
            prev = s
    return prev


def days_in_list(snaps: list[dict], today: str, symbol: str) -> int:
    """Consecutive days (incl. today) the symbol has been a candidate."""
    count = 1
    for s in reversed(snaps):
        if s.get("date", "") >= today:
            continue
        if symbol in s.get("symbols", []):
            count += 1
        else:
            break
    return count
