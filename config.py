"""
config.py
---------
All the "subjective" thresholds from the strategy doc live here as objective,
tunable numbers. Change these to backtest different definitions of
"important S/R", "close to", "high OI" and "significant OI change".

Nothing else in the codebase should hardcode a threshold — always import
from here so backtest and live scanner stay in sync.
"""

# ---------------------------------------------------------------------------
# Support / Resistance detection
# ---------------------------------------------------------------------------
SR_METHOD = "zigzag_pct"     # "zigzag_pct" | "swing" | "pivot_classic" | "prior_day_hl"
SR_TIMEFRAME_MIN = 5         # candle timeframe in minutes for swing detection
ZIGZAG_PCT = 1.0             # a swing point is confirmed once price reverses
                              # by this % from the last swing point (ZigZag logic)
SWING_LOOKBACK_DAYS = 20     # window to find swing highs/lows (legacy "swing" method)
SWING_ORDER = 3              # legacy "swing" method param, unused by zigzag_pct
MIN_TOUCHES_FOR_IMPORTANT = 1   # a level must be touched >= this many times
                                 # in the lookback window to count as "important".
                                 # zigzag_pct pivots are already significant moves,
                                 # so 1 is a reasonable default; raise to 2+ if you
                                 # want only levels price has revisited.

# ---------------------------------------------------------------------------
# "Close to" a level  (used both for price-vs-SR and SR-vs-OI-strike matching)
# ---------------------------------------------------------------------------
PROXIMITY_MODE = "pct"       # "pct" | "atr" | "absolute"
PROXIMITY_PCT = 0.5          # within 0.5% of price is "close" (if mode=pct)
PROXIMITY_ATR_MULT = 0.5     # within 0.5 * ATR(14) (if mode=atr)
PROXIMITY_ABSOLUTE = 2.0     # absolute rupee distance (if mode=absolute)

# ---------------------------------------------------------------------------
# High OI strike definition
# ---------------------------------------------------------------------------
HIGH_OI_TOP_N = 3            # consider top N strikes by OI on each side (CE/PE)
HIGH_OI_MIN_SHARE = 0.08     # OR: a strike must hold >= 8% of total OI on its
                              # side to qualify as "high OI" (whichever mode below)
HIGH_OI_MODE = "top_n"       # "top_n" | "min_share"

# ---------------------------------------------------------------------------
# Significant OI change (your rule: >100% change on PE or CE side,
# measured over a rolling 15-min window)
# ---------------------------------------------------------------------------
OI_CHANGE_THRESHOLD_PCT = 100.0   # % change that counts as "significant"
OI_CHANGE_BASELINE = "rolling_15min"  # "prev_day_close_oi" | "session_open_oi" | "rolling_15min"
OI_CHANGE_ROLLING_WINDOW_MIN = 15  # only used if BASELINE == "rolling_15min"
OI_CHANGE_LOOKUP_TOLERANCE_MIN = 3  # how far off-target a stored snapshot can be
                                     # and still be used as the "15 min ago" baseline

# ---------------------------------------------------------------------------
# OI Surge quality filters — added after reviewing a real scanner screenshot:
# your raw >100% rule alone fires on 1-lot-to-2-lot noise (e.g. a contract
# with 1 lot of OI going to 2 lots is technically "+100%" but meaningless).
# These filters require real liquidity before something counts as a surge.
# ---------------------------------------------------------------------------
OI_SURGE_MIN_OI_BASE = 100        # baseline OI must be at least this many (lots or shares,
                                   # matches whatever unit your broker's OI field uses)
OI_SURGE_MIN_TRADED_VALUE = 1_000_000   # ~₹10L, per the reference scanner's "Traded ₹10 lakh+" filter
OI_SURGE_MIN_VOL_OI_RATIO = 4.0   # today's volume must be >= 4x the OI baseline (screenshot showed
                                   # 8 of 11 real surges at 4x+; treat this as a starting point, tune
                                   # once you're comparing against real days)

