"""
core/audit_engine.py — PAIM v8.5 — Settlement + CLV Audit
Runs every 6h via GitHub Actions (run_audit.py entry point).

Pipeline for each signal whose match kicked off > SETTLEMENT_GRACE_H hours ago
(or, for legacy rows with no match_time, scanned > AUDIT_LAG_H hours ago) with
status='active':
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

AUDIT_LAG_H         = int(os.environ.get("AUDIT_LAG_H", 3))          # legacy fallback: scanned_at age, only used when match_time is missing
SETTLEMENT_GRACE_H  = int(os.environ.get("SETTLEMENT_GRACE_H", 4))   # hours after match_time before we even attempt audit
ORACLE_BUDGET = 30     # Max Gemini oracle calls per audit run
SETTLE_BUDGET = 25     # Max Gemini settlement calls per audit run

_AUDIT_COLS = {"closing_line", "clv_pct", "closed_at"}

# Terminal statuses — Ledger reads all of these
TERMINAL_STATUSES = ["settled", "closed", "expired"]


def fetch_pending(sb) -> list[dict]:
    """
    Active signals ready to audit.

    BUGFIX: this used to gate purely on `scanned_at` age (3h after scan),
    with no regard for `match_time`. A signal scanned hours or days ahead of
    its kickoff would get audited — and, since the match hadn't even started,
    Pass 1 (real settlement) always failed and Pass 2 immediately CLV-closed
    it FOREVER (fetch_pending only ever selects status='active', so a closed
    signal is never retried). That silently guaranteed most signals would
    never get a real WIN/LOSS outcome. Gating on match_time + a grace period
    instead ensures the match has actually had time to finish before we give
    up on real settlement and fall back to a CLV-only close.
    """
    now = datetime.now(timezone.utc)
    match_cutoff   = (now - timedelta(hours=SETTLEMENT_GRACE_H)).isoformat()
    scanned_cutoff = (now - timedelta(hours=AUDIT_LAG_H)).isoformat()
    rows: list[dict] = []
    try:
        res = (sb.table("signals")
               .select("*")
               .eq("status", "active")
               .lt("match_time", match_cutoff)
               .order("match_time", desc=False)
               .limit(100)
               .execute())
        rows.extend(res.data or [])
    except Exception as e:
        log.error("fetch_pending (match_time): %s", e)
    try:
        # Legacy rows with no match_time recorded — fall back to scan age.
        res2 = (sb.table("signals")
                .select("*")
                .eq("status", "active")
                .is_("match_time", "null")
                .lt("scanned_at", scanned_cutoff)
                .limit(100)
                .execute())
        rows.extend(res2.data or [])
    except Exception as e:
        log.error("fetch_pending (legacy scanned_at): %s", e)
    return rows


def _update_signal(sb, sig: dict, payload: dict) -> bool:
    """
    Persist audit result via DELETE + INSERT (RLS anon key blocks UPDATE).
    sig is the full signal row from fetch_pending(); payload contains the new fields.
    Returns True on success, False if the signal was lost.
    """
    sig_id = sig["id"]
    merged = {**sig, **payload}
    merged.pop("id", None)  # Supabase will reject explicit id on insert
    try:
        sb.table("signals").delete().eq("id", sig_id).execute()
    except Exception as e:
        log.error("Delete signal %s: %s", sig_id, e)
        return False
    try:
        sb.table("signals").insert(merged).execute()
        return True
    except Exception as e:
        # Retry without optional audit columns if schema is stale
        if any(c in str(e) for c in _AUDIT_COLS):
            core = {k: v for k, v in merged.items() if k not in _AUDIT_COLS}
            try:
                sb.table("signals").insert(core).execute()
                return True
            except Exception as e2:
                log.critical("SIGNAL %s LOST after delete — fallback insert failed: %s", sig_id, e2)
                return False
        log.critical("SIGNAL %s LOST after delete — insert failed: %s", sig_id, e)
        return False


def _log_to_ledger(sb, sig: dict, clv: float, outcome: str):
    match_time = sig.get("match_time")
    scanned_at = sig.get("scanned_at")
    ttm = None
    if match_time and scanned_at:
        try:
            mt = datetime.fromisoformat(match_time.replace("Z", "+00:00"))
            sc = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
            ttm = int((mt - sc).total_seconds() / 60)
        except Exception:
            log.debug("_ttm parse failed for match_time=%s scanned_at=%s", match_time, scanned_at)
    try:
        sb.table("ai_learning_ledger").insert({
            "signal_id":             sig.get("id"),
            "match":                 sig["match"],
            "sport":                 sig.get("sport"),
            "league":                sig.get("league"),
            "market_type":           sig.get("market_key"),
            "market":                sig.get("market"),
            "selection":             sig.get("selection_name"),
            "odds":                  sig.get("xbet_odd"),
            "time_to_match_minutes": ttm,
            "initial_edge":          sig.get("edge_pct"),
            "sharp_divergence_std":  None,
            "clv_final":             clv,
            "was_clv_positive":      clv > 0,
            "outcome":               outcome,
        }).execute()
    except Exception as e:
        # See core/settlement.py's settle_signal for why this is CRITICAL,
        # not a routine warning — a schema mismatch here means /performance
        # and the learning layer silently never see this outcome.
        log.critical("ai_learning_ledger INSERT FAILED [%s] — check migration "
                      "sql/migrate_v9_4_ledger_display_fields.sql is applied: %s",
                      sig.get("match"), e)


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

    ok = _update_signal(sb, sig, {
        "status":       status,
        "clv_pct":      float(clv),
        "closing_line": float(closing_price) if closing_price else None,
        "closed_at":    now_iso,
    })
    if ok:
        _log_to_ledger(sb, sig, float(clv), status)
    else:
        log.error("Skipping ledger write for lost signal %s", sig["id"])
    return status


def run():
    # Prefer service_role (bypasses RLS, meant for backend writes) — fall
    # back to the anon key for backward compatibility until the secret is
    # configured. This module only ever deletes/inserts/upserts, it never
    # serves public reads, so it has no business using the anon key.
    sb = create_client(
        os.environ["SUPABASE_URL"],
        os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"],
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
