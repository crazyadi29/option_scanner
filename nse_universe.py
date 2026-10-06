"""
nse_universe.py
----------------
Fetches the real, current NSE F&O equity stock list (not indices) from
Fyers' publicly published symbol master CSV — no auth needed for this file,
it's just static reference data Fyers hosts for everyone.

    https://public.fyers.in/sym_details/NSE_FO.csv

This file lists every NSE F&O contract (futures + all option strikes, all
expiries) — tens of thousands of rows. We only need the unique underlying
symbols from it, so this is parsed once and cached locally; NSE only
revises the official F&O stock list quarterly, so a stale cache for a few
days is harmless.

Column layout (from Fyers' community docs — no formal header row in the
file itself):
    0 fyToken | 1 symbol description | 2 instrument type | 3 lot size
    4 tick size | 5 ISIN | 6 trading session | 7 last update
    8 expiry (epoch) | 9 tradingsymbol (e.g. "NSE:TATAMOTORS25SEP370CE")
    10 exchange | 11 segment | 12 scrip code | 13 underlying_symbol
    14 underlying scrip code | 15 strike price | 16 option type (CE/PE/blank for futures)
    17 underlying fyToken
If Fyers changes this layout, `_UNDERLYING_COL` below is the one thing to fix.
"""
import csv
import json
import os
import time
import logging

import requests

logger = logging.getLogger(__name__)

NSE_FO_CSV_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
NSE_CM_CSV_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
CACHE_FILE = "fno_universe_cache.json"
CACHE_MAX_AGE_SEC = 7 * 24 * 60 * 60  # 1 week — NSE revises the F&O list quarterly

_UNDERLYING_COL = 13
_LOT_SIZE_COL = 3      # per Fyers' documented layout above; best-effort, see fetch_lot_sizes()
_CM_TRADINGSYMBOL_COL = 13  # same position in NSE_CM.csv's row layout
_FETCH_RETRIES = 3
_FETCH_TIMEOUT_SEC = 30
_REQUEST_HEADERS = {
    # public.fyers.in has been reported (by other developers, not just here)
    # to intermittently 403/SSL-reject bare requests without a browser-like
    # User-Agent — this header alone fixes it most of the time.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

# Index underlyings that also trade F&O but aren't "stocks" — excluded since
# you asked for the F&O *stock* universe. Add/remove here if this list drifts.
INDEX_UNDERLYINGS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "SENSEX50",
}

# Fallback list used ONLY if the live fetch fails after all retries, so the
# app can still start rather than crashing outright. This is NOT guaranteed
# current — it's a reasonably representative snapshot of large/liquid F&O
# names as a safety net, not a substitute for the real fetch. A warning is
# logged whenever this path is used; fix the fetch (network/proxy/DNS) and
# restart to get the real, current list.
FALLBACK_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL",
    "KOTAKBANK", "LT", "AXISBANK", "ITC", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "ASIANPAINT", "NESTLEIND", "WIPRO",
    "ADANIENT", "ADANIPORTS", "TATASTEEL", "TATAMOTORS", "JSWSTEEL", "NTPC",
    "POWERGRID", "HCLTECH", "TECHM", "M&M", "BAJAJFINSV", "ONGC", "COALINDIA",
    "GRASIM", "DRREDDY", "CIPLA", "DIVISLAB", "EICHERMOT", "HEROMOTOCO",
    "BAJAJ-AUTO", "BRITANNIA", "HDFCLIFE", "SBILIFE", "APOLLOHOSP", "PIDILITIND",
    "DABUR", "GODREJCP", "SHREECEM", "INDUSINDBK", "UPL", "VEDANTA",
]


def _fetch_csv_text(url: str) -> str:
    last_exc = None
    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=_FETCH_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            last_exc = e
            logger.warning(f"{url} fetch attempt {attempt}/{_FETCH_RETRIES} failed: {e}")
            if attempt < _FETCH_RETRIES:
                time.sleep(2 * attempt)  # small backoff
    raise last_exc


def _fetch_valid_equity_symbols() -> set:
    """Fetches NSE's capital-market symbol master and returns the set of
    trading symbols with a real NSE:SYMBOL-EQ listing. Used to filter out
    F&O underlyings that aren't actually subscribable equities — e.g.
    "NIFTYFPI", which appears as an underlying in NSE_FO.csv for a specific
    derivative contract type but has no corresponding equity listing, and
    will make a WebSocket subscribe fail if you pass it through unfiltered.
    Returns an empty set (skips filtering) if this fetch itself fails —
    better to include a rare bad symbol than to fail the whole universe fetch
    over a secondary validation step."""
    try:
        text = _fetch_csv_text(NSE_CM_CSV_URL)
    except requests.exceptions.RequestException as e:
        logger.warning(f"Could not fetch NSE_CM.csv for equity validation ({e}) — "
                        f"skipping the cross-check, some invalid symbols may slip through.")
        return set()

    valid = set()
    reader = csv.reader(text.splitlines())
    for row in reader:
        if len(row) <= _CM_TRADINGSYMBOL_COL:
            continue
        symbol = row[_CM_TRADINGSYMBOL_COL].strip()
        if symbol:
            valid.add(symbol)
    return valid


