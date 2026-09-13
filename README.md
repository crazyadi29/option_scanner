# F&O S/R + OI Scanner — Live Dashboard (3 tabs)

## Run it
```
python3 -m pip install flask pandas --break-system-packages
python3 app.py
```
Open http://localhost:5000

## What's new in this version

### Tab 1: Scanner
Full F&O universe (intraday + positional together, one list) — symbol,
LTP, nearest support, nearest resistance. Click any row (or use the
dropdown) to see that stock's live chart with SR levels drawn as dashed
lines and top CE/PE OI strikes marked as dots on the right edge.

### Tab 2: Watchlist
Rule (per your latest instructions): a symbol appears here when —
- **CALL side**: a resistance level is near the current price AND there's
  OI buildup at a near-money (ITM/ATM/OTM) CE strike, OR
- **PUT side**: a support level is near the current price AND there's OI
  buildup at a near-money PE strike.

"OI buildup" = that near-money strike is also one of the top-OI strikes
for that side (`config.HIGH_OI_TOP_N`, default 3) — real concentration,
not just being close to spot. Confirmed trade setups (after volume +
price-reaction checks) show in the second table on this tab.

### Tab 3: OI Surge
Two fully independent live boards. Any strike, any symbol, whose OI
changed more than `config.OI_CHANGE_THRESHOLD_PCT` (default 100%) within
the rolling `config.OI_CHANGE_ROLLING_WINDOW_MIN` (default 15 min) window
appears here directly — CE surges and PE surges in separate sections. This
has no dependency on SR levels or the watchlist; it's a raw "something big
just happened" board.

Tested end-to-end on 200 simulated minutes: 12 watchlist entries, 100 CE +
PE surges, 14 confirmed setups — all three mechanisms verified working
with real numbers before shipping this.

## Files (new/changed since last version)
- `app.py` — 3-tab dashboard, JS polling (no full-page reload, so your
  selected tab/chart persist), password gate still supported via
  `DASHBOARD_PASSWORD` env var
- `charting.py` — new: lightweight inline SVG chart per symbol, no
  matplotlib dependency
- `oi_surge.py` — new: standalone CE/PE surge board, decoupled from watchlist
- `watchlist.py` — rewritten: SR-nearby + near-money OI buildup only
  (OI surge triggering moved out to oi_surge.py)
- `oi_analysis.py` — added `get_near_money_strikes()` (ITM/ATM/OTM around spot)
- `data_feed.py`, `config.py`, `models.py`, `sr_levels.py`,
  `confirmation.py` — unchanged from before
- `scanner.py`, `data_loader.py` — original CLI/CSV version, still usable
  for offline backtesting

## Still simulated data
Running on `data_feed.SimulatedFeed`. Swap in `KiteFeed` once you hand over
Kite Connect credentials — same interface, nothing else in the app changes.

## Coming next (you mentioned)
- Gamma blast strategy — send details whenever ready, I'll add it as its
  own section/tab without touching the existing three
- Further modifications you want to share afterward

## Deploying to a real domain
See `DEPLOY.md` — unchanged from before, `Procfile`/`requirements.txt`/
`runtime.txt` still apply to this version as-is.

## Connecting real Zerodha Kite Connect data

### 1. Get API credentials
Sign up as a Kite Connect developer at https://developers.kite.trade,
create an app (needs a paid Kite Connect subscription, ₹2000/month), and
note your **API key** and **API secret**.

### 2. Install the library
```
python3 -m pip install kiteconnect --break-system-packages
```

### 3. Log in each trading day (tokens expire daily — this is Zerodha's
security model, not something we can code around)
```
export KITE_API_KEY=your_api_key
export KITE_API_SECRET=your_api_secret
python3 kite_auth.py
```
This prints a login URL, you log in with your Zerodha credentials, get
redirected to a URL containing `request_token=...`, paste that value back
into the terminal. It saves a fresh `kite_token.json` locally.

### 4. Run the dashboard against live data
```
export USE_KITE=1
export KITE_API_KEY=your_api_key
python3 app.py
```
Optionally set `KITE_UNIVERSE=RELIANCE,HDFCBANK,TCS,...` to control which
F&O stocks it scans (defaults to a demo list of 6 — for the real F&O
universe, tell me and I'll wire in a fetch of NSE's full published list).

The dashboard banner will show "Connected to Zerodha Kite — live market
data" once it's actually pulling real ticks.

