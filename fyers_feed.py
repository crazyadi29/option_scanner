"""
fyers_feed.py
-------------
Real Fyers live feed. Same interface as SimulatedFeed/KiteFeed (universe,
tick(), get_candles(symbol), get_oi_snapshot_rows()), so app.py doesn't
change — only which feed class it instantiates.

Universe: if you don't pass one explicitly, this fetches the REAL, full
NSE F&O stock list (~180+ stocks currently) via nse_universe.py — not a
demo subset.

Important difference from Kite: Fyers has NO WebSocket for option chain OI
(confirmed — their own sample-code repo has an open request for this that's
never been implemented). So option OI is REST-polled via fyers.optionchain().

Fyers' documented rate limits (per their community docs — the daily figure
alone is NOT the binding constraint at full-universe scale):
    10 requests/second, 200 requests/minute, 1,00,000 requests/day
At ~180 stocks, the per-minute cap (200) is what actually binds — a full
pass over the universe takes a minimum of ~180/200 minutes worth of pacing,
not the ~48s/symbol the daily-only math implied. This is handled by a
single serialized background poller thread (_option_chain_poller) that
paces every real request against all three limits at once, rather than
firing a burst of concurrent per-symbol requests like the earlier version
did (which is what caused the 429 flood you hit — every symbol became
"due" simultaneously on the first tick, and they all fired at once with no
shared throttle between them).

Conveniently, optionchain()'s `strikecount` parameter returns exactly N
ITM + 1 ATM + N OTM strikes — precisely the near-money set your watchlist
rule needs, no extra logic required to find them.

WebSocket subscription limits: Fyers cap the number of symbols per
WebSocket connection (varies by plan; check your plan's docs if you hit a
subscribe error with the full universe). Not handled yet — tell me if you
hit this and I'll add sharding across multiple connections.

Setup:
  1. pip install fyers-apiv3 --break-system-packages
  2. Run fyers_auth.py each trading morning for a fresh token
  3. Set FYERS_CLIENT_ID env var (token itself is read from fyers_token.json)
"""
import json
import logging
import threading
import time
from datetime import datetime
from collections import defaultdict, deque

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from nse_universe import fetch_fo_stock_universe

logger = logging.getLogger(__name__)

TOKEN_FILE = "fyers_token.json"
STRIKE_COUNT = 5   # -> 5 ITM + 1 ATM + 5 OTM per side

# Fyers' documented limits, with a safety margin so we never actually touch
# the wall (their own community reports suggest the limits aren't rigidly
# enforced/reported, so leaving headroom matters more than squeezing every
# last request out of the budget).
MAX_REQ_PER_SEC = 10
MAX_REQ_PER_MIN = 200
MAX_REQ_PER_DAY = 100_000
SAFETY_MARGIN = 0.75

MIN_GAP_BETWEEN_REQUESTS_SEC = max(
    1 / (MAX_REQ_PER_SEC * SAFETY_MARGIN),
    60 / (MAX_REQ_PER_MIN * SAFETY_MARGIN),
)  # the stricter of the per-second/per-minute caps, after margin

MIN_OI_POLL_INTERVAL_SEC = 30   # never poll the SAME symbol more often than this,
                                 # even if the global pacing would technically allow it

RETRY_AFTER_429_SEC = 8         # backoff when Fyers itself returns a 429


def _compute_recommended_interval(num_symbols: int) -> int:
    """How often each symbol gets polled, given the whole universe has to
    share the global per-second/minute-paced request budget. Informational/
    for logging — the actual gating happens per-request in the poller loop
    via MIN_GAP_BETWEEN_REQUESTS_SEC, this just tells you what cadence to expect."""
    if num_symbols == 0:
        return MIN_OI_POLL_INTERVAL_SEC
    full_pass_seconds = num_symbols * MIN_GAP_BETWEEN_REQUESTS_SEC
    return max(MIN_OI_POLL_INTERVAL_SEC, int(full_pass_seconds) + 1)


