"""
app.py
------
Live dashboard, 3 tabs:
  1. Scanner  — all F&O universe symbols (intraday + positional together),
                price chart per stock with SR levels + OI strikes marked
  2. Watchlist — symbols where an SR level is nearby AND there's OI buildup
                 at a near-money (ITM/ATM/OTM) strike on the matching side
  3. OI Surge  — two independent live boards: any strike (any symbol) whose
                 OI changed >100% in the last 15 min, split CE / PE

No uploads. A background thread pumps the (currently simulated) feed and
reruns the scanner every few seconds; the UI polls a JSON API and updates
itself without a full page reload, so tab selection and the chart you're
looking at don't reset every refresh.

Run:
    python3 app.py
Open http://localhost:5000
"""
import os
import threading
import time
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, abort, Response

import config
from data_feed import SimulatedFeed
from models import Candle
from oi_analysis import record_snapshot, get_high_oi_strikes, get_near_money_strikes, get_significant_oi_changes
from sr_levels import get_important_levels
from watchlist import Watchlist
from oi_surge import SurgeBoard
from confirmation import evaluate
from charting import render_chart
from models import FutureQuote
from gamma_blast import compute_score, should_send_telegram_alert
import telegram_alert

app = Flask(__name__)

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")


@app.before_request
def _check_auth():
    if not DASHBOARD_PASSWORD:
        return
    if request.args.get("key") == DASHBOARD_PASSWORD:
        return
    if request.cookies.get("dashboard_auth") == DASHBOARD_PASSWORD:
        return
    abort(401, description="Add ?key=YOUR_PASSWORD to the URL once.")


@app.after_request
def _set_auth_cookie(response):
    if DASHBOARD_PASSWORD and request.args.get("key") == DASHBOARD_PASSWORD:
        response.set_cookie("dashboard_auth", DASHBOARD_PASSWORD, max_age=60 * 60 * 24 * 30, httponly=True)
    return response


def _build_feed():
    """Set USE_KITE=1 or USE_FYERS=1 (plus the relevant credentials/env vars
    and a fresh token via kite_auth.py / fyers_auth.py) to go live.
    Otherwise runs on simulated demo data."""
    if os.environ.get("USE_KITE") == "1":
        from kite_feed import KiteFeed
        api_key = os.environ["KITE_API_KEY"]
        universe = os.environ.get("KITE_UNIVERSE")
        universe = universe.split(",") if universe else None
        return KiteFeed(api_key=api_key, universe=universe)
    if os.environ.get("USE_FYERS") == "1":
        from fyers_feed import FyersFeed
        client_id = os.environ["FYERS_CLIENT_ID"]
        universe = os.environ.get("FYERS_UNIVERSE")
        universe = universe.split(",") if universe else None
        return FyersFeed(client_id=client_id, universe=universe)
    return SimulatedFeed()


feed = _build_feed()
watchlist = Watchlist()
surge_board = SurgeBoard()

STATE = {
    "scanner_rows": [],
    "chart_cache": {},
    "setups": [],
    "gamma_scores": [],
    "gamma_alert_log": [],
    "last_update": None,
    "tick_count": 0,
}
STATE_LOCK = threading.Lock()
TICK_INTERVAL_SEC = 4


def _rows_to_candles(rows):
    return [Candle(symbol=r["symbol"], timestamp=r["timestamp"], open=r["open"],
                    high=r["high"], low=r["low"], close=r["close"], volume=r["volume"])
            for r in rows]