### How it works under the hood
`kite_feed.py` resolves the nearest-expiry option chain for each symbol in
your universe, opens a single WebSocket (`KiteTicker`) subscribed to spot +
all option instruments in FULL mode (FULL mode is what carries the `oi`
field — regular LTP/QUOTE mode doesn't), and aggregates ticks into 1-minute
candles and a live OI snapshot in memory. Everything downstream (SR
detection, watchlist, OI surge, confirmation) is unchanged — same code,
real data instead of simulated.

I tested `kite_feed.py`'s instrument resolution and tick-handling logic
against mocked Kite responses (expiry filtering, spot candle aggregation,
OI tracking) — verified correct — but haven't run it against a live Zerodha
connection since that needs your actual credentials. Try it and send me
whatever error comes up first, if any; WebSocket/auth edge cases are the
most likely rough spots on a first real connection.

### Known limitations to fix once you're testing live
- `KITE_UNIVERSE` defaults to 6 demo stocks — swap for the real F&O list
- No token-refresh automation — you run `kite_auth.py` manually each morning
- If the WebSocket disconnects mid-day (network blip), `kiteconnect`
  auto-reconnects, but a long gap will leave a stale-looking chart until
  reconnection — fine for now, worth hardening later if this becomes daily-use

## Connecting Fyers (for testing before Kite)

### 1. Get API credentials
Sign up as a developer at https://myapi.fyers.in, create an app (Web app
type is simplest), note your **Client ID** (looks like `XXXXX-100`) and
**Secret Key**. Fyers API access itself has no separate subscription fee
beyond your regular Fyers trading account — check their site for current
terms since this can change.

### 2. Install the library
```
python3 -m pip install fyers-apiv3 --break-system-packages
```

### 3. Log in each trading day (tokens expire daily, same as Kite)
```
export FYERS_CLIENT_ID=XXXXX-100
export FYERS_SECRET_KEY=your_secret
export FYERS_REDIRECT_URI=http://127.0.0.1:8000
python3 fyers_auth.py
```
Opens a login URL, you authenticate, get redirected (the redirect page
itself can show a browser error — that's fine, you just need the URL to
copy the `auth_code` parameter from). Saves `fyers_token.json` locally.

### 4. Run the dashboard against live Fyers data
```
export USE_FYERS=1
export FYERS_CLIENT_ID=XXXXX-100
python3 app.py
```
Optionally `FYERS_UNIVERSE=RELIANCE,HDFCBANK,TCS,...` to control the scan list.

