"""Load the trading watchlist from config/watchlist.txt."""

from __future__ import annotations

import csv
from pathlib import Path

from .settings import WATCHLIST_PATH


def load_watchlist(path: Path | str = WATCHLIST_PATH) -> list[str]:
    """Return the list of tickers from the watchlist file.

    One ticker per line. Blank lines and lines starting with ``#`` are
    ignored. Tickers are stripped and upper-cased. Order is preserved and
    duplicates are removed.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the file contains no usable tickers.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Watchlist file not found: {path}")

    tickers: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ticker = line.upper()
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)

    if not tickers:
        raise ValueError(f"Watchlist file is empty (no tickers): {path}")

    return tickers


def load_symbols_csv(path: Path | str, column: str = "Symbol") -> list[str]:
    """Return tickers from the first/``column`` field of a CSV file.

    Tolerant of the typical export quirks in these files: a UTF-8 BOM, many
    trailing empty columns, blank lines, and stray text appended to a cell
    (only the leading ticker token of each row is kept). Tickers are
    upper-cased, de-duplicated, and order is preserved.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if no usable tickers are found.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        raise ValueError(f"CSV file is empty: {path}")

    # Detect and skip a header row if the first cell matches `column`.
    header_cells = [c.strip() for c in lines[0].split(",")]
    start = 1 if header_cells and header_cells[0].lower() == column.lower() else 0

    tickers: list[str] = []
    seen: set[str] = set()
    for raw in lines[start:]:
        first = raw.split(",", 1)[0].strip()
        if not first:
            continue
        # Keep only the leading ticker token (guards against stray cell text).
        token = first.split()[0].upper()
        # Tickers are letters, optionally with '.'/'-' (e.g. BRK.B). Skip junk.
        if not all(ch.isalnum() or ch in ".-" for ch in token):
            continue
        if token not in seen:
            seen.add(token)
            tickers.append(token)

    if not tickers:
        raise ValueError(f"No tickers found in CSV: {path}")

    return tickers


def load_watchlists(directory: Path | str) -> tuple[list[str], dict[str, list[str]]]:
    """Merge every ``*.csv`` in ``directory`` into one de-duplicated symbol list.

    Returns ``(symbols, sources)`` where ``sources[symbol]`` is the list of
    watchlist file stems the symbol appeared in (so the dashboard can show
    which list flagged it). Files are processed newest-first (by modification
    time), so for a symbol present in several daily files the most recent dated
    file is listed first in its sources and appears first in the merged list.

    Raises:
        FileNotFoundError: if the directory does not exist.
        ValueError: if it contains no readable .csv watchlists.
    """
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(
            f"Watchlists folder not found: {directory}. Create it and drop .csv files in."
        )

    # Newest file first so the most recent daily drop wins in `sources` order.
    files = sorted(directory.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise ValueError(f"No .csv watchlists found in {directory}")

    symbols: list[str] = []
    seen: set[str] = set()
    sources: dict[str, list[str]] = {}
    for f in files:
        name = f.stem
        try:
            tickers = load_symbols_csv(f)
        except ValueError:
            continue  # skip an empty/garbage file rather than abort the run
        for sym in tickers:
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)
                sources[sym] = [name]
            elif name not in sources[sym]:
                sources[sym].append(name)

    if not symbols:
        raise ValueError(f"No tickers found across watchlists in {directory}")

    return symbols, sources


