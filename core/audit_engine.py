"""
core/audit_engine.py — PAIM v8.5 — Settlement + CLV Audit
Runs every 6h via GitHub Actions (run_audit.py entry point).

Pipeline for each signal scanned > AUDIT_LAG_H hours ago with status='active':
  1. Settlement pass — Gemini Search fetches real match score → status='settled'
     outcome = WIN | LOSS | PUSH | UNKNOWN
  2. CLV pass (if settlement failed) — fetch current Pinnacle closing line
     → status='closed' (real closing line) or 'expired' (proxy original price)
  3. CLV = (xbet_odd / closing_line − 1) × 100
  4. Learning Layer updates sport-specific MIN_EDGE thresholds.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

from core.learning_layer import compute_and_save as _learn
from core.oracle import get_pinnacle_price
from core.settlement import settle_signal

load_dotenv()

_fmt = logging.Formatter(fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_fmt.converter = time.gmtime
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("AUDIT")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False

AUDIT_LAG_H   = 3      # Wait this many hours after scan before auditing
ORACLE_BUDGET = 10     # Max Gemini oracle calls per audit run
SETTLE_BUDGET = 8      # Max Gemini settlement calls per audit run

_AUDIT_COLS = {"closing_line", "clv_pct", "closed_at"}

# Terminal statuses — Ledger reads all of these
TERMINAL_STATUSES = ["settled", "closed", "expired"]


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


def audit_one(sb, sig: dict, oracle_calls: list, settle_calls: list, now: datetime) -> str:
    """
    Audit a single signal.
    Pass 1: settlement (real score) → 'settled'
    Pass 2: CLV only             → 'closed' or 'expired'
    Returns the new status string.
    """
    match  = sig["match"]
    sport  = sig.get("sport", "soccer")
    league = sig.get("league", "")
    now_iso = now.isoformat()

    # ── Pass 1 : Settlement via real score ───────────────────────────
    if settle_calls[0] > 0:
        settle_calls[0] -= 1
        if settle_signal(sb, sig, now_iso):
            return "settled"
        log.info("No score yet for %s — falling back to CLV audit", match)

    # ── Pass 2 : CLV — fetch current Pinnacle closing line ────────────
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
        log.info("CLV %+.2f%% %s | %s", clv, "✓" if clv >= 0 else "✗", match)
    else:
        orig_pin = sig.get("pinnacle_price") or 0.0
        clv      = round((sig["xbet_odd"] / orig_pin - 1) * 100, 2) if orig_pin > 1.01 else 0.0
        closing_price = orig_pin
        status   = "expired"
        log.info("EXPIRED  | %s (proxy CLV %+.2f%%)", match, clv)

    _update_signal(sb, sig["id"], {
        "status":       status,
        "clv_pct":      float(clv),
        "closing_line": float(closing_price) if closing_price else None,
        "closed_at":    now_iso,
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

    now = datetime.now(timezone.utc)
    oracle_budget = [ORACLE_BUDGET]
    settle_budget = [SETTLE_BUDGET]
    counts = {"settled": 0, "closed": 0, "expired": 0}

    for sig in pending:
        status = audit_one(sb, sig, oracle_budget, settle_budget, now)
        counts[status] = counts.get(status, 0) + 1

    log.info("Audit done: %d settled | %d closed | %d expired",
             counts["settled"], counts["closed"], counts["expired"])
    log.info("Oracle: %d/%d | Settlement: %d/%d calls used",
             ORACLE_BUDGET - oracle_budget[0], ORACLE_BUDGET,
             SETTLE_BUDGET - settle_budget[0], SETTLE_BUDGET)

    log.info("--- Learning Layer ---")
    try:
        _learn(sb)
    except Exception as e:
        log.error("Learning layer: %s", e)

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
