"""
telegram_alert.py
------------------
Sends Gamma Blast alerts to Telegram, formatted like your example.

Setup:
  1. Message @BotFather on Telegram, /newbot, follow prompts -> get a bot token
  2. Message your new bot anything once (so it can message you back), then
     get your chat_id from https://api.telegram.org/bot<TOKEN>/getUpdates
     (look for "chat":{"id": ...} in the response)
  3. Set env vars:
       export TELEGRAM_BOT_TOKEN=123456:ABC-your-token
       export TELEGRAM_CHAT_ID=your_chat_id
"""
import os
import requests

from models import GammaScore

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_pct(x):
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.0f}%"


def format_alert(score: GammaScore) -> str:
    ks = score.key_strike
    fut = score.futures

    ce_oi_change = None
    if ks is not None and ks.option_type == "CE":
        ce_oi_change = ks.oi_change_pct

    lines = [
        "\U0001F6A8 GAMMA SQUEEZE ALERT",
        f"\U0001F4CC SYMBOL: {score.symbol}",
        f"\U0001F4B0 PRICE: \u20B9{score.price:,.0f}",
        f"\U0001F525 SCORE: {score.total_score:.0f}/100",
        "",
        "CALL SIDE",
        f"OI Change: {_fmt_pct(ce_oi_change)}",
        f"IV component: {score.components['iv']:.0f}/100",
        "",
    ]

    if fut is not None:
        lines += [
            "FUTURES",
            f"OI Change: {_fmt_pct(fut.oi_change_pct)}",
            f"Price: {_fmt_pct(fut.price_change_pct)}",
        ]
        if fut.oi_change_pct is not None and fut.oi_change_pct < 0 and \
           fut.price_change_pct is not None and fut.price_change_pct > 0:
            lines.append("\u2192 Short covering detected")
        lines.append("")

    if ks is not None:
        lines.append(f"\U0001F3AF Key Strike: {ks.strike:.0f} {ks.option_type}")

    setup = "HIGH MOMENTUM" if score.alert_level == "strong" else score.alert_level.upper()
    lines.append(f"\u26A1 Setup: {setup}")
    lines.append("")
    lines.append("\u26A0\uFE0F Signal only — not financial advice.")

    return "\n".join(lines)


def send_alert(score: GammaScore) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False  # not configured — caller can still show the alert in-dashboard

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