# Fyers' optionchain `volume` field is in LOTS (contracts), not raw shares —
# cross-checked against a real scanner screenshot: NIFTY 21100 PE showed
# volume=33973, ltp=1.20(close), traded=₹19.4L. shares-mode gives ₹40.7K
# (off by ~47x, clearly wrong); lots-mode with a 75 lot size gives ₹30.6L,
# the right order of magnitude — the remaining gap is expected, since their
# "Traded ₹" is a volume-weighted average price across the day while we
# only have the closing LTP (down 14.3% that day, so the day's average
# price was higher than the close). Still worth re-checking against a live
# response once you're running this for real, but this is now
# evidence-based rather than a guess.
VOLUME_UNIT = "lots"  # "shares" | "lots"

# ---------------------------------------------------------------------------
# Confirmation conditions (checked only after a significant OI change fires)
# ---------------------------------------------------------------------------
CONFIRM_VOLUME_MULT = 1.5     # current volume >= 1.5x average volume
CONFIRM_VOLUME_AVG_PERIOD = 20
CONFIRM_MIN_DELTA_CANDLES = 3  # N consecutive candles with net positive/negative
                                # order-flow delta in the expected direction
CONFIRM_REQUIRE_PRICE_REACTION = True  # candle must actually react at the level
                                        # (wick rejection / engulfing at S/R)

# ---------------------------------------------------------------------------
# Watchlist / setup housekeeping
# ---------------------------------------------------------------------------
WATCHLIST_MAX_SIZE = 50
SETUP_VALID_FOR_MIN = 60      # a generated setup expires after N minutes if untraded

# ---------------------------------------------------------------------------
# Gamma Blast strategy v2 — short-covering / stop-loss-hunt squeeze detector
# ---------------------------------------------------------------------------
# Logic: spot near an SR level -> look at the ATM + next strike on the
# matching side (CE for resistance, PE for support) -> if that strike has
# the highest OI among the near-money set (real buildup) -> watch its
# 15-min trend for OI falling + volume rising + premium rising together
# across GAMMA_TREND_CANDLES consecutive candles (sellers getting stopped
# out and covering). If the trend is NOT monotonic across all those candles,
# score is 0 — this is a trend confirmation, not just a net change.
GAMMA_WINDOW_MIN = 15                # candle size for the trend, per your spec
GAMMA_TREND_CANDLES = 3              # consecutive candles required to confirm the trend
GAMMA_SR_PROXIMITY_PCT = 0.5         # how close price must be to an SR level to gate a setup

# Weights (sum to 100) — you didn't specify a split, so these are my
# defaults with reasoning: OI unwind is the actual confirming event
# (sellers exiting), volume confirms it's real activity, premium is the
# lagging/result signal. Change freely.
GAMMA_NORMALIZE_WEIGHTS = True
GAMMA_WEIGHT_OI_UNWIND = 40
GAMMA_WEIGHT_VOLUME_RISE = 35
GAMMA_WEIGHT_PREMIUM_RISE = 25

# Component normalization — the total change across the full
# GAMMA_TREND_CANDLES window that maps to a full 100 on that component's
# own scale, before weighting (only applies once the monotonicity gate passes)
GAMMA_OI_UNWIND_PCT_FOR_100 = 30.0      # OI down 30% across the window = full marks
GAMMA_VOLUME_RISE_MULT_FOR_100 = 3.0    # period volume tripling = full marks
GAMMA_PREMIUM_RISE_PCT_FOR_100 = 25.0   # premium up 25% across the window = full marks

# Alert level thresholds (unchanged from before)
GAMMA_ALERT_STRONG = 80
GAMMA_ALERT_WATCH = 65
GAMMA_ALERT_DEVELOPING = 50

GAMMA_TELEGRAM_THRESHOLD = 80
GAMMA_TELEGRAM_RE_ALERT_COOLDOWN_MIN = 30
