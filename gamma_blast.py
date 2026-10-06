"""
gamma_blast.py
--------------
Gamma Blast v2 — short-covering / stop-loss-hunt squeeze detector.

Logic:
    1. GATE: spot price must be near an important support or resistance level.
    2. Direction: near resistance -> watch the CALL side. Near support ->
       watch the PUT side.
    3. GATE: among the ATM strike and the next strike out (OTM direction),
       the one with the higher OI is the "buildup strike" — this is where
       option sellers are concentrated.
    4. Trend confirmation, over GAMMA_TREND_CANDLES (default 3) consecutive
       15-min candles: OI falling + volume rising + premium rising, all
       three, every candle-to-candle step. This is the signature of sellers
       getting stopped out and buying back to cover (OI unwinds as volume
       and premium spike) — the actual gamma squeeze.
    5. If the trend isn't monotonic across all those candles, score is 0 —
       this scores trend CONFIRMATION, not just net change over the window.

Score = weighted blend of how much OI fell / volume rose / premium rose
across the confirmed window (config.GAMMA_WEIGHT_*).

Data note: this needs per-strike OI/volume/premium(ltp) history sampled
over time, which this module builds itself (see _history) from whatever
OIStrike objects you pass to compute_score() each tick — no separate feed
wiring needed beyond what's already flowing through the app.
"""
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from bisect import bisect_left

import config
from models import Candle, SRLevel, OIStrike, GammaScore

# (symbol, strike, option_type) -> [(timestamp, oi, volume, ltp), ...] sorted by time
_history: Dict[tuple, list] = {}

_last_telegram_alert: Dict[str, datetime] = {}


def _record(key: tuple, ts: datetime, oi, volume, ltp):
    _history.setdefault(key, [])
    _history[key].append((ts, oi, volume, ltp))
    cutoff = ts - timedelta(minutes=config.GAMMA_WINDOW_MIN * (config.GAMMA_TREND_CANDLES + 2))
    _history[key] = [h for h in _history[key] if h[0] >= cutoff]


def _bucket_end_values(key: tuple, now: datetime):
    """Returns up to (GAMMA_TREND_CANDLES + 1) values, one per completed
    GAMMA_WINDOW_MIN-minute bucket ending at or before `now`, each as
    (bucket_end_ts, oi, cumulative_volume, ltp) using the last observation
    within that bucket. Returns fewer if there isn't enough history yet."""
    history = _history.get(key, [])
    if len(history) < 2:
        return []

    n_buckets = config.GAMMA_TREND_CANDLES + 1
    window = timedelta(minutes=config.GAMMA_WINDOW_MIN)

    # bucket boundaries: ..., now-2*window, now-window, now
    boundaries = [now - window * i for i in range(n_buckets, -1, -1)]
    results = []
    for i in range(len(boundaries) - 1):
        b_start, b_end = boundaries[i], boundaries[i + 1]
        in_bucket = [h for h in history if b_start < h[0] <= b_end]
        if not in_bucket:
            continue
        last = max(in_bucket, key=lambda h: h[0])
        results.append((b_end, last[1], last[2], last[3]))
    return results


def _pct_change(old, new):
    if old is None or old == 0 or new is None:
        return None
    return ((new - old) / old) * 100.0


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _find_nearby_level(sr_levels: List[SRLevel], current_price: float, kind: str) -> Optional[SRLevel]:
    proximity = current_price * (config.GAMMA_SR_PROXIMITY_PCT / 100.0)
    candidates = [l for l in sr_levels if l.kind == kind and abs(l.price - current_price) <= proximity]
    if not candidates:
        return None
    return min(candidates, key=lambda l: abs(l.price - current_price))


def _pick_buildup_strike(near_money: List[OIStrike], current_price: float,
                          side: str) -> Optional[OIStrike]:
    """ATM + the next strike out (OTM direction for that side); returns
    whichever of those two has the higher OI — that's the buildup strike."""
    if not near_money:
        return None
    sorted_strikes = sorted(near_money, key=lambda s: s.strike)
    atm = min(sorted_strikes, key=lambda s: abs(s.strike - current_price))
    idx = sorted_strikes.index(atm)

    if side == "CE":
        next_strike = sorted_strikes[idx + 1] if idx + 1 < len(sorted_strikes) else None
    else:  # PE — OTM direction is downward
        next_strike = sorted_strikes[idx - 1] if idx - 1 >= 0 else None

    candidates = [s for s in (atm, next_strike) if s is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.oi)


