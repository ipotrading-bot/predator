"""
core/audit_engine.py — PAIM v8.5 — Closing Line Value Audit
Runs every 6h via GitHub Actions (run_audit.py entry point).

For each signal scanned > AUDIT_LAG_H hours ago with status='active':
  1. Call Oracle (Gemini Search) to fetch current Pinnacle price.
  2. If a price is found → closing_line = that price, status = 'closed'.
  3. Otherwise market has shut → closing_line = pinnacle_price at scan time,
     status = 'expired' (conservative: we assume Sharp closed efficiently).
  4. CLV = (xbet_odd / closing_line − 1) × 100
     CLV > 0  → we captured a real inefficiency (beat the closing line).
     CLV < 0  → the edge had already been arbed away before our scan.

After all signals are processed, trigger learning_layer.compute_and_save()
to update sport-specific MIN_EDGE thresholds.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

from core.learning_layer import compute_and_save as _learn
from core.oracle import get_pinnacle_price

load_dotenv()

_fmt = logging.Formatter(
    fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_fmt.converter = time.gmtime
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("AUDIT")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False

AUDIT_LAG_H   = 3     # Wait this many hours after scan before auditing
ORACLE_BUDGET = 10    # Max Gemini calls per audit run (API rate-limit guard)

_AUDIT_COLS = {"closing_line", "clv_pct", "closed_at"}


def fetch_pending(sb) -> list[dict]:
    """Signals scanned > AUDIT_LAG_H ago that are still active."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=AUDIT_LAG_H)).isoformat()
        res = (sb.table("signals")
               .select("*")
               .eq("status", "active")
               .lt("scanned_at", cutoff)
               .order("scanned_at", desc=False)
               .limit(50)
               .execute())
        return res.data or []
    except Exception as e:
        log.error("fetch_pending: %s", e)
        return []


def _update_signal(sb, sig_id: str, payload: dict):
    """Persist audit result; retries without new columns if schema is stale."""
    try:
        sb.table("signals").update(payload).eq("id", sig_id).execute()
    except Exception as e:
        if any(c in str(e) for c in _AUDIT_COLS):
            core = {k: v for k, v in payload.items() if k not in _AUDIT_COLS}
            try:
                sb.table("signals").update(core).eq("id", sig_id).execute()
            except Exception as e2:
                log.error("Update signal %s (fallback): %s", sig_id, e2)
        else:
            log.error("Update signal %s: %s", sig_id, e)


def audit_one(sb, sig: dict, oracle_calls: list) -> str:
    """
    Audit a single signal. oracle_calls is a mutable [remaining] counter.
    Returns the new status string ('closed' or 'expired').
    """
    match  = sig["match"]
    sport  = sig.get("sport", "soccer")
    league = sig.get("league", "")
    now    = datetime.now(timezone.utc)

    closing_price: float | None = None

    if oracle_calls[0] > 0:
        oracle_calls[0] -= 1
        try:
            price, _ = get_pinnacle_price(match, sport=sport, league=league)
            if price and price > 1.01:
                closing_price = price
        except Exception as e:
            log.warning("Oracle [%s]: %s", match, e)

    if closing_price:
        clv    = round((sig["xbet_odd"] / closing_price - 1) * 100, 2)
        status = "closed"
        icon   = "✓" if clv >= 0 else "✗"
        log.info("CLV %+.2f%% %s | %s", clv, icon, match)
    else:
        # Market closed — use original Pinnacle price as closing proxy
        orig_pin = sig.get("pinnacle_price") or 0.0
        if orig_pin > 1.01:
            clv = round((sig["xbet_odd"] / orig_pin - 1) * 100, 2)
        else:
            clv = 0.0
        closing_price = orig_pin
        status = "expired"
        log.info("EXPIRED  | %s (proxy CLV %+.2f%%)", match, clv)

    _update_signal(sb, sig["id"], {
        "status":       status,
        "clv_pct":      float(clv),
        "closing_line": float(closing_price) if closing_price else None,
        "closed_at":    now.isoformat(),
    })
    return status


def run():
    sb = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_KEY"],
    )

    pending = fetch_pending(sb)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("PAIM AUDIT v8.5 — %d signals pending", len(pending))

    if not pending:
        log.info("Nothing to audit.")
        log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return

    budget  = [ORACLE_BUDGET]
    closed  = 0
    expired = 0

    for sig in pending:
        status = audit_one(sb, sig, budget)
        if status == "closed":
            closed += 1
        else:
            expired += 1

    log.info("Audit done: %d closed | %d expired | Oracle: %d/%d calls used",
             closed, expired, ORACLE_BUDGET - budget[0], ORACLE_BUDGET)

    # Update adaptive thresholds with new CLV data
    log.info("--- Learning Layer ---")
    try:
        _learn(sb)
    except Exception as e:
        log.error("Learning layer: %s", e)

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