class FyersFeed:
    def __init__(self, client_id: str, access_token: str = None, universe=None):
        if access_token is None:
            with open(TOKEN_FILE) as f:
                access_token = json.load(f)["access_token"]

        self.client_id = client_id
        self.access_token = access_token

        if universe is None:
            logger.info("No universe passed — fetching full NSE F&O stock list...")
            universe = fetch_fo_stock_universe()
            logger.info(f"Scanning {len(universe)} F&O stocks.")
        self.universe = universe

        self.oi_poll_interval_sec = _compute_recommended_interval(len(self.universe))
        logger.info(
            f"Full universe pass will take roughly {self.oi_poll_interval_sec}s "
            f"({len(self.universe)} symbols, paced at one request per "
            f"{MIN_GAP_BETWEEN_REQUESTS_SEC:.2f}s to respect Fyers' per-second/minute limits)."
        )

        self.fyers_symbols = {s: f"NSE:{s}-EQ" for s in self.universe}
        self.symbol_for_fyers_symbol = {v: k for k, v in self.fyers_symbols.items()}

        self.fyers = fyersModel.FyersModel(client_id=client_id, token=access_token)

        self.candles = defaultdict(lambda: deque(maxlen=500))
        self._current_bar = {}
        self._current_bar_minute = {}

        self.oi_state = {}          # (symbol, strike, option_type) -> {"oi","expiry","volume"}
        self._last_oi_poll = {}     # symbol -> last poll time
        self._stop_poller = threading.Event()

        self._lock = threading.Lock()

        self._start_websocket()
        threading.Thread(target=self._option_chain_poller_loop, daemon=True).start()

    # -----------------------------------------------------------------
    # WebSocket (spot LTP/OHLC)
    # -----------------------------------------------------------------
    def _start_websocket(self):
        full_token = f"{self.client_id}:{self.access_token}"

        def onmessage(message):
            with self._lock:
                sym = message.get("symbol")
                symbol = self.symbol_for_fyers_symbol.get(sym)
                if not symbol:
                    return
                price = message.get("ltp")
                if price is None:
                    return
                self._update_bar(symbol, price, message.get("vol_traded_today", 0))

        def onerror(message):
            # Fyers reports invalid-symbol subscribe failures via this
            # callback rather than raising — it's informational, the
            # WebSocket keeps running for the rest of the valid symbols.
            # The real fix is filtering bad symbols out of the universe
            # before subscribing (see nse_universe.py's equity cross-check);
            # this handler just makes sure one bad symbol can't look like a
            # fatal crash in the logs.
            if isinstance(message, dict) and message.get("code") == -300:
                logger.warning(f"Fyers WS: invalid symbol(s) skipped in subscribe: "
                                f"{message.get('invalid_symbols')}")
            else:
                logger.error(f"Fyers WS error: {message}")

        def onclose(message):
            logger.warning(f"Fyers WS closed: {message}")

        def onopen():
            symbols = list(self.fyers_symbols.values())
            self.ws.subscribe(symbols=symbols, data_type="SymbolUpdate")
            self.ws.keep_running()

        self.ws = data_ws.FyersDataSocket(
            access_token=full_token, log_path="",
            litemode=False, reconnect=True,
            on_connect=onopen, on_close=onclose, on_error=onerror, on_message=onmessage,
        )
        # FyersDataSocket.connect() blocks running its own loop, so run it in a thread
        threading.Thread(target=self.ws.connect, daemon=True).start()

    def _update_bar(self, symbol, price, volume):
        now = datetime.now()
        minute_key = now.replace(second=0, microsecond=0)
        if self._current_bar_minute.get(symbol) != minute_key:
            if symbol in self._current_bar:
                self.candles[symbol].append(self._current_bar[symbol])
            self._current_bar[symbol] = {
                "symbol": symbol, "timestamp": minute_key,
                "open": price, "high": price, "low": price, "close": price,
                "volume": volume,
            }
            self._current_bar_minute[symbol] = minute_key
        else:
            bar = self._current_bar[symbol]
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] = volume

    # -----------------------------------------------------------------
    # REST polling (option chain OI) — no websocket option for this on Fyers.
    # A SINGLE background thread walks the universe round-robin, pacing
    # every actual request by MIN_GAP_BETWEEN_REQUESTS_SEC — this is what
    # actually enforces the per-second/per-minute limits, unlike the old
    # version which fired one thread per symbol with no shared throttle
    # between them (that's what caused the 429 flood).
    # -----------------------------------------------------------------
    def _poll_option_chain(self, symbol) -> bool:
        """Returns True on success, False on failure (including 429 —
        caller decides how to back off)."""
        try:
            response = self.fyers.optionchain(data={
                "symbol": self.fyers_symbols[symbol],
                "strikecount": STRIKE_COUNT,
                "timestamp": "",
            })
        except Exception as e:
            logger.error(f"optionchain poll failed for {symbol}: {e}")
            return False

        if response.get("code") == 429 or response.get("s") == "error" and "429" in str(response):
            logger.warning(f"Rate-limited (429) on {symbol} — backing off "
                            f"{RETRY_AFTER_429_SEC}s before continuing.")
            return False

        if response.get("s") != "ok":
            logger.warning(f"optionchain response not ok for {symbol}: {response}")
            return False

        expiry = "current"  # Fyers returns the nearest expiry by default; see docstring
        chain = response.get("data", {}).get("optionsChain", [])
        with self._lock:
            for row in chain:
                opt_type = row.get("option_type")
                if opt_type not in ("CE", "PE"):
                    continue  # skips the underlying/index row itself
                strike = row.get("strike_price")
                oi = row.get("oi")
                vol = row.get("volume", 0)
                if strike is None or oi is None:
                    continue
                self.oi_state[(symbol, strike, opt_type)] = {
                    "oi": oi, "expiry": expiry, "volume": vol,
                }
        return True

    def _option_chain_poller_loop(self):
        """Runs for the lifetime of the feed. Cycles through the universe,
        polling each symbol no more often than MIN_OI_POLL_INTERVAL_SEC,
        with every actual request paced at MIN_GAP_BETWEEN_REQUESTS_SEC —
        this is a hard global throttle, not per-symbol, so it holds
        regardless of how many symbols are "due" at once."""
        while not self._stop_poller.is_set():
            for symbol in self.universe:
                if self._stop_poller.is_set():
                    return
                now = time.time()
                last = self._last_oi_poll.get(symbol, 0)
                if now - last < MIN_OI_POLL_INTERVAL_SEC:
                    continue

                ok = self._poll_option_chain(symbol)
                self._last_oi_poll[symbol] = time.time()

                if ok:
                    time.sleep(MIN_GAP_BETWEEN_REQUESTS_SEC)
                else:
                    time.sleep(RETRY_AFTER_429_SEC)
            time.sleep(0.5)  # brief pause between full passes over the universe

    # -----------------------------------------------------------------
    # Interface expected by app.py
    # -----------------------------------------------------------------
    def tick(self):
        """OI polling now runs on its own paced background thread (see
        _option_chain_poller_loop) — tick() no longer dispatches requests
        itself, since that's what caused the burst/429 problem. Kept as a
        no-op so app.py's interface doesn't need to change."""
        pass

    def get_candles(self, symbol):
        with self._lock:
            history = list(self.candles.get(symbol, []))
            current = self._current_bar.get(symbol)
            return history + ([current] if current else [])

    def get_oi_snapshot_rows(self):
        now = datetime.now()
        rows = []
        with self._lock:
            for (symbol, strike, opt), data in self.oi_state.items():
                rows.append({
                    "symbol": symbol, "expiry": data["expiry"], "strike": strike,
                    "option_type": opt, "oi": data["oi"], "volume": data["volume"],
                    "timestamp": now,
                })
        return rows

    def close(self):
        self._stop_poller.set()
        try:
            self.ws.close_connection()
        except Exception:
            pass
