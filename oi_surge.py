"""
oi_surge.py
-----------
Completely separate from the watchlist. Whenever ANY strike (any symbol,
any moneyness) shows an OI change > config.OI_CHANGE_THRESHOLD_PCT within
the last config.OI_CHANGE_ROLLING_WINDOW_MIN minutes, it's added straight
to the CE-surge or PE-surge list. No SR requirement, no watchlist
requirement — this is a raw "something big just happened" board.
"""
from typing import List
from datetime import datetime

import config
from models import OIStrike
from oi_analysis import get_significant_oi_changes


class SurgeBoard:
    def __init__(self):
        self.ce_surges: list = []   # list of dicts: {strike: OIStrike, detected_at, change_pct}
        self.pe_surges: list = []

    def _already_listed(self, board: list, symbol: str, strike: float) -> bool:
        return any(item["strike_obj"].symbol == symbol and item["strike_obj"].strike == strike
                   for item in board)

    def scan(self, all_strikes: List[OIStrike]):
        """Call once per tick with the full universe's current strikes."""
        flagged = get_significant_oi_changes(all_strikes)
        for s in flagged:
            board = self.ce_surges if s.option_type == "CE" else self.pe_surges
            if self._already_listed(board, s.symbol, s.strike):
                # update in place with latest numbers
                for item in board:
                    if item["strike_obj"].symbol == s.symbol and item["strike_obj"].strike == s.strike:
                        item["strike_obj"] = s
                        item["change_pct"] = s.oi_change_pct
                        item["detected_at"] = datetime.now()
                continue
            entry = {"strike_obj": s, "change_pct": s.oi_change_pct, "detected_at": datetime.now()}
            board.append(entry)

        self.ce_surges = self.ce_surges[-config.WATCHLIST_MAX_SIZE:]
        self.pe_surges = self.pe_surges[-config.WATCHLIST_MAX_SIZE:]

    def get_ce_surges(self):
        return sorted(self.ce_surges, key=lambda i: i["detected_at"], reverse=True)

    def get_pe_surges(self):
        return sorted(self.pe_surges, key=lambda i: i["detected_at"], reverse=True)