def background_loop():
    while True:
        feed.tick()
        oi_rows = feed.get_oi_snapshot_rows()
        all_strikes = record_snapshot(oi_rows)

        surge_board.scan(all_strikes)

        scanner_rows = []
        setups_new = []
        gamma_scores_new = []

        for symbol in feed.universe:
            raw_candles = feed.get_candles(symbol)
            if len(raw_candles) < 10:
                continue
            candles = _rows_to_candles(raw_candles)
            current_price = candles[-1].close
            sr_levels = get_important_levels(symbol, candles)

            symbol_strikes = [s for s in all_strikes if s.symbol == symbol]
            if not symbol_strikes:
                continue
            expiries = set(s.expiry for s in symbol_strikes)

            for expiry in expiries:
                high_ce = get_high_oi_strikes(symbol_strikes, symbol, expiry, "CE")
                high_pe = get_high_oi_strikes(symbol_strikes, symbol, expiry, "PE")
                near_ce = get_near_money_strikes(symbol_strikes, symbol, expiry, "CE", current_price)
                near_pe = get_near_money_strikes(symbol_strikes, symbol, expiry, "PE", current_price)

                watchlist.scan_symbol(symbol, current_price, sr_levels, near_ce, near_pe, high_ce, high_pe)

                sig = get_significant_oi_changes(symbol_strikes)
                sig_keys = {(s.strike, s.option_type) for s in sig}
                for entry in watchlist.get_active():
                    if entry.symbol != symbol:
                        continue
                    if (entry.matched_strike.strike, entry.matched_strike.option_type) in sig_keys:
                        setup = evaluate(entry, candles)
                        if setup:
                            watchlist.mark_confirmed(entry)
                            setups_new.append(setup)

                # --- Gamma Blast scoring for this expiry's chain
                all_ce = [s for s in symbol_strikes if s.expiry == expiry and s.option_type == "CE"]
                all_pe = [s for s in symbol_strikes if s.expiry == expiry and s.option_type == "PE"]
                avg_volume = (sum(c.volume for c in candles[-20:]) / min(20, len(candles))) if candles else None
                futures_raw = feed.get_futures_quote(symbol) if hasattr(feed, "get_futures_quote") else None
                futures = FutureQuote(symbol=symbol, price=futures_raw["price"], oi=futures_raw["oi"],
                                       timestamp=candles[-1].timestamp) if futures_raw else None

                gscore = compute_score(symbol, candles, near_ce, near_pe, all_ce, all_pe,
                                        futures=futures, avg_volume=avg_volume)
                if gscore and gscore.alert_level != "none":
                    gamma_scores_new.append(gscore)
                    if should_send_telegram_alert(gscore):
                        sent = telegram_alert.send_alert(gscore)
                        with STATE_LOCK:
                            STATE["gamma_alert_log"] = (STATE["gamma_alert_log"] + [{
                                "symbol": gscore.symbol, "score": gscore.total_score,
                                "sent_to_telegram": sent, "at": datetime.now().strftime("%H:%M:%S"),
                            }])[-30:]

                with STATE_LOCK:
                    STATE["chart_cache"][symbol] = (candles, sr_levels, high_ce, high_pe)

            nearest_support = next((l for l in sr_levels if l.kind == "support"), None)
            nearest_resistance = next((l for l in sr_levels if l.kind == "resistance"), None)
            scanner_rows.append({
                "symbol": symbol,
                "price": round(current_price, 2),
                "support": round(nearest_support.price, 2) if nearest_support else None,
                "resistance": round(nearest_resistance.price, 2) if nearest_resistance else None,
            })

        with STATE_LOCK:
            STATE["scanner_rows"] = scanner_rows
            STATE["setups"] = (STATE["setups"] + setups_new)[-30:]
            STATE["gamma_scores"] = sorted(gamma_scores_new, key=lambda s: s.total_score, reverse=True)
            STATE["last_update"] = datetime.now().strftime("%H:%M:%S")
            STATE["tick_count"] += 1

        time.sleep(TICK_INTERVAL_SEC)


@app.route("/api/meta")
def api_meta():
    if os.environ.get("USE_KITE") == "1":
        source = "kite"
    elif os.environ.get("USE_FYERS") == "1":
        source = "fyers"
    else:
        source = "simulated"
    return jsonify({"live": source != "simulated", "source": source})


@app.route("/api/scanner")
def api_scanner():
    with STATE_LOCK:
        return jsonify({
            "rows": STATE["scanner_rows"],
            "last_update": STATE["last_update"],
            "tick_count": STATE["tick_count"],
        })