### Important architectural difference from Kite
Fyers has **no WebSocket for option chain OI** (confirmed via their own
sample-code repo — it's an open, unimplemented feature request). So:
- **Spot price**: real WebSocket ticks, aggregated into 1-min candles — same
  approach as Kite.
- **Option OI**: REST-polled via `fyers.optionchain()` every 30 seconds per
  symbol (`OI_POLL_INTERVAL_SEC` in `fyers_feed.py`) — this is the only way
  Fyers exposes it. With 6 demo symbols that's roughly 17,000 calls/day,
  comfortably inside their 1,00,000/day limit — but if you expand the
  universe a lot, raise `OI_POLL_INTERVAL_SEC` to stay within budget.

One upside: Fyers' `optionchain()` `strikecount` parameter returns exactly
N-ITM + 1-ATM + N-OTM strikes directly — a natural fit for the watchlist's
near-money rule, no extra logic needed.

I tested `fyers_feed.py`'s WebSocket tick handling and option-chain parsing
against mocked Fyers responses (correctly builds candles from ticks,
correctly filters the option chain to real CE/PE strikes only, skipping the
underlying's own row) — verified correct — but same caveat as Kite: haven't
run it against your real Fyers connection yet, since that needs your
credentials. Try it and send me the first error if something breaks.

### Switching to Kite later
Nothing about the scanner/watchlist/surge logic changes — just stop setting
`USE_FYERS=1`, set `USE_KITE=1` instead (with Kite's own env vars, see the
Kite section above). Same dashboard, same rules, different data source.

## Gamma Squeeze tab (Gamma Blast strategy)

New 4th tab, following your sequence:
```
NSE F&O data -> option-chain scanner -> Gamma/OI/Volume/IV analysis
-> Gamma Score (0-100) -> filter Score >= 80 -> Telegram alert
```

### Scoring — your weights, all measured over a rolling 15-min window
| Component | Weight |
|---|---|
| Price + volume momentum | 20% |
| Near-ATM gamma concentration | 20% |
| IV expansion | 15% |
| Short covering / futures OI | 10% |
| Expiry proximity | 10% |

**Note**: your weights sum to 75%, not 100% — `config.GAMMA_NORMALIZE_WEIGHTS`
(default `True`) rescales them proportionally so the final score still lands
on 0-100. If you actually meant a 6th unlisted factor for the remaining 25%,
tell me what it is and I'll wire it in instead of the rescale.

### Alert levels (your spec, unchanged)
- 80-100: 🔴 Strong gamma-squeeze setup
- 65-79: 🟠 Watch closely
- 50-64: 🟡 Developing
- <50: no alert, not shown on the dashboard

### Telegram
Score ≥ 80 triggers a push, formatted like your example message, with a
30-minute cooldown per symbol so a sustained squeeze doesn't spam you every
4 seconds (`config.GAMMA_TELEGRAM_RE_ALERT_COOLDOWN_MIN`). Every alert also
logs to the dashboard's "Telegram Alert Log" panel regardless of whether
Telegram is configured, so you can see it firing even before you set up a bot.

Setup:
```
export TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
export TELEGRAM_CHAT_ID=your_chat_id
```
(Message @BotFather on Telegram for a token; get your chat_id from
`https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging your bot once.)

### Data availability — what's real vs proxied right now
- **SimulatedFeed**: generates synthetic IV and a futures quote per symbol
  specifically so this whole module is testable today — verified end-to-end
  (60 simulated ticks produced real watch/developing-level scores with
  correct component breakdowns).
- **KiteFeed**: doesn't pull IV or futures quotes yet — the IV component
  falls back to option premium (`ltp`) as a proxy, and the futures
  component scores 0 until wired up.
- **FyersFeed**: `optionchain()` can return real IV if requested with
  `greeks=1` — not yet added to `fyers_feed.py`. Futures quotes aren't
  pulled either. Same fallback as Kite until both are added.

Tell me if you want IV/futures wired into Kite or Fyers next — for Fyers
specifically it's a small change (add `greeks=1` to the optionchain request
and pull a futures symbol quote alongside the equity one).

### Files
- `gamma_blast.py` — scoring engine, 5 components, rolling-window history
- `telegram_alert.py` — message formatting + sending, matches your example layout
- `models.py` — added `FutureQuote`, `GammaScore`; `OIStrike` gained `iv`/`ltp`
- `config.py` — all Gamma Blast thresholds/weights, clearly separated section
- `data_feed.py` — `SimulatedFeed` now also generates IV + futures data
- `app.py` — new Gamma Squeeze tab, `/api/gamma` route, Telegram dispatch in the background loop

## Fix: Scanner now covers the FULL NSE F&O stock universe

Previously the scanner only ever ran on a hardcoded 6-stock demo list, even
in live mode — that was a leftover placeholder that should have been fixed
once we moved off the simulator. Fixed now:

- **Fyers**: `fyers_feed.py` fetches the real, current F&O stock list
  (~180+ stocks) from Fyers' own published symbol master CSV
  (`nse_universe.py`) unless you explicitly set `FYERS_UNIVERSE`. Cached
  locally for a week since NSE only revises this list quarterly.
- **Kite**: `kite_feed.py` derives the full list directly from Kite's own
  `instruments("NFO")` call (no extra fetch needed) unless you explicitly
  set `KITE_UNIVERSE`.
- **Simulated**: unchanged, still the 6-stock demo list — it's synthetic
  data either way, no need to simulate 180 stocks for a mechanism check.

### Rate limits at full-universe scale (Fyers)
Fyers has no WebSocket for option OI (explained earlier), so OI is
REST-polled. With ~180 stocks, polling every symbol every 30 seconds would
blow past Fyers' 1,00,000 requests/day limit. `fyers_feed.py` now computes
the poll interval automatically from your universe size — at ~180 stocks
that works out to roughly every 48 seconds per symbol, comfortably inside
the daily budget. Smaller universes still poll as fast as 30s (the floor).
Verified the math directly: 180 symbols → ~84,375 calls/day, under the
100,000 limit with margin for other API calls.

### One caveat not yet handled: WebSocket subscription limits
Both Kite and Fyers cap how many instruments you can subscribe to on a
single WebSocket connection (exact limit depends on your plan). At ~180
stocks plus ~11 option strikes each for spot+near-money tracking, you might
hit that ceiling depending on your plan tier. If you see a subscribe error
mentioning a limit, tell me and I'll add sharding across multiple socket
connections — didn't want to guess at your specific plan's limit and build
something you don't need.

### Files
- `nse_universe.py` — new: fetches + caches the real NSE F&O stock list
- `fyers_feed.py`, `kite_feed.py` — updated to auto-detect the full
  universe by default, plus (Fyers only) automatic rate-limit-safe polling interval
