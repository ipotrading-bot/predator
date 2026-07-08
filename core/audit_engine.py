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

from core.db import get_db, log_to_ledger, replace_signal_row, MissingCredentialsError
from core.http_utils import gemini_quota_dead
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

# Task 3 — real closing-line capture (run_closing_line.py, hourly cadence)
CLOSING_LINE_WINDOW_MIN = 5    # minutes before kickoff a price counts as "the closing line"
CLOSING_LINE_BUDGET     = 30   # Max Gemini oracle calls per closing-line run

_AUDIT_COLS = {"closing_line", "clv_pct", "closed_at"}
_CLOSING_LINE_COLS = {"closing_pinnacle_price", "clv_pct_real"}

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
    """Persist audit result via DELETE + INSERT (RLS blocks UPDATE outright
    on this table's policies). Returns True on success, False if the signal
    was lost. Shared with core/settlement.py's settle_signal() — see
    core/db.py:replace_signal_row for the implementation."""
    merged = {**sig, **payload}
    return replace_signal_row(sb, sig["id"], merged, optional_cols=_AUDIT_COLS)


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

    # Both passes below are Gemini-backed. Once the DAILY quota is dead,
    # Pass 1 can never settle and Pass 2 can never fetch a closing line —
    # proceeding would stamp this signal 'expired' (terminal, never
    # retried) with a garbage proxy CLV. Leave it 'active' instead so the
    # next 6h audit run — after the quota reset — settles it for real.
    if gemini_quota_dead():
        return "skipped"

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
        log_to_ledger(sb, sig, float(clv), status)
    else:
        log.error("Skipping ledger write for lost signal %s", sig["id"])
    return status


def fetch_closing_line_candidates(sb) -> list[dict]:
    """
    Active signals whose match kicks off within the next
    CLOSING_LINE_WINDOW_MIN minutes and haven't had a closing price
    captured yet — the genuine "closing line" window, as opposed to this
    module's own Pass 2 CLV (fetched hours-to-days after kickoff during
    the 6h audit, at best a proxy) or core/settlement.py's entry-edge
    re-derivation (see sql/migrate_v9_5_learning_integrity.sql).
    """
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=CLOSING_LINE_WINDOW_MIN)
    try:
        res = (sb.table("signals")
               .select("*")
               .eq("status", "active")
               .is_("closing_pinnacle_price", "null")
               .gte("match_time", now.isoformat())
               .lte("match_time", window_end.isoformat())
               .limit(100)
               .execute())
        return res.data or []
    except Exception as e:
        log.error("fetch_closing_line_candidates: %s", e)
        return []


def capture_closing_lines(sb, budget: int = CLOSING_LINE_BUDGET) -> int:
    """
    For each signal within CLOSING_LINE_WINDOW_MIN of kickoff, re-fetch the
    current Pinnacle consensus price and store it as closing_pinnacle_price
    — a real closing-line measurement, distinct from pinnacle_price
    (captured at scan time, possibly hours or days earlier). clv_pct_real
    is derived immediately and does NOT wait for the match outcome (that
    remains core/settlement.py's real WIN/LOSS job) — it's an early,
    complementary quality signal: if the line moved against the scanned
    price by kickoff, the original edge was more likely stale data than a
    real market inefficiency. Runs hourly via run_closing_line.py, not as
    part of the 6h audit — this window (kickoff ± 5min) rarely aligns with
    that coarser cadence.
    """
    candidates = fetch_closing_line_candidates(sb)
    remaining = [budget]
    captured = 0
    for sig in candidates:
        if remaining[0] <= 0:
            log.warning("CLOSING LINE — Gemini oracle budget exhausted, %d signal(s) left for next run",
                        len(candidates) - captured)
            break
        remaining[0] -= 1
        try:
            price, _ = get_pinnacle_price(sig["match"], sport=sig.get("sport", "soccer"),
                                          league=sig.get("league", ""))
        except Exception as e:
            log.warning("capture_closing_lines oracle [%s]: %s", sig["match"], e)
            continue
        if not price or price <= 1.01:
            continue

        scan_price = sig.get("pinnacle_price") or 0.0
        clv_real = round((scan_price / price - 1) * 100, 2) if scan_price > 1.01 else None

        ok = replace_signal_row(sb, sig["id"], {**sig, **{
            "closing_pinnacle_price": float(price),
            "clv_pct_real":           clv_real,
        }}, optional_cols=_CLOSING_LINE_COLS)
        if ok:
            captured += 1
            log.info("CLOSING LINE | %s | scan %.3f -> close %.3f | CLV_real %+.2f%%",
                     sig["match"], scan_price, price, clv_real if clv_real is not None else 0.0)
        else:
            log.error("Failed to persist closing line for signal %s", sig["id"])
    return captured


def run_closing_lines():
    """Entry point for run_closing_line.py — hourly job, independent of
    the 6h settlement/CLV audit in run() below."""
    try:
        sb = get_db(write=True)
    except MissingCredentialsError as e:
        log.critical("%s", e)
        raise SystemExit(1)

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("PAIM CLOSING LINE v9.5 — capturing real closing prices")
    n = capture_closing_lines(sb)
    log.info("Closing-line capture done: %d signal(s) updated", n)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def run():
    # This module only ever deletes/inserts/upserts, it never serves public
    # reads — write=True fails fast and loud if SUPABASE_SERVICE_KEY is
    # missing or resolves to the wrong role, instead of silently falling
    # back to the anon key and failing every write downstream with RLS 42501.
    try:
        sb = get_db(write=True)
    except MissingCredentialsError as e:
        log.critical("%s", e)
        raise SystemExit(1)

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
    counts = {"settled": 0, "closed": 0, "expired": 0, "skipped": 0}

    for i, sig in enumerate(pending):
        if gemini_quota_dead():
            counts["skipped"] += len(pending) - i
            log.warning("Gemini daily quota exhausted — %d signals left active "
                        "for the next audit run (post quota reset ~07:00 UTC)",
                        len(pending) - i)
            break
        status = audit_one(sb, sig, oracle_budget, settle_budget, now)
        counts[status] = counts.get(status, 0) + 1

    log.info("Audit done: %d settled | %d closed | %d expired | %d skipped",
             counts["settled"], counts["closed"], counts["expired"], counts["skipped"])
    log.info("Oracle: %d/%d | Settlement: %d/%d calls used",
             ORACLE_BUDGET - oracle_budget[0], ORACLE_BUDGET,
             SETTLE_BUDGET - settle_budget[0], SETTLE_BUDGET)

    log.info("--- Learning Layer ---")
    try:
        _learn(sb)
    except Exception as e:
        log.error("Learning layer: %s", e)

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
