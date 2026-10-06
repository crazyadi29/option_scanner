"""
oi_analysis.py
--------------
Ingests option-chain OI snapshots over time and:
  1. Identifies "high OI" strikes per symbol/expiry/side (top N, config.HIGH_OI_TOP_N).
  2. Tracks OI change % against a rolling window baseline (default: 15 min ago,
     config.OI_CHANGE_ROLLING_WINDOW_MIN) and flags "significant" moves
     (your rule: > 100% change on either CE or PE side within that window).

You need to call `record_snapshot()` every time you pull a fresh option
chain (e.g. every 1-5 min from your broker), and the history builds up
automatically. `get_significant_oi_changes()` then looks back
`OI_CHANGE_ROLLING_WINDOW_MIN` minutes from the latest snapshot's timestamp
to find the comparison baseline.

Expected input format for record_snapshot():
    A list of dicts / DataFrame rows, one per strike, with columns:
    symbol, expiry, strike, option_type (CE/PE), oi, volume, timestamp

If your data source (broker or CSV) uses different column names, adjust
`COLUMN_MAP` below rather than rewriting the logic.
"""
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from bisect import bisect_left

import config
from models import OIStrike

COLUMN_MAP = {
    "symbol": "symbol",
    "expiry": "expiry",
    "strike": "strike",
    "option_type": "option_type",
    "oi": "oi",
    "volume": "volume",
    "timestamp": "timestamp",
}

# Time-series store: {(symbol, expiry, strike, option_type): [(timestamp, oi, ltp), ...]}
# sorted ascending by timestamp. Swap for a real DB (SQLite/Redis/Timescale)
# once you go live — fine for backtesting / early paper trading as-is.
_history_store: Dict[tuple, list] = {}


def _key(row) -> tuple:
    return (row["symbol"], row["expiry"], row["strike"], row["option_type"])


def _ensure_dt(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts))


def record_snapshot(raw_rows) -> List[OIStrike]:
    """Ingest one option-chain snapshot, append to history, and return
    OIStrike objects for this snapshot (with rolling-window OI and premium
    baselines attached if enough history exists yet)."""
    strikes = []
    for r in raw_rows:
        k = _key({
            "symbol": r[COLUMN_MAP["symbol"]], "expiry": r[COLUMN_MAP["expiry"]],
            "strike": r[COLUMN_MAP["strike"]], "option_type": r[COLUMN_MAP["option_type"]],
        })
        ts = _ensure_dt(r[COLUMN_MAP["timestamp"]])
        oi = r[COLUMN_MAP["oi"]]
        ltp = r.get("ltp")

        _history_store.setdefault(k, [])
        _history_store[k].append((ts, oi, ltp))
        _history_store[k].sort(key=lambda x: x[0])

        oi_baseline, ltp_baseline = _lookup_rolling_baseline(k, ts)

        strikes.append(OIStrike(
            symbol=r[COLUMN_MAP["symbol"]], expiry=r[COLUMN_MAP["expiry"]],
            strike=r[COLUMN_MAP["strike"]], option_type=r[COLUMN_MAP["option_type"]],
            oi=oi, oi_prev_baseline=oi_baseline,
            volume=r.get(COLUMN_MAP["volume"]), timestamp=ts,
            iv=r.get("iv"), ltp=ltp, ltp_prev_baseline=ltp_baseline,
        ))
    return strikes


def _lookup_rolling_baseline(key: tuple, current_ts: datetime):
    """Find the OI and ltp values closest to `current_ts - window` for this
    strike, within OI_CHANGE_LOOKUP_TOLERANCE_MIN minutes tolerance.
    Returns (oi_baseline, ltp_baseline), either of which may be None."""
    history = _history_store.get(key, [])
    if len(history) < 2:
        return None, None

    target_ts = current_ts - timedelta(minutes=config.OI_CHANGE_ROLLING_WINDOW_MIN)
    tolerance = timedelta(minutes=config.OI_CHANGE_LOOKUP_TOLERANCE_MIN)

    timestamps = [h[0] for h in history]
    pos = bisect_left(timestamps, target_ts)

    candidates = []
    if pos < len(history):
        candidates.append(history[pos])
    if pos > 0:
        candidates.append(history[pos - 1])

    best = None
    best_diff = None
    for entry in candidates:
        ts = entry[0]
        diff = abs((ts - target_ts).total_seconds())
        if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
            best, best_diff = entry, diff

    if best is None:
        return None, None
    return best[1], best[2]


# Backward-compatible alias used by scanner.py for an initial baseline load
def load_oi_snapshot(raw_rows) -> List[OIStrike]:
    return record_snapshot(raw_rows)


