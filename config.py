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
# Gamma Blast strategy — scoring-based gamma squeeze detector
# ---------------------------------------------------------------------------
GAMMA_WINDOW_MIN = 15  # all components measured over this rolling window, per your spec

# Your weights: momentum 20, gamma concentration 20, IV 15, futures 10, expiry 10 = 75.
# They don't sum to 100 — GAMMA_NORMALIZE_WEIGHTS rescales them proportionally so the
# final score still lands on a 0-100 scale. Set this False (and adjust the weights
# below to sum to 100 yourself) if you actually intended a 6th unlisted factor
# instead of a rescale.
GAMMA_NORMALIZE_WEIGHTS = True
GAMMA_WEIGHT_MOMENTUM = 20
GAMMA_WEIGHT_GAMMA_CONCENTRATION = 20
GAMMA_WEIGHT_IV = 15
GAMMA_WEIGHT_FUTURES = 10
GAMMA_WEIGHT_EXPIRY = 10

# Component normalization thresholds — the input value that maps to a full
# 100 on that component's own 0-100 scale, before weighting.
GAMMA_MOMENTUM_PRICE_PCT_FOR_100 = 1.5     # 1.5% price move in the window = full marks
GAMMA_MOMENTUM_VOLUME_MULT_FOR_100 = 4.0   # 4x average volume = full marks
GAMMA_CONCENTRATION_SHARE_FOR_100 = 0.35   # near-money OI = 35% of that side's total chain OI = full marks
GAMMA_IV_PCT_CHANGE_FOR_100 = 20.0         # +20% IV (or premium proxy) change in window = full marks
GAMMA_FUTURES_OI_DROP_FOR_100 = 15.0       # futures OI down 15% + price up = full short-covering marks
GAMMA_EXPIRY_DAYS_FOR_ZERO = 7             # 7+ days to expiry = 0 on this component
GAMMA_EXPIRY_DAYS_FOR_100 = 0              # expiry day itself = full marks

# Alert level thresholds (your spec)
GAMMA_ALERT_STRONG = 80    # 80-100: strong gamma-squeeze setup
GAMMA_ALERT_WATCH = 65     # 65-79: watch closely
GAMMA_ALERT_DEVELOPING = 50  # 50-64: developing; below this = no alert

GAMMA_TELEGRAM_THRESHOLD = 80   # only scores >= this trigger a Telegram push (per your sequence diagram)
GAMMA_TELEGRAM_RE_ALERT_COOLDOWN_MIN = 30  # don't re-push the same symbol within this window
                                            # even if it stays above threshold
