"""
gamma_blast.py
--------------
Gamma Blast strategy: scoring-based gamma-squeeze detector.

    NSE F&O data -> option-chain scanner -> Gamma/OI/Volume/IV analysis
    -> Gamma Score (0-100) -> filter Score >= 80 -> Telegram alert

Score components (your weights, auto-normalized to sum to 100 —
see config.GAMMA_NORMALIZE_WEIGHTS):
    Price + volume momentum         20%
    Near-ATM gamma concentration    20%
    IV expansion                    15%
    Short covering / futures OI     10%
    Expiry proximity                10%

All components are measured over a rolling config.GAMMA_WINDOW_MIN (15 min)
window, same rolling-history pattern as the OI surge tracker.

Data availability note: real IV and futures OI/price aren't wired into
every feed yet.
    - SimulatedFeed: generates synthetic IV and futures data so this whole
      module is testable end-to-end today.
    - KiteFeed: doesn't currently pull IV or futures quotes — falls back to
      the option premium (ltp) as an IV proxy, and the futures component
      scores 0 until wired up.
    - FyersFeed: optionchain() can return IV directly if requested with
      greeks — not yet requested in fyers_feed.py; same fallback applies
      until that's added.
This is flagged in-code (see `iv or ltp` fallback below) so nothing pretends
to have data it doesn't.
"""
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from bisect import bisect_left

import config
from models import Candle, OIStrike, FutureQuote, GammaScore

# rolling history: key -> [(timestamp, value), ...]
_price_history: Dict[str, list] = {}
_volume_history: Dict[str, list] = {}
_near_oi_history: Dict[str, list] = {}          # symbol -> [(ts, near_money_oi_sum)]
_iv_history: Dict[tuple, list] = {}             # (symbol, strike, option_type) -> [(ts, iv_or_premium)]
_futures_oi_history: Dict[str, list] = {}
_futures_price_history: Dict[str, list] = {}

_last_telegram_alert: Dict[str, datetime] = {}  # symbol -> last time we pushed a Telegram alert


def _record(store: dict, key, ts: datetime, value):
    store.setdefault(key, [])
    store[key].append((ts, value))
    # keep ~2 windows of history, drop older
    cutoff = ts - timedelta(minutes=config.GAMMA_WINDOW_MIN * 3)
    store[key] = [(t, v) for t, v in store[key] if t >= cutoff]


def _lookback_value(store: dict, key, current_ts: datetime, window_min: int):
    history = store.get(key, [])
    if len(history) < 2:
        return None
    target = current_ts - timedelta(minutes=window_min)
    timestamps = [h[0] for h in history]
    pos = bisect_left(timestamps, target)
    candidates = []
    if pos < len(history):
        candidates.append(history[pos])
    if pos > 0:
        candidates.append(history[pos - 1])
    if not candidates:
        return None
    best = min(candidates, key=lambda c: abs((c[0] - target).total_seconds()))
    return best[1]


def _pct_change(old, new):
    if old is None or old == 0:
        return None
    return ((new - old) / old) * 100.0


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Component scorers — each returns 0-100
# ---------------------------------------------------------------------------

def _score_momentum(symbol: str, current_price: float, current_volume: float,
                     avg_volume: Optional[float], now: datetime) -> float:
    _record(_price_history, symbol, now, current_price)
    old_price = _lookback_value(_price_history, symbol, now, config.GAMMA_WINDOW_MIN)
    price_pct = abs(_pct_change(old_price, current_price) or 0)
    price_component = _clamp(price_pct / config.GAMMA_MOMENTUM_PRICE_PCT_FOR_100 * 100)

    vol_mult = (current_volume / avg_volume) if avg_volume else 0
    volume_component = _clamp(vol_mult / config.GAMMA_MOMENTUM_VOLUME_MULT_FOR_100 * 100)

    return (price_component + volume_component) / 2


def _score_gamma_concentration(symbol: str, near_money_ce: List[OIStrike],
                                near_money_pe: List[OIStrike],
                                all_ce: List[OIStrike], all_pe: List[OIStrike],
                                now: datetime) -> float:
    near_oi = sum(s.oi for s in near_money_ce) + sum(s.oi for s in near_money_pe)
    total_oi = sum(s.oi for s in all_ce) + sum(s.oi for s in all_pe)
    if total_oi == 0:
        return 0.0
    share = near_oi / total_oi
    return _clamp(share / config.GAMMA_CONCENTRATION_SHARE_FOR_100 * 100)


def _score_iv_expansion(symbol: str, near_money_ce: List[OIStrike],
                         near_money_pe: List[OIStrike], now: datetime) -> float:
    strikes = near_money_ce + near_money_pe
    if not strikes:
        return 0.0
    values = [(s.iv if s.iv is not None else s.ltp) for s in strikes]
    values = [v for v in values if v is not None]
    if not values:
        return 0.0
    current_avg = sum(values) / len(values)

    key = symbol
    _record(_iv_history, key, now, current_avg)
    old_avg = _lookback_value(_iv_history, key, now, config.GAMMA_WINDOW_MIN)
    pct = _pct_change(old_avg, current_avg)
    if pct is None:
        return 0.0
    return _clamp(pct / config.GAMMA_IV_PCT_CHANGE_FOR_100 * 100)


