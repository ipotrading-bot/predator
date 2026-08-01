"""
core/db.py — Single source of truth for Supabase client creation.

RLS policies restrict INSERT/UPDATE/DELETE on signals/ai_learning_ledger/meta
to the service_role. Every write call site in this repo used to roll its own
`os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")`
fallback — which authenticates fine on the anon key (no 401) and then fails
every single write with a Postgres 42501 (row-level security), one row at a
time, with no single clear error pointing at the actual cause. That's what
produced the 2026-07-07 incident: ~17h of "0/N signals persisted" across
every scheduled run, each showing green (success) in GitHub Actions because
nothing ever raised.

get_db(write=True) fails fast and loud instead: it resolves the key's
privilege level locally (no network call) and refuses to hand back a client
if it isn't actually service_role — so a wrong/missing key aborts the run in
its first second with an unambiguous message, not 17 hours later across a
wall of per-signal RLS errors. This covers both Supabase key formats: the
legacy JWT (role claim in the payload) and the newer opaque
sb_secret_.../sb_publishable_... keys (role is the prefix) — the first
version of this fix only handled the JWT format, which passed on GitHub
Actions but kept failing on Vercel because that deployment's key was the
newer format and decoded to role=None instead of a clear anon/service_role
verdict.
"""
import base64
import json
import logging
import os

from supabase import create_client

log = logging.getLogger("PREDATOR.db")


class MissingCredentialsError(RuntimeError):
    pass


def _key_role(token: str) -> str | None:
    """Identify the privilege level of a Supabase API key, old or new format.

    Supabase has two key formats in the wild:
    - Legacy JWT (still issued for most projects as of writing): a
      `header.payload.signature` token whose payload has a `role` claim of
      `anon` or `service_role`.
    - New-style keys (`sb_publishable_...` / `sb_secret_...`): opaque
      strings, not JWTs — the role is encoded in the prefix itself, there's
      nothing to decode. A project provisioned/migrated after Supabase's
      2025 key rotation shows these on its dashboard instead of (or beside)
      the legacy anon/service_role JWTs.

    Returns 'anon', 'service_role', or None if neither format is recognized.
    """
    if token.startswith("sb_secret_"):
        return "service_role"
    if token.startswith("sb_publishable_"):
        return "anon"
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)  # restore stripped b64 padding
        payload = json.loads(base64.urlsafe_b64decode(segment))
        return payload.get("role")
    except Exception:
        return None


def get_db(write: bool = False):
    """
    Returns a configured Supabase client, or None if the DB isn't configured
    at all (no SUPABASE_URL, or no usable key for the requested mode) —
    callers that treat "no DB" as a soft/optional condition should check for
    None.

    write=False (default): read-only usage. The anon key is correct here —
      RLS SELECT policies allow it, and reads should never require the more
      privileged service_role key.

    write=True: caller intends to INSERT/UPDATE/DELETE. Requires
      SUPABASE_SERVICE_KEY to be set AND to actually decode to
      role="service_role". Raises MissingCredentialsError immediately
      otherwise — never silently falls back to the anon key for writes.
    """
    url = os.environ.get("SUPABASE_URL")
    if not url:
        return None

    if write:
        service_key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not service_key:
            raise MissingCredentialsError(
                "SUPABASE_SERVICE_KEY is not set. Writes require the "
                "service_role key — RLS rejects INSERT/UPDATE/DELETE from "
                "the anon key. Refusing to silently fall back to SUPABASE_KEY."
            )
        role = _key_role(service_key)
        if role != "service_role":
            raise MissingCredentialsError(
                f"SUPABASE_SERVICE_KEY resolves to role={role!r}, not "
                "'service_role' — this is the anon/publishable key (or an "
                "unrecognized value), not the service_role/secret key. "
                "Writes will be rejected by RLS. Fix: Supabase dashboard -> "
                "Project Settings -> API Keys -> copy the 'service_role' "
                "(legacy JWT) or 'secret' (sb_secret_... new format) key "
                "into this GitHub/Vercel secret — not 'anon'/'publishable'."
            )
        return create_client(url, service_key)

    anon_key = os.environ.get("SUPABASE_KEY")
    if not anon_key:
        return None
    return create_client(url, anon_key)


# ── Shared delete+insert helpers (settlement & audit both replace a row in
#    place — Supabase RLS for the anon key blocks UPDATE outright, and the
#    service_role key still can't UPDATE a row whose primary key we don't
#    want to preserve across a status transition, so both go delete+insert) ──

