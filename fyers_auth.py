"""
fyers_auth.py
-------------
Fyers access tokens also expire daily (same story as Kite — broker security
model, not something we can code around). Run this each trading morning
before starting the dashboard.

Setup first:
  1. pip install fyers-apiv3 --break-system-packages
  2. Create an app at https://myapi.fyers.in (App Type: Web app is simplest)
     Note your Client ID (looks like "XXXXX-100") and Secret Key.
     Set a Redirect URL — for local testing, http://127.0.0.1:8000 is fine
     (you don't need anything actually listening there; you just copy the
     auth_code out of the URL the browser lands on, even if it shows an error page).

Run:
    export FYERS_CLIENT_ID=XXXXX-100
    export FYERS_SECRET_KEY=your_secret
    export FYERS_REDIRECT_URI=http://127.0.0.1:8000
    python3 fyers_auth.py
"""
import os
import json
from fyers_apiv3 import fyersModel

TOKEN_FILE = "fyers_token.json"


def main():
    client_id = os.environ.get("FYERS_CLIENT_ID")
    secret_key = os.environ.get("FYERS_SECRET_KEY")
    redirect_uri = os.environ.get("FYERS_REDIRECT_URI")

    if not all([client_id, secret_key, redirect_uri]):
        print("Set FYERS_CLIENT_ID, FYERS_SECRET_KEY, and FYERS_REDIRECT_URI env vars first.")
        return

    session = fyersModel.SessionModel(
        client_id=client_id, secret_key=secret_key, redirect_uri=redirect_uri,
        response_type="code", grant_type="authorization_code",
    )

    print("\n1. Open this URL, log in with your Fyers credentials + 2FA:")
    print(f"\n   {session.generate_authcode()}\n")
    print("2. You'll land on your redirect URL (it may show a browser error page —")
    print("   that's fine, you just need the URL). Copy the 'auth_code' parameter from it.")
    auth_code = input("\nPaste auth_code here: ").strip()

    session.set_token(auth_code)
    response = session.generate_token()

    if "access_token" not in response:
        print(f"\nFailed to generate token. Response: {response}")
        print("Common causes: auth_code expired (they're single-use, redo the login flow "
              "if this takes too long), or wrong secret_key/redirect_uri.")
        return

    access_token = response["access_token"]
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "client_id": client_id}, f)

    print(f"\nSaved access token to {TOKEN_FILE}. Valid until ~end of trading day. "
          f"Re-run this script tomorrow before starting the dashboard.")


if __name__ == "__main__":
    main()
