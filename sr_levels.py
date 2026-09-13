"""
sr_levels.py
------------
Computes support/resistance levels from OHLC candle history.

Method (config.SR_METHOD == "swing"):
  A candle's high is a swing high if it is the max high among
  `SWING_ORDER` candles on either side. Same logic (min low) for swing lows.
  Levels within PROXIMITY of each other are merged/clustered, and a level's
  "touches" count is how many times price came back within proximity of it.
  Only levels with touches >= MIN_TOUCHES_FOR_IMPORTANT are called "important".
"""
from typing import List
from datetime import datetime
import pandas as pd

import config
from models import Candle, SRLevel


def _to_df(candles: List[Candle]) -> pd.DataFrame:
    df = pd.DataFrame([c.__dict__ for c in candles])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _proximity_value(price: float, atr: float = None) -> float:
    if config.PROXIMITY_MODE == "pct":
        return price * (config.PROXIMITY_PCT / 100.0)
    elif config.PROXIMITY_MODE == "atr":
        return (atr or 0) * config.PROXIMITY_ATR_MULT
    else:
        return config.PROXIMITY_ABSOLUTE


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]


def find_zigzag_points(df: pd.DataFrame, pct: float) -> List[dict]:
    """
    Classic % ZigZag: walk through candles tracking the running high/low.
    A swing HIGH is confirmed once price subsequently drops `pct`% from that
    high (that high becomes a resistance point). A swing LOW is confirmed
    once price subsequently rises `pct`% from that low (support point).
    Uses candle high/low (not just close) to catch intra-candle extremes.
    """
    if df.empty:
        return []

    points = []
    threshold = pct / 100.0

    # state: direction we're currently tracking ("up" looking for high, "down" looking for low)
    last_pivot_price = df.iloc[0]["close"]
    last_pivot_idx = 0
    direction = None  # unknown until first confirmed move

    running_high = df.iloc[0]["high"]
    running_high_idx = 0
    running_low = df.iloc[0]["low"]
    running_low_idx = 0

    for i in range(1, len(df)):
        row = df.iloc[i]

        if row["high"] > running_high:
            running_high = row["high"]
            running_high_idx = i
        if row["low"] < running_low:
            running_low = row["low"]
            running_low_idx = i

        if direction in (None, "up"):
            # looking for a swing high: check if price has dropped `pct` from running_high
            if running_high > 0 and (running_high - row["low"]) / running_high >= threshold:
                points.append({"price": running_high, "kind": "resistance",
                                "timestamp": df.iloc[running_high_idx]["timestamp"]})
                direction = "down"
                running_low = row["low"]
                running_low_idx = i

        if direction in (None, "down"):
            # looking for a swing low: check if price has risen `pct` from running_low
            if running_low > 0 and (row["high"] - running_low) / running_low >= threshold:
                points.append({"price": running_low, "kind": "support",
                                "timestamp": df.iloc[running_low_idx]["timestamp"]})
                direction = "up"
                running_high = row["high"]
                running_high_idx = i

    return points


def resample_to_timeframe(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    df = df.set_index("timestamp")
    agg = df.resample(f"{minutes}min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    agg = agg.reset_index()
    return agg


def find_swing_points(df: pd.DataFrame, order: int) -> List[dict]:
    points = []
    n = len(df)
    for i in range(order, n - order):
        window = df.iloc[i - order: i + order + 1]
        row = df.iloc[i]
        if row["high"] == window["high"].max():
            points.append({"price": row["high"], "kind": "resistance",
                            "timestamp": row["timestamp"]})
        if row["low"] == window["low"].min():
            points.append({"price": row["low"], "kind": "support",
                            "timestamp": row["timestamp"]})
    return points


def cluster_levels(points: List[dict], proximity: float) -> List[dict]:
    """Merge nearby swing points of the same kind into single levels,
    counting touches."""
    clustered = []
    for kind in ("support", "resistance"):
        kind_points = sorted([p for p in points if p["kind"] == kind],
                              key=lambda p: p["price"])
        cluster = []
        for p in kind_points:
            if not cluster or abs(p["price"] - cluster[-1]["price"]) <= proximity:
                cluster.append(p)
            else:
                clustered.append(_merge_cluster(cluster, kind))
                cluster = [p]
        if cluster:
            clustered.append(_merge_cluster(cluster, kind))
    return clustered


def _merge_cluster(cluster: List[dict], kind: str) -> dict:
    avg_price = sum(p["price"] for p in cluster) / len(cluster)
    last_touch = max(p["timestamp"] for p in cluster)
    return {"price": avg_price, "kind": kind, "touches": len(cluster),
            "last_touch": last_touch}


def get_important_levels(symbol: str, candles: List[Candle]) -> List[SRLevel]:
    """
    Main entry point. Returns only levels that meet
    config.MIN_TOUCHES_FOR_IMPORTANT.
    """
    if len(candles) < 5:
        return []

    df = _to_df(candles)

    if config.SR_METHOD == "zigzag_pct":
        df = resample_to_timeframe(df, config.SR_TIMEFRAME_MIN)
        atr = _atr(df)
        current_price = df.iloc[-1]["close"]
        proximity = _proximity_value(current_price, atr)
        raw_points = find_zigzag_points(df, config.ZIGZAG_PCT)
    else:
        lookback = df.tail(config.SWING_LOOKBACK_DAYS) if config.SWING_LOOKBACK_DAYS else df
        atr = _atr(df)
        current_price = df.iloc[-1]["close"]
        proximity = _proximity_value(current_price, atr)
        raw_points = find_swing_points(lookback, config.SWING_ORDER)

    merged = cluster_levels(raw_points, proximity)

    important = [
        SRLevel(symbol=symbol, price=round(m["price"], 2), kind=m["kind"],
                touches=m["touches"], last_touch=m["last_touch"])
        for m in merged
        if m["touches"] >= config.MIN_TOUCHES_FOR_IMPORTANT
    ]
    important.sort(key=lambda lvl: abs(lvl.price - current_price))
    return important