@app.route("/api/watchlist")
def api_watchlist():
    active = watchlist.get_active()
    with STATE_LOCK:
        setups = STATE["setups"]
    return jsonify({
        "watchlist": [
            {"symbol": e.symbol, "side": e.matched_strike.option_type,
             "strike": e.matched_strike.strike, "oi": e.matched_strike.oi,
             "level": e.sr_level.price if e.sr_level else None,
             "level_kind": e.sr_level.kind if e.sr_level else None,
             "reason": e.reason}
            for e in active
        ],
        "setups": [
            {"symbol": s.symbol, "direction": s.direction,
             "entry_zone": s.entry_zone, "confirmations": s.confirmations}
            for s in setups
        ],
    })


@app.route("/api/surge")
def api_surge():
    def fmt(item):
        s = item["strike_obj"]
        return {"symbol": s.symbol, "strike": s.strike, "oi": s.oi,
                "change_pct": round(item["change_pct"], 1) if item["change_pct"] is not None else None,
                "detected_at": item["detected_at"].strftime("%H:%M:%S")}
    return jsonify({
        "ce_surge": [fmt(i) for i in surge_board.get_ce_surges()],
        "pe_surge": [fmt(i) for i in surge_board.get_pe_surges()],
    })


@app.route("/api/gamma")
def api_gamma():
    with STATE_LOCK:
        scores = STATE["gamma_scores"]
        alert_log = STATE["gamma_alert_log"]
    return jsonify({
        "scores": [
            {"symbol": s.symbol, "score": s.total_score, "level": s.alert_level,
             "price": s.price, "components": {k: round(v, 1) for k, v in s.components.items()},
             "key_strike": (f"{s.key_strike.strike:.0f} {s.key_strike.option_type}"
                            if s.key_strike else None)}
            for s in scores
        ],
        "alert_log": alert_log,
    })


@app.route("/chart/<symbol>")
def chart(symbol):
    with STATE_LOCK:
        cached = STATE["chart_cache"].get(symbol)
    if not cached:
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="300">' \
              '<rect width="100%" height="100%" fill="#0f1117"/></svg>'
    else:
        candles, sr_levels, high_ce, high_pe = cached
        svg = render_chart(symbol, candles, sr_levels, high_ce, high_pe)
    return Response(svg, mimetype="image/svg+xml")


