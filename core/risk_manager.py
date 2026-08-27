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

     DESIGN NOTE — sizing basis (resolved 2026-07-11, operator decision:
     dashboard is canonical — see get_current_exposure()'s docstring):
       - kelly_pct (summed here) is each INDIVIDUAL signal's solo Kelly
         stake, as if that signal were bet alone — computed once at scan
         time (run_engine.py's _emit()) and persisted on the row. It's
         also what templates/index.html's dashboard shows and lets the
         operator size a bet against directly (per-signal, editable
         bankroll).
       - Telegram's system recommendation (core.tax_engine.suggest_system(),
         invoked from run_engine._suggest_systems_by_window()) used to
         compute its combined stake independently — a numerically-optimal
         Kelly figure on the combo's own probability/odds, unrelated to
         the sum of its legs' kelly_pct. suggest_system() now caps that
         stake at the sum of its own legs' kelly_pct (see its docstring),
         so Telegram can never recommend more than the dashboard already
         implies for the same underlying signals.
     Net effect: this cap's kelly_pct basis is now a genuine upper bound
     on real recommended capital on BOTH channels for any window's
     signals, not just a conservative proxy for one of them. It still
     over-counts in the case where a window's legs never combined into
     any tax-viable system at all (kelly_pct is summed here regardless,
     even though Telegram recommended nothing for them) — that remains
     the safe direction for a cap.

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

from core.constants import net_b

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

    Sizing-base note (see the module docstring's DESIGN NOTE for the full
    explanation): kelly_pct is each signal's SOLO stake, the same number
    templates/index.html's dashboard shows as a directly-actionable
    per-signal recommendation — now the canonical basis. Telegram's own
    stake is a single combined figure per time-window accumulator from
    core.tax_engine.suggest_system(), which caps that figure at the sum
    of its own legs' kelly_pct, so this function's aggregate solo-Kelly
    sum is a genuine upper bound on real recommended capital across both
    channels, not just a proxy for one of them.
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

    La courbe d'équité est NETTE DE TAXE depuis le 2026-08-27
    (`core.constants.net_b`, A2). Elle créditait auparavant le gain BRUT : un
    disjoncteur qui surestime chaque gain et compte chaque perte en entier
    sous-estime le drawdown réel, et se déclenche donc trop tard —
    exactement quand il devrait être le plus utile. Les pertes, elles, ne
    sont pas taxées : la retenue ne frappe que le gain net d'un pari gagnant.

    ⚠️ UNE COTE MANQUANTE N'EST PLUS REMPLACÉE PAR 2.0 (B4, 2026-08-27). Ce
    défaut inventait un gain : à cote réelle 1,20, créditer 2.00 multipliait le
    profit par cinq et faisait remonter la courbe d'un pari qui n'avait presque
    rien rapporté. Un disjoncteur nourri de gains fictifs ne se déclenche
    jamais.
    Le traitement est ASYMÉTRIQUE, et c'est voulu :
      · un WIN sans cote exploitable est ÉCARTÉ — on ne peut pas valoriser son
        gain, et l'écarter fait paraître le drawdown PIRE, donc déclenche plus
        tôt : c'est le sens sûr pour un organe de sécurité ;
      · une LOSS sans cote est CONSERVÉE — elle vaut −mise, la cote n'y change
        rien. L'écarter retirerait des pertes de l'échantillon et ferait
        paraître le portefeuille plus sain qu'il n'est, exactement l'erreur
        qu'on corrige.
    Mesuré le 2026-08-27 : 0 ligne sans cote sur 315. C'est un filet.
    """
    decisive = [r for r in ledger_rows
                if r.get("outcome") in ("WIN", "LOSS") and r.get("kelly_pct")]
    valorisables, ecartes = [], 0
    for r in decisive:
        if r["outcome"] == "LOSS":
            valorisables.append(r)
            continue
        try:
            cote = float(r.get("odds") or 0)
        except (TypeError, ValueError):
            cote = 0.0
        if cote > 1.01:
            valorisables.append(r)
        else:
            ecartes += 1
    if ecartes:
        log.warning("rolling_drawdown: %d gain(s) sans cote exploitable écarté(s) "
                    "— jamais remplacés par une cote inventée", ecartes)
    decisive = valorisables
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
            equity += stake * net_b(float(r["odds"]))
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
        # FILTRE CÔTÉ SQL (B4, 2026-08-27). La requête tirait les `window_n`
        # dernières lignes TOUS STATUTS confondus, puis filtrait en Python sur
        # WIN/LOSS. Or le ledger est majoritairement fait d'`expired` : mesuré
        # ce jour-là, les 20 dernières lignes ne contenaient qu'UNE SEULE
        # ligne décisive. `rolling_drawdown` rendant 0.0 sous deux lignes, le
        # disjoncteur ne pouvait PAS se déclencher — il était inerte, en vert,
        # depuis que les expirations dominent. Avec le filtre : 20 lignes sur
        # 20 exploitables, drawdown mesuré 1,7 %.
        res = (sb.table("ai_learning_ledger")
               .select("outcome, kelly_pct, odds, created_at")
               .in_("outcome", ["WIN", "LOSS"])
               .order("created_at", desc=True)
               .limit(window_n)
               .execute())
        rows = res.data or []
    except Exception as e:
        log.error("check_circuit_breaker: %s — not pausing on a read error alone", e)
        return False
    if len(rows) < window_n:
        log.info("check_circuit_breaker: %d résultats décisifs seulement sur %d "
                 "demandés — la fenêtre est plus courte que prévu", len(rows), window_n)

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
        # Même filtre CÔTÉ SQL que le disjoncteur global — voir sa docstring
        # pour la mesure. Un sport dont les dernières lignes sont surtout des
        # expirations aurait une fenêtre encore plus courte que la fenêtre
        # globale, donc un disjoncteur encore plus sûrement inerte.
        res = (sb.table("ai_learning_ledger")
               .select("outcome, kelly_pct, odds, created_at")
               .eq("sport", sport)
               .in_("outcome", ["WIN", "LOSS"])
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
