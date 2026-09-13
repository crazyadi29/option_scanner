"""
data_loader.py
---------------
Loads your OI data (and candle data) from disk so you can test the scanner
before wiring a live broker feed. Supports CSV for now — tell me your
actual file format/columns and I'll adjust this file specifically.

Expected OI CSV columns (rename via oi_analysis.COLUMN_MAP if yours differ):
    symbol, expiry, strike, option_type, oi, volume, timestamp

Expected candle CSV columns:
    symbol, timestamp, open, high, low, close, volume
"""
import pandas as pd
from typing import List
from datetime import datetime

from models import Candle


def load_oi_csv(path: str) -> List[dict]:
    df = pd.read_csv(path)
    return df.to_dict(orient="records")


def load_candles_csv(path: str, symbol: str = None) -> List[Candle]:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    if symbol:
        df = df[df["symbol"] == symbol]
    return [
        Candle(
            symbol=row["symbol"], timestamp=row["timestamp"],
            open=row["open"], high=row["high"], low=row["low"],
            close=row["close"], volume=row["volume"],
        )
        for _, row in df.iterrows()
    ]