PAGE = """
<!doctype html>
<html>
<head>
<title>F&O Scanner</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 1100px;
         margin: 24px auto; padding: 0 20px; background: #0f1117; color: #e6e6e6; }
  .topbar { display: flex; justify-content: space-between; align-items: baseline; }
  h1 { font-size: 20px; margin: 0; }
  .status { font-size: 12px; color: #6b7280; }
  .status .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#4fd1c5; margin-right:6px; }
  .tabs { display: flex; gap: 6px; margin: 18px 0; border-bottom: 1px solid #262b36; }
  .tab { padding: 8px 16px; cursor: pointer; color: #9aa0ac; font-size: 13px; font-weight: 600;
         border-bottom: 2px solid transparent; }
  .tab.active { color: #e6e6e6; border-bottom-color: #4f7cff; }
  .panel { display: none; } .panel.active { display: block; }
  .card { background: #171a21; border: 1px solid #262b36; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }
  h2 { font-size: 13px; margin: 0 0 10px 0; color: #9aa0ac; text-transform: uppercase; letter-spacing: 0.5px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #262b36; }
  th { color: #9aa0ac; font-weight: 600; font-size: 11px; text-transform: uppercase; }
  .tag-CE { color: #ff6b6b; font-weight: 600; } .tag-PE { color: #4fd1c5; font-weight: 600; }
  .empty { color: #6b7280; font-style: italic; padding: 8px 0; }
  .setup-long { color: #4fd1c5; font-weight: 700; } .setup-short { color: #ff6b6b; font-weight: 700; }
  .badge { font-size: 10px; padding: 2px 6px; border-radius: 10px; background: #262b36; color: #9aa0ac; margin-left: 6px; }
  .demo-notice { font-size: 12px; color: #b08b2e; background: #221d0f; border: 1px solid #3a3115; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; }
  select { background: #0f1117; color: #e6e6e6; border: 1px solid #333a47; border-radius: 6px; padding: 6px 8px; margin-bottom: 10px; }
  img.chart { width: 100%; border-radius: 6px; }
  .row-clickable { cursor: pointer; }
  .row-clickable:hover { background: #1d212b; }
  .surge-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
  .gamma-card { border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; border: 1px solid; }
  .gamma-strong { background: #2a1215; border-color: #6b2530; }
  .gamma-watch { background: #2a1f0f; border-color: #6b4d1a; }
  .gamma-developing { background: #1a1f14; border-color: #4a5530; }
  .gamma-title { display: flex; justify-content: space-between; align-items: center; font-weight: 700; font-size: 14px; }
  .gamma-score-strong { color: #ff6b6b; } .gamma-score-watch { color: #f0a030; } .gamma-score-developing { color: #d9d95c; }
  .gamma-components { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin-top: 10px; font-size: 11px; color: #9aa0ac; }
  .gamma-comp-val { color: #e6e6e6; font-size: 13px; font-weight: 600; }
  .gamma-meta { font-size: 12px; color: #9aa0ac; margin-top: 8px; }
  .gamma-log { font-size: 12px; color: #6b7280; margin-top: 4px; }
</style>
</head>
<body>
<div class="topbar">
  <h1>F&amp;O Support/Resistance + OI Scanner</h1>
  <div class="status"><span class="dot"></span>live &middot; <span id="lastUpdate">-</span> &middot; tick <span id="tickCount">0</span></div>
</div>
<div class="demo-notice" id="dataModeNotice">Checking data source&hellip;</div>

<div class="tabs">
  <div class="tab active" data-tab="scanner">Scanner</div>
  <div class="tab" data-tab="watchlist">Watchlist</div>
  <div class="tab" data-tab="surge">OI Surge</div>
  <div class="tab" data-tab="gamma">Gamma Squeeze</div>
</div>

<div class="panel active" id="panel-scanner">
  <div class="card">
    <h2>F&amp;O Universe</h2>
    <table id="scannerTable">
      <tr><th>Symbol</th><th>LTP</th><th>Support</th><th>Resistance</th></tr>
    </table>
  </div>
  <div class="card">
    <h2>Chart</h2>
    <select id="chartSymbolSelect"></select>
    <img class="chart" id="chartImg" src="">
  </div>
</div>

<div class="panel" id="panel-watchlist">
  <div class="card">
    <h2>Watchlist <span class="badge" id="watchlistCount">0</span></h2>
    <table id="watchlistTable">
      <tr><th>Symbol</th><th>Side</th><th>Strike</th><th>OI</th><th>SR Level</th><th>Reason</th></tr>
    </table>
    <div class="empty" id="watchlistEmpty" style="display:none">No watchlist entries yet — needs an SR level nearby AND OI buildup at a near-money strike.</div>
  </div>
  <div class="card">
    <h2>Trade Setups <span class="badge" id="setupsCount">0</span></h2>
    <table id="setupsTable">
      <tr><th>Symbol</th><th>Direction</th><th>Entry Zone</th><th>Confirmations</th></tr>
    </table>
    <div class="empty" id="setupsEmpty" style="display:none">No confirmed setups yet.</div>
  </div>
</div>

<div class="panel" id="panel-surge">
  <div class="surge-cols">
    <div class="card">
      <h2>CE OI Surge <span class="badge" id="ceSurgeCount">0</span></h2>
      <table id="ceSurgeTable"><tr><th>Symbol</th><th>Strike</th><th>OI</th><th>Delta%</th><th>At</th></tr></table>
      <div class="empty" id="ceSurgeEmpty" style="display:none">No CE strikes with &gt;100% OI change in the last 15 min.</div>
    </div>
    <div class="card">
      <h2>PE OI Surge <span class="badge" id="peSurgeCount">0</span></h2>
      <table id="peSurgeTable"><tr><th>Symbol</th><th>Strike</th><th>OI</th><th>Delta%</th><th>At</th></tr></table>
      <div class="empty" id="peSurgeEmpty" style="display:none">No PE strikes with &gt;100% OI change in the last 15 min.</div>
    </div>
  </div>
</div>

<div class="panel" id="panel-gamma">
  <div class="card">
    <h2>Gamma Squeeze Scores <span class="badge" id="gammaCount">0</span></h2>
    <div id="gammaCards"></div>
    <div class="empty" id="gammaEmpty" style="display:none">No symbols scoring 50+ right now.</div>
  </div>
  <div class="card">
    <h2>Telegram Alert Log</h2>
    <div id="gammaAlertLog"></div>
    <div class="empty" id="gammaAlertEmpty" style="display:none">No alerts sent yet (fires at score \u2265 80, TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID env vars must be set to actually deliver).</div>
  </div>
</div>

<script>
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('panel-' + t.dataset.tab).classList.add('active');
}));

let knownSymbols = [];
let selectedChartSymbol = null;

function fmtNum(n) { return n.toLocaleString('en-IN'); }

async function refreshScanner() {
  const r = await fetch('/api/scanner'); const d = await r.json();
  document.getElementById('lastUpdate').textContent = d.last_update || 'starting...';
  document.getElementById('tickCount').textContent = d.tick_count;
  const table = document.getElementById('scannerTable');
  table.innerHTML = '<tr><th>Symbol</th><th>LTP</th><th>Support</th><th>Resistance</th></tr>';
  d.rows.forEach(row => {
    const tr = document.createElement('tr');
    tr.className = 'row-clickable';
    tr.onclick = () => selectChart(row.symbol);
    tr.innerHTML = `<td>${row.symbol}</td><td>${row.price}</td><td>${row.support ?? '-'}</td><td>${row.resistance ?? '-'}</td>`;
    table.appendChild(tr);
  });
  const sel = document.getElementById('chartSymbolSelect');
  const symbols = d.rows.map(r => r.symbol);
  if (JSON.stringify(symbols) !== JSON.stringify(knownSymbols)) {
    knownSymbols = symbols;
    sel.innerHTML = symbols.map(s => `<option value="${s}">${s}</option>`).join('');
    if (!selectedChartSymbol && symbols.length) selectChart(symbols[0]);
  }
}

function selectChart(sym) {
  selectedChartSymbol = sym;
  document.getElementById('chartSymbolSelect').value = sym;
  document.getElementById('chartImg').src = '/chart/' + sym + '?t=' + Date.now();
}
document.getElementById('chartSymbolSelect').addEventListener('change', e => selectChart(e.target.value));

async function refreshWatchlist() {
  const r = await fetch('/api/watchlist'); const d = await r.json();
  const wt = document.getElementById('watchlistTable');
  wt.innerHTML = '<tr><th>Symbol</th><th>Side</th><th>Strike</th><th>OI</th><th>SR Level</th><th>Reason</th></tr>';
  document.getElementById('watchlistCount').textContent = d.watchlist.length;
  document.getElementById('watchlistEmpty').style.display = d.watchlist.length ? 'none' : 'block';
  d.watchlist.forEach(e => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${e.symbol}</td><td class="tag-${e.side}">${e.side}</td><td>${e.strike}</td>
      <td>${fmtNum(e.oi)}</td><td>${e.level ?? '-'} (${e.level_kind ?? '-'})</td><td>${e.reason}</td>`;
    wt.appendChild(tr);
  });
  const st = document.getElementById('setupsTable');
  st.innerHTML = '<tr><th>Symbol</th><th>Direction</th><th>Entry Zone</th><th>Confirmations</th></tr>';
  document.getElementById('setupsCount').textContent = d.setups.length;
  document.getElementById('setupsEmpty').style.display = d.setups.length ? 'none' : 'block';
  d.setups.forEach(s => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${s.symbol}</td><td class="setup-${s.direction}">${s.direction.toUpperCase()}</td>
      <td>${s.entry_zone.toFixed(2)}</td><td>${s.confirmations.join(', ')}</td>`;
    st.appendChild(tr);
  });
}

async function refreshSurge() {
  const r = await fetch('/api/surge'); const d = await r.json();
  const ce = document.getElementById('ceSurgeTable');
  ce.innerHTML = '<tr><th>Symbol</th><th>Strike</th><th>OI</th><th>Delta%</th><th>At</th></tr>';
  document.getElementById('ceSurgeCount').textContent = d.ce_surge.length;
  document.getElementById('ceSurgeEmpty').style.display = d.ce_surge.length ? 'none' : 'block';
  d.ce_surge.forEach(s => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${s.symbol}</td><td>${s.strike}</td><td>${fmtNum(s.oi)}</td><td>+${s.change_pct}%</td><td>${s.detected_at}</td>`;
    ce.appendChild(tr);
  });
  const pe = document.getElementById('peSurgeTable');
  pe.innerHTML = '<tr><th>Symbol</th><th>Strike</th><th>OI</th><th>Delta%</th><th>At</th></tr>';
  document.getElementById('peSurgeCount').textContent = d.pe_surge.length;
  document.getElementById('peSurgeEmpty').style.display = d.pe_surge.length ? 'none' : 'block';
  d.pe_surge.forEach(s => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${s.symbol}</td><td>${s.strike}</td><td>${fmtNum(s.oi)}</td><td>+${s.change_pct}%</td><td>${s.detected_at}</td>`;
    pe.appendChild(tr);
  });
}

async function refreshGamma() {
  const r = await fetch('/api/gamma'); const d = await r.json();
  const box = document.getElementById('gammaCards');
  box.innerHTML = '';
  document.getElementById('gammaCount').textContent = d.scores.length;
  document.getElementById('gammaEmpty').style.display = d.scores.length ? 'none' : 'block';
  const compLabels = {momentum: 'Momentum', gamma_concentration: 'Gamma Conc.', iv: 'IV Exp.', futures: 'Futures', expiry: 'Expiry'};
  d.scores.forEach(s => {
    const card = document.createElement('div');
    card.className = 'gamma-card gamma-' + s.level;
    const icon = s.level === 'strong' ? '🔴' : (s.level === 'watch' ? '🟠' : '🟡');
    let compsHtml = '';
    for (const k in compLabels) {
      compsHtml += `<div><div>${compLabels[k]}</div><div class="gamma-comp-val">${s.components[k]}</div></div>`;
    }
    card.innerHTML = `
      <div class="gamma-title"><span>${icon} ${s.symbol} &middot; \u20B9${s.price.toFixed(2)}</span>
        <span class="gamma-score-${s.level}">${s.score.toFixed(0)}/100</span></div>
      <div class="gamma-components">${compsHtml}</div>
      <div class="gamma-meta">Key strike: ${s.key_strike ?? '-'} &middot; Level: ${s.level.toUpperCase()}</div>`;
    box.appendChild(card);
  });

  const logBox = document.getElementById('gammaAlertLog');
  logBox.innerHTML = '';
  document.getElementById('gammaAlertEmpty').style.display = d.alert_log.length ? 'none' : 'block';
  d.alert_log.slice().reverse().forEach(a => {
    const div = document.createElement('div');
    div.className = 'gamma-log';
    div.textContent = `${a.at} \u2014 ${a.symbol} score ${a.score.toFixed(0)} \u2014 ${a.sent_to_telegram ? 'sent to Telegram' : 'Telegram not configured, logged only'}`;
    logBox.appendChild(div);
  });
}

function refreshAll() {
  refreshScanner();
  refreshWatchlist();
  refreshSurge();
  refreshGamma();
  if (selectedChartSymbol) document.getElementById('chartImg').src = '/chart/' + selectedChartSymbol + '?t=' + Date.now();
}
refreshAll();
setInterval(refreshAll, 4000);

fetch('/api/meta').then(r => r.json()).then(d => {
  const el = document.getElementById('dataModeNotice');
  if (d.live) {
    el.textContent = 'Connected to ' + d.source.toUpperCase() + ' — live market data.';
    el.style.color = '#4fd1c5'; el.style.background = '#0f221d'; el.style.borderColor = '#153a31';
  } else {
    el.textContent = 'Running on simulated demo data — set USE_FYERS=1 or USE_KITE=1 to go live. Rule logic is real either way.';
  }
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)), host="0.0.0.0", threaded=True)
else:
    _t = threading.Thread(target=background_loop, daemon=True)
    _t.start()
