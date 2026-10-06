"""
oi_surge.py
-----------
Completely separate from the watchlist. Whenever ANY strike (any symbol,
any moneyness) shows an OI change > config.OI_CHANGE_THRESHOLD_PCT within
the last config.OI_CHANGE_ROLLING_WINDOW_MIN minutes, AND clears the
liquidity/quality filters below, it's added to the CE-surge or PE-surge
list. No SR requirement, no watchlist requirement — this is a raw
"something big just happened" board.

Filters added after reviewing a real options-scanner screenshot (a raw
>100% OI-change rule alone fires on tiny, meaningless bases — a 1-lot to
2-lot move is technically "+100%"):
    - OI_SURGE_MIN_OI_BASE: baseline OI must be a real number, not noise
    - OI_SURGE_MIN_TRADED_VALUE: real money has to be moving (~₹10L default,
      matching the reference scanner's own "Traded ₹10 lakh+" filter)
    - OI_SURGE_MIN_VOL_OI_RATIO: today's volume must be a meaningful
      multiple of the OI base (the reference screenshot's real surges were
      mostly 4x+)
Each surge entry also carries a `build_up` classification (Long/Short
Buildup, Short Covering, Long Unwinding) — see oi_analysis.classify_build_up.
"""
from typing import List, Optional
from datetime import datetime

import config
from models import OIStrike
from oi_analysis import (
    get_significant_oi_changes, classify_build_up, get_vol_oi_ratio, get_traded_value,
)

_lot_size_cache: Optional[dict] = None


def _get_lot_size(symbol: str) -> Optional[int]:
    """Lazily loads the lot-size table once (nse_universe.fetch_lot_sizes
    does its own local caching to disk, so this just avoids re-reading
    that cache file on every single tick)."""
    global _lot_size_cache
    if _lot_size_cache is None:
        try:
            from nse_universe import fetch_lot_sizes
            _lot_size_cache = fetch_lot_sizes()
        except Exception:
            _lot_size_cache = {}
    return _lot_size_cache.get(symbol)


def _passes_quality_filters(strike: OIStrike) -> bool:
    if not strike.oi_prev_baseline or strike.oi_prev_baseline < config.OI_SURGE_MIN_OI_BASE:
        return False

    ratio = get_vol_oi_ratio(strike)
    if ratio is None or ratio < config.OI_SURGE_MIN_VOL_OI_RATIO:
        return False

    traded_value = get_traded_value(strike, _get_lot_size(strike.symbol))
    # If we couldn't compute a traded value (most likely: lot size lookup
    # failed for this symbol — see nse_universe.fetch_lot_sizes' own
    # caveat about being best-effort), don't silently hide the whole
    # surge board over it — apply the filter only when we actually have
    # the data to apply it. The OI-base and vol/OI-ratio floors above
    # still gate out the obvious noise either way.
    if traded_value is not None and traded_value < config.OI_SURGE_MIN_TRADED_VALUE:
        return False

    return True


class SurgeBoard:
    def __init__(self):
        self.ce_surges: list = []   # list of dicts: strike_obj, change_pct, build_up, vol_oi_ratio, traded_value, detected_at
        self.pe_surges: list = []

    def _already_listed(self, board: list, symbol: str, strike: float) -> bool:
        return any(item["strike_obj"].symbol == symbol and item["strike_obj"].strike == strike
                   for item in board)

    def scan(self, all_strikes: List[OIStrike]):
        """Call once per tick with the full universe's current strikes."""
        flagged = get_significant_oi_changes(all_strikes)
        flagged = [s for s in flagged if _passes_quality_filters(s)]

        for s in flagged:
            board = self.ce_surges if s.option_type == "CE" else self.pe_surges
            entry_data = {
                "strike_obj": s,
                "change_pct": s.oi_change_pct,
                "build_up": classify_build_up(s),
                "vol_oi_ratio": get_vol_oi_ratio(s),
                "traded_value": get_traded_value(s, _get_lot_size(s.symbol)),
            }
            if self._already_listed(board, s.symbol, s.strike):
                for item in board:
                    if item["strike_obj"].symbol == s.symbol and item["strike_obj"].strike == s.strike:
                        item.update(entry_data)
                        item["detected_at"] = datetime.now()
                continue
            entry_data["detected_at"] = datetime.now()
            board.append(entry_data)

        self.ce_surges = self.ce_surges[-config.WATCHLIST_MAX_SIZE:]
        self.pe_surges = self.pe_surges[-config.WATCHLIST_MAX_SIZE:]

    def get_ce_surges(self):
        return sorted(self.ce_surges, key=lambda i: i["detected_at"], reverse=True)

    def get_pe_surges(self):
        return sorted(self.pe_surges, key=lambda i: i["detected_at"], reverse=True)
