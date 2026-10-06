# Deploying option_scanner on Railway — fully automated

Two services in one Railway project:
1. **Main app** — the Flask dashboard, always running
2. **Cron Job** — runs `fyers_auto_auth.py` every trading morning, refreshes
   the token on the main app, redeploys it automatically

No manual `fyers_auth.py` + copy-paste ever again, once this is set up.

---

## 0. Before you start — get your TOTP secret

Fyers app -> Profile -> 2FA settings -> enable/reset TOTP-based 2FA -> when
it shows a QR code, look for "can't scan this?" / "enter manually" -> copy
the raw secret (looks like `JBSWY3DPEHPK3PXP`). Save it — this is
`FYERS_TOTP_SECRET` below. If your account's 2FA is already set to
something else, you'll need to switch it to the authenticator-app/TOTP
method first.

---

## 1. Push the latest code to GitHub
```
cd ~/Desktop/option_scanner
git add .
git commit -m "Add Railway deployment + automated Fyers auth"
git push
```

---

## 2. Create the main app service on Railway

1. railway.app -> your project (or New Project) -> **Deploy from GitHub repo**
   -> select `option-scanner`
2. Once it's created, click into the service -> **Settings**:
   - **Start Command**: `gunicorn app:app --workers 1 --threads 8 --timeout 120`
     (keep `--workers 1` — more workers means multiple independent
     background loops corrupting shared in-memory state)
3. **Variables** tab -> add:
   ```
   USE_FYERS=1
   FYERS_CLIENT_ID=8D95NDHW02-100
   DASHBOARD_PASSWORD=<something only you know>
   TELEGRAM_BOT_TOKEN=<if using Gamma Blast Telegram alerts>
   TELEGRAM_CHAT_ID=<same>
   ```
   Don't set `FYERS_ACCESS_TOKEN` manually — the Cron Job sets it for you.
4. Deploy. It'll fail or sit with no live data until step 5 runs once — that's expected.

---

## 3. Get the three Railway IDs you'll need

With the main app service open, look at the URL:
```
railway.app/project/<PROJECT_ID>/service/<SERVICE_ID>?environmentId=<ENVIRONMENT_ID>
```
Copy all three.

---

## 4. Get a Railway API token

Railway dashboard -> account icon -> **Account Settings** -> **Tokens** ->
**Create Token** -> scope it to this project -> copy it (shown once).

---

## 5. Create the Cron Job service

1. Same Railway project -> **New** -> **Empty Service** (or "Cron Job" if
   your Railway UI offers that service type directly)
2. Connect it to the **same GitHub repo**
3. **Settings** -> **Cron Schedule**: `0 3 * * 1-5` (3:00 AM UTC =
   8:30 AM IST, before the 9:15 AM market open, Monday-Friday only).
   Adjust the UTC offset if you're not in IST.
4. **Start Command**: `python3 fyers_auto_auth.py`
5. **Variables** tab -> add:
   ```
   FYERS_CLIENT_ID=8D95NDHW02-100
   FYERS_SECRET_KEY=<your Fyers app secret>
   FYERS_REDIRECT_URI=https://www.google.com
   FYERS_FY_ID=<your Fyers login ID, e.g. XY00000>
   FYERS_PIN=<your 4-digit trading PIN>
   FYERS_TOTP_SECRET=<from step 0>
   RAILWAY_API_TOKEN=<from step 4>
   RAILWAY_PROJECT_ID=<from step 3>
   RAILWAY_SERVICE_ID=<the MAIN APP's service ID, from step 3>
   RAILWAY_ENVIRONMENT_ID=<from step 3>
   ```

---

## 6. Run it once manually to confirm it works

Cron Job service -> trigger a manual run (Railway lets you run a cron
service on-demand, not just on schedule) -> check its logs. You should see:
```
Starting headless Fyers login...
Got a fresh access token. Updating Railway...
FYERS_ACCESS_TOKEN updated on Railway.
Redeploy triggered...
Done.
```
Then check the main app's logs — it should restart and the dashboard
banner should say "Connected to FYERS — live market data."

---

## If the headless login breaks

`fyers_auto_auth.py` uses Fyers' internal login endpoints — the same ones
their own login page calls, not a stable public API. This is a known
pattern used by other algo-trading setups, but Fyers could change it
without notice.

**Fallback if it ever fails**: run `fyers_auth.py` locally (the manual
version, still in the repo — don't delete it), copy the token, paste it
into the main app service's `FYERS_ACCESS_TOKEN` variable on Railway by
hand, then manually trigger a redeploy from the Railway dashboard. That
gets you through one trading day while you fix the automation.

---

## Cost note

You're already on Railway's Hobby plan. The Cron Job only runs for a few
seconds once a day, so it adds negligible usage cost — the main app's
continuous WebSocket + polling is still the dominant cost driver, same as
before.