def fetch_fo_stock_universe(force_refresh: bool = False) -> list:
    """Returns a sorted list of NSE F&O equity stock trading symbols
    (e.g. "RELIANCE", "TCS", ...). Caches locally; pass force_refresh=True
    to bypass the cache. Falls back to a static list (with a loud warning)
    if the live fetch fails after retries, so the app can still start."""
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
            if time.time() - cache.get("fetched_at", 0) < CACHE_MAX_AGE_SEC:
                logger.info(f"Using cached F&O universe ({len(cache['symbols'])} stocks).")
                return cache["symbols"]
        except (json.JSONDecodeError, KeyError):
            pass  # fall through to refetch on a corrupt cache

    logger.info(f"Fetching NSE F&O symbol master from {NSE_FO_CSV_URL} ...")
    try:
        text = _fetch_csv_text(NSE_FO_CSV_URL)
    except requests.exceptions.RequestException as e:
        logger.warning(
            f"Could not fetch the live NSE F&O list after {_FETCH_RETRIES} attempts ({e}). "
            f"Falling back to a static {len(FALLBACK_UNIVERSE)}-stock list — this may be "
            f"stale or incomplete. Fix your network/proxy and restart to get the real, "
            f"current list."
        )
        return sorted(FALLBACK_UNIVERSE)

    underlyings = set()
    lot_sizes = {}   # underlying -> lot size, best-effort (see fetch_lot_sizes docstring)
    reader = csv.reader(text.splitlines())
    for row in reader:
        if len(row) <= _UNDERLYING_COL:
            continue
        underlying = row[_UNDERLYING_COL].strip()
        if underlying and underlying not in INDEX_UNDERLYINGS:
            underlyings.add(underlying)
        if underlying and len(row) > _LOT_SIZE_COL:
            try:
                lot_sizes[underlying] = int(float(row[_LOT_SIZE_COL]))
            except (ValueError, TypeError):
                pass

    if not underlyings:
        logger.warning(
            "Parsed 0 symbols from NSE_FO.csv — Fyers likely changed the file's column "
            "layout. Falling back to the static list until _UNDERLYING_COL in "
            "nse_universe.py is fixed against a fresh sample of the CSV."
        )
        return sorted(FALLBACK_UNIVERSE)

    valid_equities = _fetch_valid_equity_symbols()
    if valid_equities:
        dropped = underlyings - valid_equities
        if dropped:
            logger.info(f"Dropping {len(dropped)} F&O underlyings with no real NSE equity "
                        f"listing (e.g. index-contract artifacts like NIFTYFPI): {sorted(dropped)}")
        underlyings = underlyings & valid_equities

    symbols = sorted(underlyings)
    if not symbols:
        logger.warning("Equity cross-check filtered out everything — likely a bug in the "
                        "cross-check itself. Falling back to the static list.")
        return sorted(FALLBACK_UNIVERSE)

    with open(CACHE_FILE, "w") as f:
        json.dump({"fetched_at": time.time(), "symbols": symbols,
                    "lot_sizes": {s: lot_sizes[s] for s in symbols if s in lot_sizes}}, f)

    logger.info(f"Fetched {len(symbols)} F&O stocks, cached to {CACHE_FILE}.")
    return symbols


def fetch_lot_sizes(force_refresh: bool = False) -> dict:
    """Returns {underlying_symbol: lot_size} for NSE F&O stocks, e.g.
    {"RELIANCE": 250, "TATAMOTORS": 1425, ...}.

    Best-effort: lot size comes from column 3 of the same NSE_FO.csv used
    for the universe list, per Fyers' documented layout (see module
    docstring). This has NOT been verified against a live response — if
    the numbers look wrong once you're running against real data (e.g.
    traded-value filters in oi_surge.py rejecting everything, or accepting
    obvious noise), that's the first thing to check. Returns {} if lot
    sizes couldn't be parsed; callers should treat that as "skip
    lot-size-dependent filtering" rather than failing outright.
    """
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
            if time.time() - cache.get("fetched_at", 0) < CACHE_MAX_AGE_SEC and cache.get("lot_sizes"):
                return cache["lot_sizes"]
        except (json.JSONDecodeError, KeyError):
            pass

    # not cached (or cache predates this feature) — trigger a fetch, which
    # populates and caches lot_sizes as a side effect, then re-read
    fetch_fo_stock_universe(force_refresh=True)
    try:
        with open(CACHE_FILE) as f:
            return json.load(f).get("lot_sizes", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
