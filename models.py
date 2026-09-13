"""
models.py
---------
Plain dataclasses used across the scanner. Keeping these decoupled from
any broker SDK means you can swap Kite / Upstox / a CSV file underneath
without touching the strategy logic.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Candle:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SRLevel:
    symbol: str
    price: float
    kind: str          # "support" | "resistance"
    touches: int
    last_touch: datetime


@dataclass
class OIStrike:
    symbol: str
    expiry: str
    strike: float
    option_type: str   # "CE" | "PE"
    oi: int
    oi_prev_baseline: Optional[int] = None
    volume: Optional[int] = None
    timestamp: Optional[datetime] = None
    iv: Optional[float] = None       # implied volatility, % — populated where the broker provides it
    ltp: Optional[float] = None      # option premium, used as an IV proxy when iv is unavailable

    @property
    def oi_change_pct(self) -> Optional[float]:
        if not self.oi_prev_baseline or self.oi_prev_baseline == 0:
            return None
        return ((self.oi - self.oi_prev_baseline) / self.oi_prev_baseline) * 100.0


@dataclass
class FutureQuote:
    symbol: str
    price: float
    oi: int
    oi_prev_baseline: Optional[int] = None
    price_prev_baseline: Optional[float] = None
    timestamp: Optional[datetime] = None

    @property
    def oi_change_pct(self) -> Optional[float]:
        if not self.oi_prev_baseline or self.oi_prev_baseline == 0:
            return None
        return ((self.oi - self.oi_prev_baseline) / self.oi_prev_baseline) * 100.0

    @property
    def price_change_pct(self) -> Optional[float]:
        if not self.price_prev_baseline:
            return None
        return ((self.price - self.price_prev_baseline) / self.price_prev_baseline) * 100.0


@dataclass
class GammaScore:
    symbol: str
    total_score: float
    components: dict          # {"momentum":.., "gamma_concentration":.., "iv":.., "futures":.., "expiry":..}
    alert_level: str          # "strong" | "watch" | "developing" | "none"
    price: float
    key_strike: Optional[OIStrike] = None
    futures: Optional[FutureQuote] = None
    computed_at: datetime = None
    setup_note: str = ""


@dataclass
class WatchlistEntry:
    symbol: str
    matched_strike: OIStrike
    added_at: datetime
    sr_level: Optional[SRLevel] = None   # None for pure OI-surge entries with no nearby SR
    reason: str = ""
    status: str = "watching"   # "watching" | "confirmed" | "setup" | "expired"


@dataclass
class TradeSetup:
    symbol: str
    direction: str      # "long" | "short"
    entry_zone: float
    sr_level: SRLevel
    triggering_strike: OIStrike
    confirmations: list = field(default_factory=list)
    generated_at: datetime = None
