"""
run_monte_carlo.py — PREDATOR PAIM v9.5 — Bankroll growth simulator entry point.
Manual/on-demand only (.github/workflows/tools.yml, input `monte_carlo`) —
a decision aid to run before any withdrawal, not a scheduled job. Bootstraps
from real ai_learning_ledger outcomes; refuses to run on too small a sample
rather than report a distribution built on noise.
"""
import logging
import os
import time

import requests
from dotenv import load_dotenv

from core.db import get_db
from core.monte_carlo import format_report, historical_returns, simulate

load_dotenv()

_fmt = logging.Formatter(fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_fmt.converter = time.gmtime
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("MONTE_CARLO")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False

MIN_HISTORICAL_SAMPLES = 30  # same bar as core/learning_layer.py's _MIN_SAMPLES


def _send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram non configuré — rapport affiché en log uniquement")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
                          timeout=15)
        if r.status_code != 200:
            log.error("Telegram HTTP %d: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("Telegram: %s", e)


def run():
    sb = get_db(write=False)
    if sb is None:
        log.critical("Supabase not configured (SUPABASE_URL/SUPABASE_KEY) — cannot run")
        raise SystemExit(1)

    try:
        res = sb.table("ai_learning_ledger").select("outcome, kelly_pct, odds").execute()
        rows = res.data or []
    except Exception as e:
        log.critical("Failed to read ai_learning_ledger: %s", e)
        raise SystemExit(1)

    returns = historical_returns(rows)
    if len(returns) < MIN_HISTORICAL_SAMPLES:
        msg = (f"Only {len(returns)} decisive settled signals with stake+odds data — "
               f"need at least {MIN_HISTORICAL_SAMPLES} before a bootstrap simulation "
               f"means anything. Not running.")
        log.warning(msg)
        raise SystemExit(0)

    result = simulate(returns)
    report = format_report(result)
    log.info("\n%s", report)
    _send_telegram(report)


if __name__ == "__main__":
    run()
