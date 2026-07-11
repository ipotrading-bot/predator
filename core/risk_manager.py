"""
core/risk_manager.py — PAIM v9.5 — Portfolio risk guardrails (Task 7).

Two independent safety layers on top of core/tax_engine.py's per-bet EV
check, which only ever asks "is THIS bet/system +EV net of tax?" and has
no notion of how much capital is already at risk across everything else
currently active:

  1. Exposure cap — total active stake across every currently
     status='active' signal must never exceed MAX_EXPOSURE_PCT of
     bankroll. get_current_exposure() sums it; callers reduce the
     bankroll they pass into kelly_stake()/suggest_system() by whatever
     is already committed, so a maxed-out portfolio naturally sizes new
     stakes down to 0 rather than stacking risk on top of risk.

  2. Circuit breaker — a rolling drawdown beyond DRAWDOWN_LIMIT_PCT over
     the last DRAWDOWN_WINDOW_N settled (real WIN/LOSS) signals pauses
     new signal emission entirely until a human calls resume_emission()
     — never automatically, even if the very next signal would have
     looked fine. A losing streak that big means something is off with
     the model or the market, not bad luck to bet through.

     check_circuit_breaker_by_sport() is the same mechanism scoped to one
     sport — the global check above can dilute/hide a sport in a genuine
     bad streak behind other sports performing fine, and conversely a
     global blowup concentrated in one sport pauses everything rather than
     just the sport that's actually broken. Both checks run independently;
     either one pausing is enough to stop that sport's (or everything's)
     outward Telegram recommendation.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("PREDATOR.risk_manager")

MAX_EXPOSURE_PCT    = 0.15   # % of bankroll — never more than this actively staked at once
DRAWDOWN_WINDOW_N    = 20    # last N decisive (WIN/LOSS) ledger rows for the circuit breaker
DRAWDOWN_LIMIT_PCT   = 0.25  # 25% rolling drawdown over that window trips the breaker

_PAUSE_KEY = "risk_circuit_breaker_paused"
_SPORT_PAUSE_KEY_PREFIX = "risk_circuit_breaker_paused_"


def get_current_exposure(sb, bankroll: float) -> float:
    """
    Sum of active stakes (currency units) across every signal currently
    status='active' — capital already committed to bets not yet settled.
    Uses kelly_pct (% of bankroll, already recorded on each signal at scan
    time — see run_engine.py's _emit()); a row missing kelly_pct
    contributes 0. Fails to 0.0 (not "unknown"/exception) on a read error
    — see get_exposure_headroom for why fail-open here is the deliberate,
    documented choice, not an oversight.
    """
    try:
        res = sb.table("signals").select("kelly_pct").eq("status", "active").execute()
        rows = res.data or []
    except Exception as e:
        log.error("get_current_exposure: %s — assuming 0 exposure", e)
        return 0.0
    total_pct = sum((r.get("kelly_pct") or 0.0) for r in rows)
    return total_pct / 100 * bankroll


def get_exposure_headroom(sb, bankroll: float, max_pct: float = MAX_EXPOSURE_PCT) -> float:
    """
    Currency units of bankroll still available before the exposure cap is
    hit. 0 or negative means no new stake should be sized — callers pass
    `max(0.0, headroom)` as the effective bankroll into
    kelly_stake()/suggest_system() so a maxed-out portfolio naturally
    produces stake=0 for new signals instead of stacking more risk on top.

    Fails open (returns the FULL bankroll, not 0) if the exposure read
    itself fails — a transient Supabase read error should not silently
    zero out every stake for the rest of the run; that failure mode is
    worse than temporarily not enforcing the cap for one scan cycle. This
    is the opposite fail-direction from the circuit breaker below on
    purpose: exposure is a soft cap on sizing, the circuit breaker is a
    hard stop on emission — losing the ability to check one should not
    silently become the other.
    """
    try:
        sb.table("signals").select("kelly_pct").eq("status", "active").limit(1).execute()
    except Exception as e:
        log.warning("get_exposure_headroom: %s — failing open, using full bankroll", e)
        return bankroll
    current = get_current_exposure(sb, bankroll)
    return max_pct * bankroll - current


def is_emission_paused(sb) -> bool:
    """True if the circuit breaker has tripped and not yet been manually
    cleared (see resume_emission())."""
    try:
        res = sb.table("meta").select("value").eq("key", _PAUSE_KEY).limit(1).execute()
        return bool(res.data) and res.data[0].get("value") == "true"
    except Exception as e:
        log.warning("is_emission_paused: %s — assuming not paused", e)
        return False


def rolling_drawdown(ledger_rows: list[dict]) -> float:
    """
    Rolling drawdown (fraction, e.g. 0.25 = 25%) over `ledger_rows` (any
    order — sorted oldest-first internally), using kelly_pct as the stake
    weight (same convention as core/learning_layer.py's ROI calc — a %
    of the same fixed reference bankroll, so it cancels correctly without
    needing the absolute € amount). Only WIN/LOSS rows with a kelly_pct
    count; PUSH/UNKNOWN/closed/expired carry no real result and are
    dropped, same as core/learning_layer.py. Returns 0.0 with fewer than
    2 decisive rows (nothing to draw down from).
    """
    decisive = [r for r in ledger_rows if r.get("outcome") in ("WIN", "LOSS") and r.get("kelly_pct")]
    if len(decisive) < 2:
        return 0.0

    # ledger rows are typically fetched newest-first (order("created_at",
    # desc=True)) — walk oldest-first to build a running equity curve.
    ordered = sorted(decisive, key=lambda r: r.get("created_at") or "")
    equity = 100.0   # arbitrary base — only the peak/trough RATIO matters
    curve = [equity]
    for r in ordered:
        stake = r["kelly_pct"]
        if r["outcome"] == "WIN":
            equity += stake * ((r.get("odds") or 2.0) - 1)
        else:
            equity -= stake
        curve.append(equity)

    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    return max_dd


def check_circuit_breaker(sb, window_n: int = DRAWDOWN_WINDOW_N,
                          limit_pct: float = DRAWDOWN_LIMIT_PCT) -> bool:
    """
    Evaluate the rolling drawdown over the last `window_n` ai_learning_ledger
    rows; if it exceeds `limit_pct`, trip the breaker (persist to `meta`)
    and return True — caller must stop emitting new signals and alert.
    Once tripped, stays tripped on every subsequent call (checked first,
    before re-evaluating the window) even if a later window looks fine —
    only resume_emission() clears it, so a couple of lucky results right
    after a real blowup can't silently resume live emission on their own.
    """
    if is_emission_paused(sb):
        return True
    try:
        res = (sb.table("ai_learning_ledger")
               .select("outcome, kelly_pct, odds, created_at")
               .order("created_at", desc=True)
               .limit(window_n)
               .execute())
        rows = res.data or []
    except Exception as e:
        log.error("check_circuit_breaker: %s — not pausing on a read error alone", e)
        return False

    dd = rolling_drawdown(rows)
    if dd > limit_pct:
        log.critical("CIRCUIT BREAKER TRIPPED — rolling drawdown %.1f%% > %.1f%% over last %d signals",
                     dd * 100, limit_pct * 100, window_n)
        try:
            sb.table("meta").upsert({
                "key":        _PAUSE_KEY,
                "value":      "true",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            log.error("check_circuit_breaker: failed to persist pause flag: %s", e)
        return True
    return False


def resume_emission(sb) -> None:
    """Manually clear the circuit breaker — the ONLY way emission resumes
    once tripped (see module docstring). Not called anywhere in this
    codebase by design; run it by hand (console/one-off script) once
    you've reviewed why the breaker tripped."""
    sb.table("meta").upsert({
        "key":        _PAUSE_KEY,
        "value":      "false",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def is_sport_emission_paused(sb, sport: str) -> bool:
    """True if `sport`'s own circuit breaker has tripped and not yet been
    manually cleared (see resume_sport_emission())."""
    try:
        res = sb.table("meta").select("value").eq("key", _SPORT_PAUSE_KEY_PREFIX + sport).limit(1).execute()
        return bool(res.data) and res.data[0].get("value") == "true"
    except Exception as e:
        log.warning("is_sport_emission_paused[%s]: %s — assuming not paused", sport, e)
        return False


def check_circuit_breaker_by_sport(sb, sport: str, window_n: int = DRAWDOWN_WINDOW_N,
                                   limit_pct: float = DRAWDOWN_LIMIT_PCT) -> bool:
    """
    Same rolling-drawdown check as check_circuit_breaker(), scoped to one
    sport's own ai_learning_ledger rows — catches a sport in a genuine bad
    streak that the global (all-sports) check would dilute away, and lets
    that sport specifically stop recommending while every other sport keeps
    running normally. Same "sticky until manually cleared" behavior as the
    global breaker: checked first, before re-evaluating the window, so a
    couple of lucky results right after a real blowup in this sport can't
    silently resume it on their own.
    """
    if is_sport_emission_paused(sb, sport):
        return True
    try:
        res = (sb.table("ai_learning_ledger")
               .select("outcome, kelly_pct, odds, created_at")
               .eq("sport", sport)
               .order("created_at", desc=True)
               .limit(window_n)
               .execute())
        rows = res.data or []
    except Exception as e:
        log.error("check_circuit_breaker_by_sport[%s]: %s — not pausing on a read error alone", sport, e)
        return False

    dd = rolling_drawdown(rows)
    if dd > limit_pct:
        log.critical("SPORT CIRCUIT BREAKER TRIPPED [%s] — rolling drawdown %.1f%% > %.1f%% "
                     "over last %d signals", sport, dd * 100, limit_pct * 100, window_n)
        try:
            sb.table("meta").upsert({
                "key":        _SPORT_PAUSE_KEY_PREFIX + sport,
                "value":      "true",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            log.error("check_circuit_breaker_by_sport[%s]: failed to persist pause flag: %s", sport, e)
        return True
    return False


def resume_sport_emission(sb, sport: str) -> None:
    """Manually clear `sport`'s circuit breaker — the ONLY way its emission
    resumes once tripped. Not called anywhere in this codebase by design;
    run it by hand once you've reviewed why that sport's breaker tripped."""
    sb.table("meta").upsert({
        "key":        _SPORT_PAUSE_KEY_PREFIX + sport,
        "value":      "false",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    log.warning("Circuit breaker manually cleared — emission resumed")
