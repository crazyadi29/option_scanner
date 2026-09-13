"""
watchlist.py
------------
Watchlist rule (as of your latest instructions):

  CALL side:  a resistance level is near current price
              AND there is OI buildup at one of the near-money CE strikes
              (ITM/ATM/OTM, i.e. the strike nearest the spot price and its
              two immediate neighbors) => add to watchlist, CE side.

  PUT side:   a support level is near current price
              AND there is OI buildup at one of the near-money PE strikes
              => add to watchlist, PE side.

"OI buildup" = the near-money strike is also one of the top-OI strikes for
that side (config.HIGH_OI_TOP_N) — i.e. real open interest concentration,
not just proximity to spot.

Note: OI SURGE (>100% change in 15 min) is now a completely separate
concern — see oi_surge.py. It no longer feeds the watchlist; it has its own
CE-surge / PE-surge sections in the dashboard.
"""
from typing import List, Optional
from datetime import datetime

import config
from models import SRLevel, OIStrike, WatchlistEntry
from oi_analysis import get_near_money_strikes


class Watchlist:
    def __init__(self):
        self.entries: List[WatchlistEntry] = []

    def _nearby_level(self, sr_levels: List[SRLevel], current_price: float,
                       kind: str) -> Optional[SRLevel]:
        proximity = current_price * (config.PROXIMITY_PCT / 100.0)
        candidates = [l for l in sr_levels
                      if l.kind == kind and abs(l.price - current_price) <= proximity]
        if not candidates:
            return None
        return min(candidates, key=lambda l: abs(l.price - current_price))

    def _buildup_strike(self, near_money: List[OIStrike],
                         high_oi: List[OIStrike]) -> Optional[OIStrike]:
        high_oi_strikes = {s.strike for s in high_oi}
        matches = [s for s in near_money if s.strike in high_oi_strikes]
        if not matches:
            return None
        return max(matches, key=lambda s: s.oi)

    def _already_watching(self, symbol: str, option_type: str) -> bool:
        return any(
            e.symbol == symbol and e.matched_strike.option_type == option_type
            and e.status == "watching"
            for e in self.entries
        )

    def scan_symbol(self, symbol: str, current_price: float,
                     sr_levels: List[SRLevel],
                     near_money_ce: List[OIStrike], near_money_pe: List[OIStrike],
                     high_oi_ce: List[OIStrike], high_oi_pe: List[OIStrike]) -> List[WatchlistEntry]:
        new_entries = []

        # CALL side: resistance nearby + CE buildup at near-money strike
        resistance = self._nearby_level(sr_levels, current_price, "resistance")
        ce_strike = self._buildup_strike(near_money_ce, high_oi_ce)
        if resistance and ce_strike and not self._already_watching(symbol, "CE"):
            reason = (f"Resistance at {resistance.price} nearby "
                      f"({current_price:.1f} spot) + CE OI buildup at strike "
                      f"{ce_strike.strike} (OI={ce_strike.oi:,})")
            new_entries.append(WatchlistEntry(
                symbol=symbol, sr_level=resistance, matched_strike=ce_strike,
                added_at=datetime.now(), reason=reason,
            ))

        # PUT side: support nearby + PE buildup at near-money strike
        support = self._nearby_level(sr_levels, current_price, "support")
        pe_strike = self._buildup_strike(near_money_pe, high_oi_pe)
        if support and pe_strike and not self._already_watching(symbol, "PE"):
            reason = (f"Support at {support.price} nearby "
                      f"({current_price:.1f} spot) + PE OI buildup at strike "
                      f"{pe_strike.strike} (OI={pe_strike.oi:,})")
            new_entries.append(WatchlistEntry(
                symbol=symbol, sr_level=support, matched_strike=pe_strike,
                added_at=datetime.now(), reason=reason,
            ))

        if new_entries:
            self.entries.extend(new_entries)
            self.entries = self.entries[-config.WATCHLIST_MAX_SIZE:]
        return new_entries

    def get_active(self) -> List[WatchlistEntry]:
        return [e for e in self.entries if e.status == "watching"]

    def mark_confirmed(self, entry: WatchlistEntry):
        entry.status = "confirmed"
