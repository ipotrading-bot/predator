"""backfill_ledger.py — PAIM v9.1 — One-shot repair script
Context: signals with status IN ('closed','expired','settled') were audited
successfully (clv_pct/closing_line/closed_at are filled in `signals`), but
the insert into `ai_learning_ledger` failed silently with PGRST204 because
the ledger table was missing the 'match' and 'outcome' columns (fixed by
sql/migrate_v8_9.sql). Those signals will NEVER be retried by audit_engine
because fetch_pending() only selects status='active'.
This script re-reads every terminal-status signal from `signals` and
inserts the corresponding row into `ai_learning_ledger`, skipping any
signal_id that's already present (safe to re-run).
Usage:
  python backfill_ledger.py
"""
import logging
import time
from datetime import datetime
from dotenv import load_dotenv

from core.db import get_db, MissingCredentialsError

load_dotenv()

_fmt = logging.Formatter(fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_fmt.converter = time.gmtime
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("BACKFILL")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False

TERMINAL_STATUSES = ["settled", "closed", "expired"]

def _ttm(match_time, scanned_at):
    if not (match_time and scanned_at):
        return None
    try:
        mt = datetime.fromisoformat(match_time.replace("Z", "+00:00"))
        sc = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
        return int((mt - sc).total_seconds() / 60)
    except Exception:
        return None

def run():
    # This is a write-only repair script, never a public-facing reader —
    # write=True fails fast if SUPABASE_SERVICE_KEY is missing/wrong instead
    # of silently degrading to the anon key (RLS would reject every insert).
    try:
        sb = get_db(write=True)
    except MissingCredentialsError as e:
        log.critical("%s", e)
        raise SystemExit(1)

    # 1) Which signal_ids are already in the ledger? (avoid duplicates)
    existing = sb.table("ai_learning_ledger").select("signal_id").execute()
    already = {r["signal_id"] for r in (existing.data or []) if r.get("signal_id") is not None}
    log.info("Ledger already has %d entries with signal_id set", len(already))

    # 2) Pull every terminal-status signal
    all_signals = []
    for status in TERMINAL_STATUSES:
        res = (sb.table("signals")
               .select("*")
               .eq("status", status)
               .execute())
        rows = res.data or []
        log.info("signals status=%s → %d rows", status, len(rows))
        all_signals.extend(rows)

    log.info("Total terminal-status signals found: %d", len(all_signals))

    inserted, skipped, failed = 0, 0, 0
    for sig in all_signals:
        sig_id = sig.get("id")
        if sig_id in already:
            skipped += 1
            continue

        clv = sig.get("clv_pct")
        outcome = sig.get("outcome") or sig.get("status")  # fallback to status (closed/expired) if outcome not set
        if clv is None:
            # No CLV means audit never actually completed for this row — skip
            skipped += 1
            continue

        payload = {
            "signal_id":             sig_id,
            "match":                 sig.get("match"),
            "sport":                 sig.get("sport"),
            "league":                sig.get("league"),
            "market_type":           sig.get("market_key"),
            "market":                sig.get("market"),
            "selection":             sig.get("selection_name"),
            "odds":                  sig.get("xbet_odd"),
            "time_to_match_minutes": _ttm(sig.get("match_time"), sig.get("scanned_at")),
            "initial_edge":          sig.get("edge_pct"),
            "sharp_divergence_std":  None,
            "clv_final":             clv,
            "was_clv_positive":      clv >= 0,
            "outcome":               outcome,
        }
        try:
            sb.table("ai_learning_ledger").insert(payload).execute()
            inserted += 1
        except Exception as e:
            failed += 1
            log.warning("Insert failed for signal_id=%s (%s): %s", sig_id, sig.get("match"), e)

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("Backfill done: %d inserted | %d skipped (already present/no CLV) | %d failed",
              inserted, skipped, failed)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

if __name__ == "__main__":
    run()
