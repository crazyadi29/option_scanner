"""
telegram_alert.py
------------------
Sends Gamma Blast alerts to Telegram.

Setup:
  1. Message @BotFather on Telegram, /newbot, follow prompts -> get a bot token
  2. Message your new bot anything once, then get your chat_id from
     https://api.telegram.org/bot<TOKEN>/getUpdates (look for "chat":{"id": ...})
  3. export TELEGRAM_BOT_TOKEN=... / export TELEGRAM_CHAT_ID=...
"""
import os
import requests

from models import GammaScore

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def format_alert(score: GammaScore) -> str:
    ks = score.key_strike
    c = score.components

    lines = [
        "\U0001F6A8 GAMMA SQUEEZE ALERT",
        f"\U0001F4CC SYMBOL: {score.symbol}",
        f"\U0001F4B0 PRICE: \u20B9{score.price:,.2f}",
        f"\U0001F525 SCORE: {score.total_score:.0f}/100",
        "",
        score.setup_note,
        "",
    ]

    if ks is not None:
        lines.append(f"\U0001F3AF Key Strike: {ks.strike:.0f} {ks.option_type}")
        if ks.oi:
            lines.append(f"Current OI: {ks.oi:,}")

    lines += [
        "",
        f"OI Unwind: {c.get('oi_unwind', 0):.0f}/100",
        f"Volume Rise: {c.get('volume_rise', 0):.0f}/100",
        f"Premium Rise: {c.get('premium_rise', 0):.0f}/100",
        "",
        "\u2192 Short covering pattern detected (OI down, volume up, premium up "
        "over consecutive 15-min candles).",
        "",
        "\u26A0\uFE0F Signal only — not financial advice.",
    ]

    return "\n".join(lines)


def send_alert(score: GammaScore) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    text = format_alert(score)
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False
