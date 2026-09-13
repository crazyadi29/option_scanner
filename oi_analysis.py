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

# Time-series store: {(symbol, expiry, strike, option_type): [(timestamp, oi), ...]}
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
    OIStrike objects for this snapshot (with rolling-window baseline attached
    if enough history exists yet)."""
    strikes = []
    for r in raw_rows:
        k = _key({
            "symbol": r[COLUMN_MAP["symbol"]], "expiry": r[COLUMN_MAP["expiry"]],
            "strike": r[COLUMN_MAP["strike"]], "option_type": r[COLUMN_MAP["option_type"]],
        })
        ts = _ensure_dt(r[COLUMN_MAP["timestamp"]])
        oi = r[COLUMN_MAP["oi"]]

        _history_store.setdefault(k, [])
        _history_store[k].append((ts, oi))
        _history_store[k].sort(key=lambda x: x[0])

        baseline = _lookup_rolling_baseline(k, ts)

        strikes.append(OIStrike(
            symbol=r[COLUMN_MAP["symbol"]], expiry=r[COLUMN_MAP["expiry"]],
            strike=r[COLUMN_MAP["strike"]], option_type=r[COLUMN_MAP["option_type"]],
            oi=oi, oi_prev_baseline=baseline,
            volume=r.get(COLUMN_MAP["volume"]), timestamp=ts,
            iv=r.get("iv"), ltp=r.get("ltp"),
        ))
    return strikes


def _lookup_rolling_baseline(key: tuple, current_ts: datetime) -> Optional[int]:
    """Find the OI value closest to `current_ts - window` for this strike,
    within OI_CHANGE_LOOKUP_TOLERANCE_MIN minutes tolerance."""
    history = _history_store.get(key, [])
    if len(history) < 2:
        return None

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
    for ts, oi in candidates:
        diff = abs((ts - target_ts).total_seconds())
        if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
            best, best_diff = oi, diff

    return best


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