def set_baseline(oi_rows: List[OIStrike]) -> None:
    """Legacy helper — with rolling_15min baseline mode this isn't needed;
    kept as a no-op-safe wrapper in case OI_CHANGE_BASELINE is switched back
    to a fixed session/prev-day baseline."""
    pass


def get_near_money_strikes(strikes: List[OIStrike], symbol: str, expiry: str,
                            option_type: str, spot_price: float) -> List[OIStrike]:
    """Return up to 3 strikes around the current price for one side:
    the nearest strike (ATM) plus its immediate neighbor above and below.
    Used for the 'OI buildup at ITM/ATM/OTM' watchlist rule — deliberately
    independent of get_high_oi_strikes (that's for the top-OI ranking;
    this is purely about which strikes are near the money)."""
    side = sorted(
        [s for s in strikes if s.symbol == symbol and s.expiry == expiry
         and s.option_type == option_type],
        key=lambda s: s.strike,
    )
    if not side:
        return []

    atm = min(side, key=lambda s: abs(s.strike - spot_price))
    idx = side.index(atm)
    result = [atm]
    if idx > 0:
        result.append(side[idx - 1])
    if idx < len(side) - 1:
        result.append(side[idx + 1])
    return result


def get_high_oi_strikes(strikes: List[OIStrike], symbol: str, expiry: str,
                         option_type: str) -> List[OIStrike]:
    """Return the strikes considered 'high OI' per config settings
    (default: top 3 by OI), for one symbol/expiry/side."""
    side = [s for s in strikes
            if s.symbol == symbol and s.expiry == expiry
            and s.option_type == option_type]
    if not side:
        return []
    side.sort(key=lambda s: s.oi, reverse=True)

    if config.HIGH_OI_MODE == "top_n":
        return side[:config.HIGH_OI_TOP_N]
    else:
        total = sum(s.oi for s in side)
        return [s for s in side if total > 0 and (s.oi / total) >= config.HIGH_OI_MIN_SHARE]


def get_significant_oi_changes(strikes: List[OIStrike]) -> List[OIStrike]:
    """Filter strikes whose OI change vs the rolling baseline exceeds
    config.OI_CHANGE_THRESHOLD_PCT (your >100% in last 15 min rule)."""
    flagged = []
    for s in strikes:
        pct = s.oi_change_pct
        if pct is not None and abs(pct) >= config.OI_CHANGE_THRESHOLD_PCT:
            flagged.append(s)
    return flagged


def classify_build_up(strike: OIStrike) -> Optional[str]:
    """Classic OI/price build-up classification, from OI change direction
    x premium change direction (same logic every options scanner uses —
    this is what the "Build-up" column in your screenshot is):
        OI up   + premium up   -> "Long Buildup"    (buyers adding)
        OI up   + premium down -> "Short Buildup"   (writers adding)
        OI down + premium up   -> "Short Covering"  (writers exiting)
        OI down + premium down -> "Long Unwinding"  (buyers exiting)
    Returns None if there isn't enough history yet to classify (needs both
    an OI and a premium baseline from the rolling window)."""
    oi_pct = strike.oi_change_pct
    ltp_pct = strike.ltp_change_pct
    if oi_pct is None or ltp_pct is None:
        return None
    if oi_pct >= 0 and ltp_pct >= 0:
        return "Long Buildup"
    if oi_pct >= 0 and ltp_pct < 0:
        return "Short Buildup"
    if oi_pct < 0 and ltp_pct >= 0:
        return "Short Covering"
    return "Long Unwinding"


def get_vol_oi_ratio(strike: OIStrike) -> Optional[float]:
    """Today's traded volume divided by the OI baseline from the rolling
    window (proxy for 'volume vs prior OI' — same idea as the Vol÷OI
    column in your screenshot, though that one uses yesterday's close OI
    specifically; ours uses the same rolling baseline as everything else
    here for consistency). Returns None if volume or a baseline is missing."""
    if strike.volume is None or not strike.oi_prev_baseline:
        return None
    return strike.volume / strike.oi_prev_baseline


def get_traded_value(strike: OIStrike, lot_size: Optional[int]) -> Optional[float]:
    """Rough traded value in rupees for this strike today: volume * premium,
    scaled by lot size if config.VOLUME_UNIT == "lots" (i.e. Fyers' volume
    field is contracts, not shares — UNVERIFIED, see config.py comment).
    Returns None if inputs are missing, so callers can decide whether to
    skip the traded-value filter entirely rather than wrongly reject a
    strike over incomplete data."""
    if strike.volume is None or strike.ltp is None:
        return None
    if config.VOLUME_UNIT == "lots":
        if not lot_size:
            return None  # can't compute traded value without a lot size in this mode
        return strike.volume * lot_size * strike.ltp
    return strike.volume * strike.ltp
