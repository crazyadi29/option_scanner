# Fyers Access Token Setup Guide

Fyers access tokens expire **daily**. You need to renew them each trading morning before the market opens.

---

## Step 1: Create a Fyers App (One-Time Setup)

1. Go to **https://myapi.fyers.in**
2. Log in with your Fyers trading account
3. Create a new app:
   - **App Name:** Any name (e.g., "Scanner")
   - **App Type:** Web app
4. Once created, you'll see:
   - **Client ID** (looks like `XXXXX-100`) ← Save this
   - **Client Secret** ← Save this
5. Set the **Redirect URL** to: `http://127.0.0.1:8000`
   - This URL doesn't need to be real; you're just extracting a code from it

---

## Step 2: Get Your First Token Manually (Today)

Run this on your **local machine**:

```bash
# Clone the repo locally
git clone https://github.com/crazyadi29/option_scanner.git
cd option_scanner

# Install dependencies
pip install fyers-apiv3

# Set your Fyers app credentials
export FYERS_CLIENT_ID="XXXXX-100"
export FYERS_SECRET_KEY="your_secret_key"
export FYERS_REDIRECT_URI="http://127.0.0.1:8000"

# Run the auth script
python3 fyers_auth.py
```

**What happens:**
1. Script prints a login URL → Click it, log in with 2FA
2. After login, you land on a redirect URL (may show "error" — ignore it)
3. Copy the `auth_code` from the URL's `?auth_code=XXXXXXXXXXXXX` parameter
4. Paste it back into the terminal
5. Script saves `fyers_token.json` with your access token

**Example output:**
```
1. Open this URL, log in with your Fyers credentials + 2FA:

   https://api-t1.fyers.in/api/v3/login?...

2. You'll land on your redirect URL (it may show a browser error page —
   that's fine, you just need the URL). Copy the 'auth_code' parameter from it.

Paste auth_code here: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
Saved access token to fyers_token.json.
```

---

## Step 3: Upload Token to Railway

1. Open the token file:
   ```bash
   cat fyers_token.json
   ```
   You'll see something like:
   ```json
   {"access_token": "long_token_string_here", "client_id": "XXXXX-100"}
   ```

2. Go to Railway Dashboard → `option_scanner` service → **Variables**
3. Add a new variable:
   - **Name:** `FYERS_ACCESS_TOKEN`
   - **Value:** (paste the `access_token` value from above)
4. **Redeploy** the service for it to take effect

---

## Step 4: Also Set These Variables (For Live Data)

In the same Variables section, add:

| Variable | Value | Notes |
|----------|-------|-------|
| `USE_FYERS` | `1` | Enables live Fyers data (vs. simulated) |
| `FYERS_CLIENT_ID` | `XXXXX-100` | From Step 1 |
| `FYERS_UNIVERSE` | `NIFTY,BANKNIFTY` | (Optional) Comma-separated symbols. If omitted, fetches full NSE F&O universe (~180+ stocks) |

---

## Step 5: Set Up Automatic Daily Token Renewal (Optional but Recommended)

Instead of manually running `fyers_auth.py` each morning, create a **cron job** on Railway that renews the token automatically.

### Get Your TOTP Secret (Required for Automation)

This is needed so the cron job can log in without human 2FA clicks.

1. Open your **Fyers app** (mobile or web)
2. Go to **Profile → Settings → 2FA**
3. If TOTP is already enabled:
   - Disable it, then re-enable it to see the secret
4. When setting up TOTP, Fyers shows you a QR code
5. Look for a link like **"Can't scan?"** or **"Manual entry"**
6. Copy the raw secret key (looks like `JBSWY3DPEHPK3PXP`)
7. Save this as `FYERS_TOTP_SECRET` below

### Create the Cron Job Service on Railway

1. Go to your project on Railway
2. **+ New** → **Cron Job**
3. Name it: `fyers-token-renewer`
4. Set the cron schedule: `0 8 * * 1-5` (8 AM every trading day, Mon-Fri)
   - Adjust for your timezone
5. Use the same GitHub repo: `crazyadi29/option_scanner`
6. Start command: `python3 fyers_auto_auth.py`

### Set Variables on the Cron Job

Add these variables to the **cron job service** (NOT the main app):

| Variable | Value | Source |
|----------|-------|--------|
| `FYERS_CLIENT_ID` | `XXXXX-100` | Step 1 |
| `FYERS_SECRET_KEY` | Your secret | Step 1 |
| `FYERS_REDIRECT_URI` | `http://127.0.0.1:8000` | (exact value) |
| `FYERS_FY_ID` | Your Fyers login ID | e.g., `XY00000` (6 chars) |
| `FYERS_PIN` | Your 4-digit trading PIN | |
| `FYERS_TOTP_SECRET` | Your 2FA secret | From above |
| `RAILWAY_API_TOKEN` | (create below) | |
| `RAILWAY_PROJECT_ID` | `2d8a8e5a-e48c-43b1-8464-6d672568874a` | From this project |
| `RAILWAY_SERVICE_ID` | `b5e25b7b-3250-4a13-a2f1-baa38857e7d8` | `option_scanner` service ID |
| `RAILWAY_ENVIRONMENT_ID` | `d3a310df-1bab-4d4b-9354-269333def42d` | `production` environment |

### Get a Railway API Token

1. Go to **railway.app** → Click your **account icon** (top right)
2. **Settings** → **Tokens**
3. **Create Token**
4. Scope: This project
5. Copy the token → paste it as `RAILWAY_API_TOKEN` above

---

## What Happens When the Cron Runs

1. Logs into Fyers headlessly (no browser, no 2FA clicks — uses TOTP)
2. Gets a fresh access token
3. Calls Railway's API to update the `FYERS_ACCESS_TOKEN` variable
4. Triggers a redeploy of `option_scanner` so it picks up the new token
5. Your dashboard keeps running with live data all day

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'fyers_apiv3'"
Install it:
```bash
pip install fyers-apiv3 --break-system-packages
```

### "Failed to generate token. Response: ..."
- **auth_code expired?** They're single-use and expire in a few minutes — redo the login flow
- **Wrong secret_key?** Double-check against myapi.fyers.in
- **Wrong redirect_uri?** Must match exactly what you set on myapi.fyers.in

### "Headless login failed" (from the cron job)
- Wrong `FYERS_FY_ID` or `FYERS_PIN`
- Wrong `FYERS_TOTP_SECRET`
- Fallback: Run `fyers_auth.py` manually and set `FYERS_ACCESS_TOKEN` by hand for that day

### Token still not working after uploading
- Redeploy the service after updating the variable (changes don't auto-reload)
- Make sure `USE_FYERS=1` is also set

---

## Summary

| Task | When | How |
|------|------|-----|
| Get first token | Today (once) | Run `fyers_auth.py` locally, copy token to Railway |
| Renew daily (manual) | Each trading morning | Re-run `fyers_auth.py` locally, update Railway variable |
| Renew daily (automatic) | Set once, runs forever | Create cron job with `fyers_auto_auth.py` |

**Recommended:** Set up the automatic cron job (Step 5) so you never have to think about token renewal again.

