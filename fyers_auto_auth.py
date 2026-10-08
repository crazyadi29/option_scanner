"""
fyers_auto_auth.py
-------------------
Fully automated daily Fyers login — no browser, no manual auth_code paste.
Meant to run as a Railway Cron Job (see DEPLOY_RAILWAY.md) shortly before
market open each trading day.

What it does:
  1. Logs into Fyers headlessly using your FY_ID + PIN + a TOTP secret
     (the same kind of secret an authenticator app like Google Authenticator
     uses) instead of a human clicking through a login page.
  2. Completes the OAuth code exchange (same final step as fyers_auth.py)
     to get a fresh access_token.
  3. Writes that token to Railway's environment variables via their GraphQL
     API, then triggers a redeploy so the running app picks it up.

IMPORTANT — this uses Fyers' internal web-login endpoints (the same ones
their own login page calls), not a documented, stable public API. Several
Indian algo-trading communities use this exact pattern successfully, but
it's not officially supported, so:
  - It can break without notice if Fyers changes their login page's internals
  - If it ever fails, the safe fallback is the manual flow: run fyers_auth.py
    yourself that morning and paste the token into Railway by hand
  - Keep fyers_auth.py around — don't delete it

Setup — one-time:
  1. Get your TOTP secret: Fyers app -> Profile -> change/enable 2FA -> when
     it shows you a QR code for an authenticator app, there's also a "can't
     scan" / "enter manually" option showing the raw secret key (looks like
     "JBSWY3DPEHPK3PXP") — save that, it's FYERS_TOTP_SECRET below. If 2FA
     is already enabled without TOTP, you'll need to reset it to the TOTP
     method first.
  2. pip install pyotp requests --break-system-packages
  3. Get a Railway API token: railway.app -> account settings -> Tokens ->
     create one scoped to this project.
  4. Find your Railway project ID, service ID, and environment ID — in the
     Railway dashboard, click into the service, the URL contains all three:
     railway.app/project/<PROJECT_ID>/service/<SERVICE_ID>?environmentId=<ENV_ID>

Required env vars (set these in the Railway Cron Job service, NOT the main
app service — this script runs separately from app.py):
    FYERS_CLIENT_ID       e.g. XXXXX-100
    FYERS_SECRET_KEY
    FYERS_REDIRECT_URI    must match the app registered at myapi.fyers.in
    FYERS_FY_ID           your Fyers login ID (e.g. XY00000)
    FYERS_PIN             your 4-digit trading PIN
    FYERS_TOTP_SECRET     from step 1 above
    RAILWAY_API_TOKEN
    RAILWAY_PROJECT_ID
    RAILWAY_SERVICE_ID    the MAIN APP's service ID (the one to update/redeploy)
    RAILWAY_ENVIRONMENT_ID
"""
import os
import sys
import time
import json
import logging

import pyotp
import requests
from fyers_apiv3 import fyersModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