def _score_trend(key: tuple, now: datetime):
    """Returns (oi_score, volume_score, premium_score, debug_dict) or all
    zeros if there isn't enough history or the trend isn't monotonic across
    every consecutive candle."""
    buckets = _bucket_end_values(key, now)
    if len(buckets) < config.GAMMA_TREND_CANDLES + 1:
        return 0.0, 0.0, 0.0, {"reason": "insufficient history"}

    # last N+1 buckets -> N candle-to-candle steps
    buckets = buckets[-(config.GAMMA_TREND_CANDLES + 1):]

    oi_series = [b[1] for b in buckets]
    vol_cum_series = [b[2] for b in buckets]
    ltp_series = [b[3] for b in buckets]

    # per-bucket traded volume = delta of cumulative volume (Fyers/Kite both
    # report cumulative-for-the-day volume, not per-candle)
    vol_period_series = [vol_cum_series[i] - vol_cum_series[i - 1] for i in range(1, len(vol_cum_series))]

    oi_steps_down = all(oi_series[i] < oi_series[i - 1] for i in range(1, len(oi_series)))
    vol_steps_up = all(vol_period_series[i] > vol_period_series[i - 1] for i in range(1, len(vol_period_series))) \
        if len(vol_period_series) > 1 else (len(vol_period_series) == 1)
    premium_steps_up = all(ltp_series[i] > ltp_series[i - 1] for i in range(1, len(ltp_series)))

    debug = {
        "oi_series": oi_series, "vol_period_series": vol_period_series, "ltp_series": ltp_series,
        "oi_monotonic_down": oi_steps_down, "vol_monotonic_up": vol_steps_up,
        "premium_monotonic_up": premium_steps_up,
    }

    if not (oi_steps_down and vol_steps_up and premium_steps_up):
        return 0.0, 0.0, 0.0, debug

    oi_pct = _pct_change(oi_series[0], oi_series[-1]) or 0
    oi_score = _clamp(abs(min(oi_pct, 0)) / config.GAMMA_OI_UNWIND_PCT_FOR_100 * 100)

    vol_mult = (vol_period_series[-1] / vol_period_series[0]) if vol_period_series[0] > 0 else 0
    vol_score = _clamp(vol_mult / config.GAMMA_VOLUME_RISE_MULT_FOR_100 * 100)

    premium_pct = _pct_change(ltp_series[0], ltp_series[-1]) or 0
    premium_score = _clamp(max(premium_pct, 0) / config.GAMMA_PREMIUM_RISE_PCT_FOR_100 * 100)

    return oi_score, vol_score, premium_score, debug


def compute_score(symbol: str, candles: List[Candle], sr_levels: List[SRLevel],
                   near_money_ce: List[OIStrike], near_money_pe: List[OIStrike]) -> Optional[GammaScore]:
    if not candles:
        return None
    # Use the data's own timestamp as "now", not wall-clock time — this is
    # what makes the module correct for both live use and backtesting/
    # fast-forwarded simulation, where candle timestamps don't match real time.
    now = candles[-1].timestamp
    current_price = candles[-1].close

    # record every near-money strike's current reading, regardless of
    # whether a setup is active right now — history needs to accumulate
    # continuously so it's ready when a gate does pass
    for s in (near_money_ce + near_money_pe):
        _record((symbol, s.strike, s.option_type), s.timestamp or now, s.oi, s.volume or 0, s.ltp)

    resistance = _find_nearby_level(sr_levels, current_price, "resistance")
    support = _find_nearby_level(sr_levels, current_price, "support")

    if resistance is not None:
        side, level, near_money = "CE", resistance, near_money_ce
    elif support is not None:
        side, level, near_money = "PE", support, near_money_pe
    else:
        return None  # gate 1 failed: not near any SR level

    buildup_strike = _pick_buildup_strike(near_money, current_price, side)
    if buildup_strike is None:
        return None  # gate 2 failed: no usable near-money strike

    key = (symbol, buildup_strike.strike, buildup_strike.option_type)
    oi_score, vol_score, premium_score, debug = _score_trend(key, now)

    weights = {
        "oi_unwind": config.GAMMA_WEIGHT_OI_UNWIND,
        "volume_rise": config.GAMMA_WEIGHT_VOLUME_RISE,
        "premium_rise": config.GAMMA_WEIGHT_PREMIUM_RISE,
    }
    total_weight = sum(weights.values())
    scale = (100.0 / total_weight) if config.GAMMA_NORMALIZE_WEIGHTS else 1.0

    components = {"oi_unwind": oi_score, "volume_rise": vol_score, "premium_rise": premium_score}
    total_score = sum(components[k] * weights[k] for k in weights) / 100.0 * scale
    total_score = round(_clamp(total_score), 1)

    if total_score >= config.GAMMA_ALERT_STRONG:
        alert_level = "strong"
    elif total_score >= config.GAMMA_ALERT_WATCH:
        alert_level = "watch"
    elif total_score >= config.GAMMA_ALERT_DEVELOPING:
        alert_level = "developing"
    else:
        alert_level = "none"

    setup_note = f"{level.kind.capitalize()} {level.price:.1f} nearby, {side} buildup at {buildup_strike.strike:.0f}"

    return GammaScore(
        symbol=symbol, total_score=total_score, components=components,
        alert_level=alert_level, price=current_price, key_strike=buildup_strike,
        futures=None, computed_at=now, setup_note=setup_note,
    )


def should_send_telegram_alert(score: GammaScore) -> bool:
    if score.total_score < config.GAMMA_TELEGRAM_THRESHOLD:
        return False
    last = _last_telegram_alert.get(score.symbol)
    if last and (datetime.now() - last) < timedelta(minutes=config.GAMMA_TELEGRAM_RE_ALERT_COOLDOWN_MIN):
        return False
    _last_telegram_alert[score.symbol] = datetime.now()
    return True
