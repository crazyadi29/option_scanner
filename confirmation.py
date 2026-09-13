"""
confirmation.py
----------------
Once a watchlist entry sees a significant OI change (config threshold),
these checks decide whether it graduates to a TradeSetup.

Implements:
  - volume confirmation (current vol >= CONFIRM_VOLUME_MULT * avg vol)
  - price reaction at the level (wick rejection / engulfing)
Delta/order-flow confirmation is stubbed — plug in your order-flow feed
(most brokers don't give true bid/ask delta over API; if you have a
separate provider for this, wire it into `check_delta_confirmation`).
"""
from typing import List
import pandas as pd

import config
from models import Candle, WatchlistEntry, TradeSetup


def check_volume_confirmation(candles: List[Candle]) -> bool:
    if len(candles) < config.CONFIRM_VOLUME_AVG_PERIOD + 1:
        return False
    vols = [c.volume for c in candles]
    avg = sum(vols[-config.CONFIRM_VOLUME_AVG_PERIOD - 1:-1]) / config.CONFIRM_VOLUME_AVG_PERIOD
    current = vols[-1]
    return avg > 0 and current >= config.CONFIRM_VOLUME_MULT * avg


def check_price_reaction(candles: List[Candle], level_price: float, kind: str) -> bool:
    """Very simple reaction check: last candle wicks through the level but
    closes back on the correct side (rejection candle)."""
    if not candles:
        return False
    c = candles[-1]
    proximity = level_price * (config.PROXIMITY_PCT / 100.0)

    if kind == "support":
        touched = c.low <= level_price + proximity
        rejected = c.close > level_price
        return touched and rejected
    else:  # resistance
        touched = c.high >= level_price - proximity
        rejected = c.close < level_price
        return touched and rejected


def check_delta_confirmation(order_flow_data=None) -> bool:
    """Stub — wire in real order-flow / footprint data here if you have a
    source for it. Returns True (pass-through) until implemented so it
    doesn't silently block setups; flip to False-by-default once wired."""
    return True


def evaluate(entry: WatchlistEntry, candles: List[Candle]) -> TradeSetup | None:
    # Direction/level: prefer the matched SR level; if this entry came purely
    # from an OI surge with no nearby SR level, infer from option side
    # (PE surge => support-side bias/long bias, CE surge => resistance/short bias).
    if entry.sr_level is not None:
        kind = entry.sr_level.kind
        level_price = entry.sr_level.price
    else:
        kind = "support" if entry.matched_strike.option_type == "PE" else "resistance"
        level_price = entry.matched_strike.strike

    checks = {
        "volume": check_volume_confirmation(candles),
        "price_reaction": check_price_reaction(
            candles, level_price, kind
        ) if config.CONFIRM_REQUIRE_PRICE_REACTION else True,
        "delta": check_delta_confirmation(),
    }

    if all(checks.values()):
        direction = "long" if kind == "support" else "short"
        return TradeSetup(
            symbol=entry.symbol,
            direction=direction,
            entry_zone=level_price,
            sr_level=entry.sr_level,
            triggering_strike=entry.matched_strike,
            confirmations=[k for k, v in checks.items() if v],
        )
    return None
