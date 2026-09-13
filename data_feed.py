"""
data_feed.py
------------
Pluggable data source for the dashboard. Right now this runs a SIMULATED
feed (random-walk prices + occasional OI surges) so the dashboard has
something live to show today. Swap `SimulatedFeed` for `KiteFeed` (stub
included below) once you hand over Kite Connect API credentials — the
dashboard/scanner code doesn't need to change, only which feed class
`app.py` instantiates.
"""
import random
import time
import threading
from datetime import datetime, timedelta
from collections import defaultdict, deque

# A small slice of the F&O universe for the demo. Replace with the real
# NSE F&O stock list fetch once live (broker APIs / NSE's own published list).
DEMO_UNIVERSE = ["RELIANCE", "HDFCBANK", "TCS", "INFY", "SBIN", "ICICIBANK"]

STRIKE_STEP = {  # rough strike spacing per stock, for realistic option chains
    "RELIANCE": 20, "HDFCBANK": 10, "TCS": 20, "INFY": 10, "SBIN": 5, "ICICIBANK": 10,
}


class SimulatedFeed:
    """Generates a random-walk price per symbol and a plausible option
    chain with occasional deliberate OI surges, so you can see the
    watchlist/setup/gamma-blast logic actually fire without live broker
    access. Also generates synthetic IV and a futures quote per symbol,
    since Gamma Blast needs both and not every real feed provides them yet."""

    def __init__(self, universe=None):
        self.universe = universe or DEMO_UNIVERSE
        self.prices = {s: random.uniform(500, 3000) for s in self.universe}
        self.candles = defaultdict(lambda: deque(maxlen=500))  # symbol -> deque of dict rows
        self.oi_state = {}  # (symbol, strike, option_type) -> current oi
        self.iv_state = {}  # (symbol, strike, option_type) -> current IV %
        self.futures_state = {}  # symbol -> {"price":.., "oi":..}
        self._init_option_chains()
        self._lock = threading.Lock()

    def _init_option_chains(self):
        for sym in self.universe:
            step = STRIKE_STEP[sym]
            base_strike = round(self.prices[sym] / step) * step
            for offset in range(-4, 5):
                strike = base_strike + offset * step
                for opt in ("CE", "PE"):
                    self.oi_state[(sym, strike, opt)] = random.randint(50_000, 300_000)
                    self.iv_state[(sym, strike, opt)] = random.uniform(18, 35)
            self.futures_state[sym] = {
                "price": self.prices[sym] * (1 + random.gauss(0, 0.001)),
                "oi": random.randint(500_000, 2_000_000),
            }

    def tick(self):
        """Advance simulated market state by one step. Call this periodically."""
        now = datetime.now()
        with self._lock:
            for sym in self.universe:
                # random-walk price, with occasional bigger moves
                move_pct = random.gauss(0, 0.15)
                if random.random() < 0.05:
                    move_pct += random.choice([-1, 1]) * random.uniform(0.5, 1.2)
                self.prices[sym] *= (1 + move_pct / 100)

                p = self.prices[sym]
                o = p * (1 + random.gauss(0, 0.05) / 100)
                h = max(o, p) * (1 + abs(random.gauss(0, 0.05)) / 100)
                l = min(o, p) * (1 - abs(random.gauss(0, 0.05)) / 100)
                vol = random.randint(20_000, 200_000)
                self.candles[sym].append({
                    "symbol": sym, "timestamp": now, "open": o, "high": h,
                    "low": l, "close": p, "volume": vol,
                })

                # update option chain OI + IV: mostly small drift, occasionally a surge
                step = STRIKE_STEP[sym]
                base_strike = round(p / step) * step
                for offset in range(-4, 5):
                    strike = base_strike + offset * step
                    for opt in ("CE", "PE"):
                        key = (sym, strike, opt)
                        current = self.oi_state.get(key, random.randint(50_000, 300_000))
                        surging = random.random() < 0.02
                        if surging:
                            current = int(current * random.uniform(2.0, 3.5))  # surge
                        else:
                            current = int(current * (1 + random.gauss(0, 0.03)))
                        self.oi_state[key] = max(1000, current)

                        iv = self.iv_state.get(key, random.uniform(18, 35))
                        iv_move = random.gauss(0, 0.5) + (random.uniform(3, 8) if surging else 0)
                        self.iv_state[key] = max(5.0, iv + iv_move)

                # futures: OI mostly drifts, occasionally short-covering pattern
                # (OI drop + price rise) to exercise the futures score component
                fut = self.futures_state[sym]
                fut["price"] = p * (1 + random.gauss(0, 0.0005))
                if random.random() < 0.03:
                    fut["oi"] = int(fut["oi"] * random.uniform(0.80, 0.90))  # short covering
                else:
                    fut["oi"] = int(fut["oi"] * (1 + random.gauss(0, 0.01)))
                fut["oi"] = max(10_000, fut["oi"])

    def get_candles(self, symbol):
        with self._lock:
            return list(self.candles[symbol])

    def get_oi_snapshot_rows(self):
        """Return current option-chain state as rows in the format
        oi_analysis.record_snapshot() expects."""
        now = datetime.now()
        rows = []
        with self._lock:
            for (sym, strike, opt), oi in self.oi_state.items():
                rows.append({
                    "symbol": sym, "expiry": "2026-09-25", "strike": strike,
                    "option_type": opt, "oi": oi,
                    "volume": random.randint(1000, 20000), "timestamp": now,
                    "iv": round(self.iv_state.get((sym, strike, opt), 20.0), 2),
                    "ltp": round(max(0.5, abs(strike - self.prices.get(sym, strike)) * 0.1
                                      + random.uniform(1, 15)), 2),
                })
        return rows

    def get_futures_quote(self, symbol):
        """Returns a dict {"price":.., "oi":..} or None. Extra to the base
        interface — gamma_blast checks for this method with getattr/hasattr
        so feeds that don't implement it (yet) just skip the futures score
        component instead of erroring."""
        with self._lock:
            fut = self.futures_state.get(symbol)
            return dict(fut) if fut else None


class KiteFeed:
    """Deprecated stub — the real implementation now lives in kite_feed.py.
    Kept only so `from data_feed import KiteFeed` doesn't hard-crash old
    imports; use `from kite_feed import KiteFeed` going forward."""
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Use kite_feed.KiteFeed instead — this stub is deprecated.")