def _rvol_from_file(path: Path) -> dict[str, float]:
    """Map ticker -> RVOL from one CSV (proper CSV parse; case-insensitive
    header match on a column containing 'rvol'/'rel vol'/'relative vol').
    Returns {} if the file has no such column."""
    path = Path(path)
    out: dict[str, float] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return out
            fmap = {h.lower().strip(): h for h in reader.fieldnames if h}
            kcol = fmap.get("symbol") or reader.fieldnames[0]
            vcol = next((fmap[h] for h in fmap
                         if "rvol" in h or "rel vol" in h or "relative vol" in h), None)
            if vcol is None:
                return out
            for row in reader:
                sym = (row.get(kcol) or "").strip().upper().split(" ")[0]
                if not sym or sym in out:
                    continue
                raw = (row.get(vcol) or "").strip().replace("%", "").replace(",", "")
                try:
                    out[sym] = float(raw)
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def load_rvol_map(directory: Path | str) -> dict[str, float]:
    """Map ticker -> RVOL using the *newest* watchlist CSV that carries it.

    Files are read newest-first (by mtime); the first valid RVOL wins, so the
    value reflects the most recent file containing the symbol (matching how
    ``load_watchlists`` orders sources).
    """
    directory = Path(directory)
    if not directory.exists():
        return {}
    files = sorted(directory.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: dict[str, float] = {}
    for f in files:
        for sym, rv in _rvol_from_file(f).items():
            out.setdefault(sym, rv)
    return out


def _sector_from_file(path: Path) -> dict[str, str]:
    """Map ticker -> broad sector from one CSV's 'Industry'/'Sector' column
    (the part before ' - ', e.g. 'Chemical - Specialty' -> 'Chemical')."""
    path = Path(path)
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return out
            fmap = {h.lower().strip(): h for h in reader.fieldnames if h}
            kcol = fmap.get("symbol") or reader.fieldnames[0]
            vcol = next((fmap[h] for h in fmap if "industry" in h or "sector" in h), None)
            if vcol is None:
                return out
            for row in reader:
                sym = (row.get(kcol) or "").strip().upper().split(" ")[0]
                if not sym or sym in out:
                    continue
                val = (row.get(vcol) or "").strip().split(" - ")[0].strip()
                if val:
                    out[sym] = val
    except OSError:
        pass
    return out


def load_sector_map(directory: Path | str) -> dict[str, str]:
    """Map ticker -> sector using the newest watchlist CSV that carries it."""
    directory = Path(directory)
    if not directory.exists():
        return {}
    files = sorted(directory.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: dict[str, str] = {}
    for f in files:
        for sym, sec in _sector_from_file(f).items():
            out.setdefault(sym, sec)
    return out


def _earnings_from_file(path: Path) -> dict[str, str]:
    """Map ticker -> next earnings/report date from a CSV column whose header
    contains 'earn' or 'report'/'report date' (e.g. Barchart's 'Next Earnings').
    The cell value is passed through as-is for display — any date format the
    screener exports just works; empty/missing column -> empty map."""
    path = Path(path)
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return out
            fmap = {h.lower().strip(): h for h in reader.fieldnames if h}
            kcol = fmap.get("symbol") or reader.fieldnames[0]
            ecol = next((fmap[h] for h in fmap
                         if "earn" in h or "report" in h), None)
            if ecol is None:
                return out
            for row in reader:
                sym = (row.get(kcol) or "").strip().upper().split(" ")[0]
                if not sym or sym in out:
                    continue
                raw = (row.get(ecol) or "").strip()
                if raw and raw not in ("-", "N/A", "NA", "0"):
                    out[sym] = raw
    except OSError:
        pass
    return out


def _wl_file_date(path: Path):
    """Parse a watchlist file's DATE from its name (e.g. momentum_24.7.2026.csv ->
    2026-07-24). Returns a date or None."""
    import re
    from datetime import date as _date
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", Path(path).name)
    if not m:
        return None
    try:
        return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def load_last_listed_map(directory: Path | str) -> dict[str, str]:
    """Map ticker -> ISO date of the NEWEST watchlist file (by its filename date)
    that still lists it. Dynamic freshness signal: how recently a symbol appeared
    in a momentum file. Recomputed from the files on disk every call."""
    directory = Path(directory)
    if not directory.exists():
        return {}
    dated = [(fd, p) for p in directory.glob("*.csv") if (fd := _wl_file_date(p))]
    dated.sort(key=lambda x: x[0])  # oldest -> newest, so the newest date wins
    out: dict[str, str] = {}
    for fd, p in dated:
        try:
            with p.open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                if not reader.fieldnames:
                    continue
                kcol = next((h for h in reader.fieldnames if h and h.lower().strip() == "symbol"),
                            reader.fieldnames[0])
                iso = fd.isoformat()
                for row in reader:
                    sym = (row.get(kcol) or "").strip().upper().split(" ")[0]
                    if sym:
                        out[sym] = iso  # newest file overwrites (loop is oldest->newest)
        except OSError:
            pass
    return out


def load_earnings_map(directory: Path | str) -> dict[str, str]:
    """Map ticker -> next earnings/report date using the newest CSV that carries
    an earnings/report column. Empty until such a column is added to the export."""
    directory = Path(directory)
    if not directory.exists():
        return {}
    files = sorted(directory.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: dict[str, str] = {}
    for f in files:
        for sym, e in _earnings_from_file(f).items():
            out.setdefault(sym, e)
    return out