FYERS_LOGIN_BASE = "https://api-t2.fyers.in/vagator2/v2"
FYERS_TOKEN_BASE = "https://api-t1.fyers.in/api/v3"
RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def headless_login() -> str:
    """Returns a fresh Fyers access_token, with no human interaction."""
    client_id = _env("FYERS_CLIENT_ID")
    secret_key = _env("FYERS_SECRET_KEY")
    redirect_uri = _env("FYERS_REDIRECT_URI")
    fy_id = _env("FYERS_FY_ID")
    pin = _env("FYERS_PIN")
    totp_secret = _env("FYERS_TOTP_SECRET")

    session = requests.Session()

    # Step 1: send_login_otp — tells Fyers which user is logging in
    r1 = session.post(f"{FYERS_LOGIN_BASE}/send_login_otp",
                       json={"fy_id": _obfuscate(fy_id), "app_id": "2"},
                       timeout=15)
    r1.raise_for_status()
    request_key = r1.json()["request_key"]

    # Step 2: verify the TOTP code (generated live, same as an authenticator app would)
    totp_code = pyotp.TOTP(totp_secret).now()
    r2 = session.post(f"{FYERS_LOGIN_BASE}/verify_otp",
                       json={"request_key": request_key, "otp": totp_code}, timeout=15)
    r2.raise_for_status()
    request_key_2 = r2.json()["request_key"]

    # Step 3: verify the trading PIN
    r3 = session.post(f"{FYERS_LOGIN_BASE}/verify_pin",
                       json={"request_key": request_key_2, "identity_type": "pin", "identifier": _obfuscate(pin)},
                       timeout=15)
    r3.raise_for_status()
    access_token_internal = r3.json()["data"]["access_token"]

    # Step 4: use that short-lived internal token to complete the normal
    # OAuth code flow (same endpoint the login redirect would normally hit)
    headers = {"authorization": f"Bearer {access_token_internal}"}
    token_payload = {
        "fyers_id": fy_id, "app_id": client_id.split("-")[0],
        "redirect_uri": redirect_uri, "appType": client_id.split("-")[1] if "-" in client_id else "100",
        "code_challenge": "", "state": "auto", "scope": "", "nonce": "", "response_type": "code",
        "create_cookie": True,
    }
    r4 = session.post(f"{FYERS_TOKEN_BASE}/token", json=token_payload, headers=headers, timeout=15)
    r4.raise_for_status()
    redirect_url = r4.json()["Url"]
    auth_code = redirect_url.split("auth_code=")[1].split("&")[0]

    # Step 5: exchange auth_code for the real access_token, same as the
    # manual fyers_auth.py flow — this part IS the documented, stable SDK call
    session_model = fyersModel.SessionModel(
        client_id=client_id, secret_key=secret_key, redirect_uri=redirect_uri,
        response_type="code", grant_type="authorization_code",
    )
    session_model.set_token(auth_code)
    response = session_model.generate_token()
    if "access_token" not in response:
        raise RuntimeError(f"Token exchange failed: {response}")

    return response["access_token"]


def _obfuscate(value: str) -> str:
    """Fyers' login endpoints expect base64-ish obfuscated field values for
    fy_id/pin, not plaintext — this matches what their own login page sends.
    If Fyers changes this encoding, this is the first thing to check."""
    import base64
    return base64.b64encode(value.encode()).decode()


def update_railway_env_and_redeploy(access_token: str):
    api_token = _env("RAILWAY_API_TOKEN")
    project_id = _env("RAILWAY_PROJECT_ID")
    service_id = _env("TARGET_SERVICE_ID")
    environment_id = _env("RAILWAY_ENVIRONMENT_ID")

    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}

    # 1. Update the FYERS_ACCESS_TOKEN variable
    set_var_mutation = {
        "query": """
            mutation VariableUpsert($input: VariableUpsertInput!) {
                variableUpsert(input: $input)
            }
        """,
        "variables": {
            "input": {
                "projectId": project_id, "environmentId": environment_id,
                "serviceId": service_id, "name": "FYERS_ACCESS_TOKEN", "value": access_token,
            }
        },
    }
    resp = requests.post(RAILWAY_GRAPHQL_URL, json=set_var_mutation, headers=headers, timeout=20)
    resp.raise_for_status()
    result = resp.json()
    if "errors" in result:
        raise RuntimeError(f"Railway variable update failed: {result['errors']}")
    logger.info("FYERS_ACCESS_TOKEN updated on Railway.")

    # 2. Trigger a redeploy so the running app actually picks up the new token
    #    (env var changes don't hot-reload into a running process)
    redeploy_mutation = {
        "query": """
            mutation ServiceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
                serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
            }
        """,
        "variables": {"serviceId": service_id, "environmentId": environment_id},
    }
    resp2 = requests.post(RAILWAY_GRAPHQL_URL, json=redeploy_mutation, headers=headers, timeout=20)
    resp2.raise_for_status()
    result2 = resp2.json()
    if "errors" in result2:
        raise RuntimeError(f"Railway redeploy trigger failed: {result2['errors']}")
    logger.info("Redeploy triggered — the app will restart with the fresh token in a minute or two.")


def main():
    logger.info("Starting headless Fyers login...")
    try:
        access_token = headless_login()
    except Exception as e:
        logger.error(f"Headless login failed: {e}")
        logger.error("Fallback: run fyers_auth.py manually and set FYERS_ACCESS_TOKEN "
                      "on Railway by hand for today.")
        sys.exit(1)

    logger.info("Got a fresh access token. Updating Railway...")
    try:
        update_railway_env_and_redeploy(access_token)
    except Exception as e:
        logger.error(f"Railway update failed: {e}")
        logger.error(f"You have a valid token though — set it manually: {access_token[:12]}...")
        sys.exit(1)

    logger.info("Done.")


if __name__ == "__main__":
    main()