def replace_signal_row(sb, signal_id, merged: dict, optional_cols: frozenset = frozenset()) -> bool:
    """
    Delete signal `signal_id` and re-insert `merged` (which should already
    contain the merged old+new fields, with 'id' still present or absent —
    it is stripped here either way since Supabase rejects an explicit id on
    insert into a serial/identity column).

    If the insert fails because of an unrecognized column (stale schema —
    a migration adding `optional_cols` hasn't been applied to this DB yet),
    retries once with those columns stripped. Returns True on success,
    False if the signal is lost (deleted but never successfully re-inserted
    — logged at CRITICAL since this is a silent data-loss path).
    """
    payload = dict(merged)
    payload.pop("id", None)
    try:
        sb.table("signals").delete().eq("id", signal_id).execute()
    except Exception as e:
        log.error("replace_signal_row delete [%s]: %s", signal_id, e)
        return False
    try:
        sb.table("signals").insert(payload).execute()
        return True
    except Exception as e:
        if optional_cols and any(c in str(e) for c in optional_cols):
            core = {k: v for k, v in payload.items() if k not in optional_cols}
            try:
                sb.table("signals").insert(core).execute()
                return True
            except Exception as e2:
                log.critical("SIGNAL %s LOST after delete — fallback insert failed: %s", signal_id, e2)
                return False
        log.critical("SIGNAL %s LOST after delete — insert failed: %s", signal_id, e)
        return False


def log_to_ledger(sb, sig: dict, clv: float, outcome: str) -> None:
    """Insert one row into ai_learning_ledger for a settled/closed/expired
    signal. Failure here is logged CRITICAL (not swallowed as routine) —
    it means /performance and the learning layer silently never see this
    outcome, most commonly because
    sql/migrate_v9_4_ledger_display_fields.sql hasn't been applied yet.

    `clv` is CLV only when the caller genuinely re-fetched a price at a
    later point in time (core/audit_engine.py's oracle pass); when the
    caller is core/settlement.py's settle_signal(), it is a re-derivation
    of the entry edge (identical to `initial_edge` below) and must never be
    used as a real closing-line signal — see core/settlement.py for why.
    core/learning_layer.py must key its threshold adjustments off `outcome`
    (real WIN/LOSS), never off this field.

    `kelly_pct` carries the Kelly stake sizing (% of bankroll) recorded on
    the signal at scan time, so a real ROI can later be computed as a
    stake-weighted average — Σ(kelly_pct·(odds-1)) if WIN else -kelly_pct,
    / Σ(kelly_pct) — instead of a flat per-bet average.

    `closing_pinnacle_price`/`clv_pct_real` carry core/audit_engine.py's
    genuine closing-line capture (Task 3, run_closing_line.py's hourly
    job) forward past this signal's eventual purge from `signals` — this
    is the only clv_pct_real* field that IS real CLV; see above for why
    `clv` isn't.

    `sharp_prob` carries the model's own predicted win probability at scan
    time — needed for a Brier score (core/stats_utils.py), which a bare
    win rate can't provide since it says nothing about whether the STATED
    confidence was trustworthy.

    All of the above require sql/migrate_v9_5_learning_integrity.sql,
    sql/migrate_v9_6_closing_line.sql, and sql/migrate_v9_7_ledger_brier.sql;
    until applied, the insert retries once with those columns stripped
    (same optional_cols pattern as replace_signal_row)."""
    match_time = sig.get("match_time")
    scanned_at = sig.get("scanned_at")
    ttm = None
    if match_time and scanned_at:
        try:
            from datetime import datetime as _dt
            mt = _dt.fromisoformat(match_time.replace("Z", "+00:00"))
            sc = _dt.fromisoformat(scanned_at.replace("Z", "+00:00"))
            ttm = int((mt - sc).total_seconds() / 60)
        except Exception:
            log.debug("_ttm parse failed for match_time=%s scanned_at=%s", match_time, scanned_at)
    # signals.sharp_sources is TEXT holding a JSON string (see _emit in
    # run_engine.py), but ai_learning_ledger.sharp_sources is jsonb
    # (sql/migrate_v9_10_ledger_consensus.sql). Inserting the raw string would
    # store a JSON *scalar*, so `sharp_sources->>'circa'` would return NULL —
    # exactly the query this column exists to serve. Decode it to a real object.
    sharp_sources = sig.get("sharp_sources")
    if isinstance(sharp_sources, str):
        try:
            sharp_sources = json.loads(sharp_sources)
        except (ValueError, TypeError):
            log.debug("sharp_sources not decodable, dropping: %.60s", sharp_sources)
            sharp_sources = None
    _optional = {
        "kelly_pct":              sig.get("kelly_pct"),
        "closing_pinnacle_price": sig.get("closing_pinnacle_price"),
        "clv_pct_real":           sig.get("clv_pct_real"),
        "sharp_prob":             sig.get("sharp_prob"),
        "sharp_sources":          sharp_sources,
        "consensus_score":        sig.get("consensus_score"),
    }
    payload = {
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
        **_optional,
    }
    try:
        sb.table("ai_learning_ledger").insert(payload).execute()
    except Exception as e:
        if any(c in str(e) for c in _optional):
            try:
                core = {k: v for k, v in payload.items() if k not in _optional}
                sb.table("ai_learning_ledger").insert(core).execute()
                return
            except Exception as e2:
                e = e2
        log.critical("ai_learning_ledger INSERT FAILED [%s] — check migrations "
                      "sql/migrate_v9_4_ledger_display_fields.sql, "
                      "sql/migrate_v9_5_learning_integrity.sql, "
                      "sql/migrate_v9_6_closing_line.sql, and "
                      "sql/migrate_v9_7_ledger_brier.sql are applied: %s",
                      sig.get("match"), e)