def _score_futures(symbol: str, futures: Optional[FutureQuote], now: datetime) -> float:
    if futures is None:
        return 0.0
    _record(_futures_oi_history, symbol, now, futures.oi)
    _record(_futures_price_history, symbol, now, futures.price)

    old_oi = _lookback_value(_futures_oi_history, symbol, now, config.GAMMA_WINDOW_MIN)
    old_price = _lookback_value(_futures_price_history, symbol, now, config.GAMMA_WINDOW_MIN)
    oi_pct = _pct_change(old_oi, futures.oi)
    price_pct = _pct_change(old_price, futures.price)
    if oi_pct is None or price_pct is None:
        return 0.0

    # short covering signature: OI falling while price rising
    if oi_pct < 0 and price_pct > 0:
        drop_component = _clamp(abs(oi_pct) / config.GAMMA_FUTURES_OI_DROP_FOR_100 * 100)
        return drop_component
    return 0.0


def _score_expiry(expiry_str: str, now: datetime) -> float:
    try:
        expiry_date = datetime.fromisoformat(expiry_str).date()
    except (ValueError, TypeError):
        return 0.0  # unknown/placeholder expiry (e.g. Fyers "current") — can't score this component
    days_left = (expiry_date - now.date()).days
    if days_left <= config.GAMMA_EXPIRY_DAYS_FOR_100:
        return 100.0
    if days_left >= config.GAMMA_EXPIRY_DAYS_FOR_ZERO:
        return 0.0
    span = config.GAMMA_EXPIRY_DAYS_FOR_ZERO - config.GAMMA_EXPIRY_DAYS_FOR_100
    return _clamp((config.GAMMA_EXPIRY_DAYS_FOR_ZERO - days_left) / span * 100)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_score(symbol: str, candles: List[Candle],
                   near_money_ce: List[OIStrike], near_money_pe: List[OIStrike],
                   all_ce: List[OIStrike], all_pe: List[OIStrike],
                   futures: Optional[FutureQuote] = None,
                   avg_volume: Optional[float] = None) -> Optional[GammaScore]:
    if not candles:
        return None
    now = datetime.now()
    current_price = candles[-1].close
    current_volume = candles[-1].volume

    momentum = _score_momentum(symbol, current_price, current_volume, avg_volume, now)
    gamma_conc = _score_gamma_concentration(symbol, near_money_ce, near_money_pe, all_ce, all_pe, now)
    iv = _score_iv_expansion(symbol, near_money_ce, near_money_pe, now)
    futures_score = _score_futures(symbol, futures, now)

    expiry_str = (near_money_ce[0].expiry if near_money_ce
                  else near_money_pe[0].expiry if near_money_pe else None)
    expiry = _score_expiry(expiry_str, now) if expiry_str else 0.0

    weights = {
        "momentum": config.GAMMA_WEIGHT_MOMENTUM,
        "gamma_concentration": config.GAMMA_WEIGHT_GAMMA_CONCENTRATION,
        "iv": config.GAMMA_WEIGHT_IV,
        "futures": config.GAMMA_WEIGHT_FUTURES,
        "expiry": config.GAMMA_WEIGHT_EXPIRY,
    }
    total_weight = sum(weights.values())
    scale = (100.0 / total_weight) if config.GAMMA_NORMALIZE_WEIGHTS else 1.0

    components = {
        "momentum": momentum, "gamma_concentration": gamma_conc,
        "iv": iv, "futures": futures_score, "expiry": expiry,
    }
    total_score = sum(components[k] * weights[k] for k in weights) / 100.0 * scale
    total_score = round(_clamp(total_score), 1)

    if total_score >= config.GAMMA_ALERT_STRONG:
        level = "strong"
    elif total_score >= config.GAMMA_ALERT_WATCH:
        level = "watch"
    elif total_score >= config.GAMMA_ALERT_DEVELOPING:
        level = "developing"
    else:
        level = "none"

    # key strike: highest-OI near-money strike on whichever side is stronger
    all_near = near_money_ce + near_money_pe
    key_strike = max(all_near, key=lambda s: s.oi) if all_near else None

    return GammaScore(
        symbol=symbol, total_score=total_score, components=components,
        alert_level=level, price=current_price, key_strike=key_strike,
        futures=futures, computed_at=now,
    )


def should_send_telegram_alert(score: GammaScore) -> bool:
    if score.total_score < config.GAMMA_TELEGRAM_THRESHOLD:
        return False
    last = _last_telegram_alert.get(score.symbol)
    if last and (datetime.now() - last) < timedelta(minutes=config.GAMMA_TELEGRAM_RE_ALERT_COOLDOWN_MIN):
        return False
    _last_telegram_alert[score.symbol] = datetime.now()
    return True
