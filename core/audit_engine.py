"""
core/audit_engine.py — PAIM v8.5 — Settlement + CLV Audit
Runs every 6h via GitHub Actions (run_audit.py entry point).

Pipeline for each signal whose match kicked off > SETTLEMENT_GRACE_H hours ago
(or, for legacy rows with no match_time, scanned > AUDIT_LAG_H hours ago) with
status='active':
  1. Settlement pass — web search (Groq/Tavily) fetches real match score → status='settled'
     outcome = WIN | LOSS | PUSH | UNKNOWN
  2. CLV pass — ONLY once the match is > EXPIRE_AFTER_H old. Before that, a
     failed settlement leaves the signal 'active' for the next run to retry;
     'closed'/'expired' are terminal and must never be spent on a signal we
     merely failed to look up (rate limit, exhausted search budget).
     → status='closed' (real closing line) or 'expired' (proxy original price)
  3. CLV = (xbet_odd / closing_line − 1) × 100
  4. Learning Layer updates sport-specific MIN_EDGE thresholds.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from core.db import (get_db, log_to_ledger,
                     update_signal_fields, MissingCredentialsError)
from core.ai_search import (ai_available, ai_dead as gemini_quota_dead,
                            search_credits_left, search_exhausted)
# Task 3 — real closing-line capture (run_closing_line.py). The window
# constants and the optional-column set are shared with core/closing_line.py,
# which captures the same fields for free off the OddsAPI scan feed; they
# live in core/constants.py so the two paths can never drift apart.
from core.constants import (CLOSING_LINE_BUDGET, CLOSING_LINE_COLS,
                            CLOSING_LINE_REFRESH_MIN, CLOSING_LINE_TIGHTEN_MIN,
                            CLOSING_LINE_WINDOW_MIN, CLOSING_SRC_EXCHANGE,
                            CLOSING_SRC_ODDSAPI, CLOSING_SRC_ORACLE)
from core.learning_layer import compute_and_save as _learn
from core.oracle import get_pinnacle_price
from core.run_contract import terminer as _terminer_run, verdict_de_fin
from core.paim_engine import resolve_selection_side
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
# Hours after match_time before a failed settlement is allowed to become the
# TERMINAL 'expired'/'closed' status. Below this, a signal we could not settle
# stays 'active' and the next 6h run retries it. MUST stay comfortably under
# run_engine.py's past-match purge window (48h) or a retried signal gets
# deleted before it ever reaches the ledger.
EXPIRE_AFTER_H      = int(os.environ.get("EXPIRE_AFTER_H", 36))
# Tavily credits Pass 2 (CLV) must leave untouched for Pass 1 (settlement).
# Pass 2 spends up to 3 per signal, Pass 1 at most 1 — this floor keeps roughly
# a dozen real settlements reachable no matter how much CLV ran before them.
CLV_CREDIT_RESERVE  = int(os.environ.get("CLV_CREDIT_RESERVE", 12))
ORACLE_BUDGET = 30     # Max oracle (web search) calls per audit run
SETTLE_BUDGET = 25     # Max settlement (web search) calls per audit run

_AUDIT_COLS = {"closing_line", "clv_pct", "closed_at"}
_CLOSING_LINE_COLS = CLOSING_LINE_COLS

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
    """Écrit le résultat d'audit par un UPDATE en place. Rend True si la ligne
    est à jour.

    C'était un DELETE + INSERT jusqu'au 2026-08-27 (B1), sur la foi d'un « RLS
    blocks UPDATE outright » qui n'est plus vrai : la policy
    `service_role_update` existe depuis migrate_v9_3, vérifiée en base. Le
    détour exposait la ligne à une perte définitive entre les deux ordres et
    lui donnait un `id` neuf à chaque passage.

    On ne patche QUE `payload` : fusionner `sig` entier renvoyait à la base
    des colonnes qu'on n'avait pas lues pour les modifier — dont celles de
    closing line, qu'un autre job peut avoir posées entre-temps."""
    return update_signal_fields(sb, sig["id"], payload, optional_cols=_AUDIT_COLS)


def _past_expiry(sig: dict, now: datetime) -> bool:
    """True once `sig`'s match is old enough that a terminal 'closed'/'expired'
    is fair — i.e. retrying settlement on a later run is pointless.

    A signal with no usable match_time can't be dated, so it falls back to
    scan age; if neither parses we return True rather than keeping an
    undatable row active forever (run_engine.py's purge would drop it anyway).
    """
    for field, span in (("match_time", EXPIRE_AFTER_H),
                        ("scanned_at", EXPIRE_AFTER_H + SETTLEMENT_GRACE_H)):
        raw = sig.get(field) or ""
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts) >= timedelta(hours=span)
    return True


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

    # BOTH passes are AI-search-backed (Groq/Tavily, core/ai_search.py). If the
    # search layer cannot run at all, Pass 1 can never settle and Pass 2 can
    # never fetch a closing line — proceeding would stamp this signal 'expired'
    # (terminal, never retried) with a garbage proxy CLV. Leave it 'active' so
    # a later run settles it for real.
    if not ai_available() or gemini_quota_dead():
        return "skipped"

    # ── Pass 1 : Settlement via real score ───────────────────────────
    if settle_calls[0] > 0:
        settle_calls[0] -= 1
        if settle_signal(sb, sig, now_iso):
            return "settled"
        log.info("No score yet for %s — falling back to CLV audit", match)

    # A failed Pass 1 is NOT proof the score is unavailable — it is far more
    # often proof we could not look. Confirmed live on run 29854918520
    # (2026-07-21 17:54 UTC): 16 signals settled fine, then compound-mini
    # started returning "rate limit minute" and the Tavily run budget (25
    # credits, shared with Pass 2's 3-bookmaker oracle lookups) ran out —
    # "compound-mini KO et aucune donnée Tavily — abandon". The 6 signals
    # caught in that window were stamped 'expired' for matches that had been
    # over for up to 15h and whose scores were on every public scoreboard
    # (Brewers 8-3 Mets, Rangers 3-10 White Sox, Storm 102-105 Lynx...).
    # 'expired' is terminal — fetch_pending only ever selects status='active'
    # — so a transient rate limit permanently cost those signals their real
    # WIN/LOSS, and the learning layer scored them as unknown.
    #
    # So: only let a failure become terminal once the match is old enough that
    # retrying is genuinely pointless. Before EXPIRE_AFTER_H, stay 'active' and
    # let the next 6h run try again with a fresh per-run search budget.
    if gemini_quota_dead() or search_exhausted() or not _past_expiry(sig, now):
        log.info("RETRY LATER | %s — settlement failed, signal left active", match)
        return "skipped"

    # ── Pass 2 : CLV — fetch current Pinnacle closing line ────────────
    # Pass 2 costs up to 3 Tavily lookups per signal (Pinnacle, Betfair, Circa
    # — see core/oracle.py) out of the SAME per-run budget Pass 1 needs, and it
    # runs interleaved: signal N's CLV spends credits signal N+1's settlement
    # will want. That is how the 2026-07-21 run starved itself. Settlement wins
    # every time — a real WIN/LOSS is permanent, CLV is a metric — so once the
    # budget is down to the reserve, stop buying CLV entirely and leave what is
    # left to scores.
    closing_price: float | None = None
    if search_credits_left() <= CLV_CREDIT_RESERVE:
        log.info("CLV SKIP | %s — %d crédits restants réservés au settlement",
                 match, search_credits_left())
        return "skipped"

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
    CLOSING_LINE_WINDOW_MIN minutes — the genuine "closing line" window, as
    opposed to this module's own Pass 2 CLV (fetched hours-to-days after
    kickoff during the 6h audit, at best a proxy) or core/settlement.py's
    entry-edge re-derivation (see sql/migrate_v9_5_learning_integrity.sql).

    Deliberately NOT filtered on `closing_pinnacle_price IS NULL`: a signal
    already carrying a price is still a candidate, so a later run — one
    closer to kickoff — can refine it. That refresh is what makes capture
    independent of GitHub's unreliable cron (see CLOSING_LINE_WINDOW_MIN).
    Staleness is enforced per-signal in capture_closing_lines(), which is
    also where the oracle budget is spent.

    `match_time >= now` is load-bearing: once the match has started the
    oracle would return a live/in-play price, which is not a closing line.
    """
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=CLOSING_LINE_WINDOW_MIN)
    try:
        res = (sb.table("signals")
               .select("*")
               .eq("status", "active")
               .gte("match_time", now.isoformat())
               .lte("match_time", window_end.isoformat())
               .limit(100)
               .execute())
        return res.data or []
    except Exception as e:
        log.error("fetch_closing_line_candidates: %s", e)
        return []


def _needs_refresh(sig: dict, now: datetime) -> bool:
    """Should this signal be (re-)priced on this run?

    Always yes if it has no price at all — that first capture anywhere in the
    240-min window is what guarantees we end up with *something* even when the
    scheduler drops several ticks in a row.

    After that, only inside CLOSING_LINE_TIGHTEN_MIN of kickoff, and at most
    every CLOSING_LINE_REFRESH_MIN. Re-pricing a match still three hours out
    buys no accuracy — the line has not converged yet — and this job shares
    its web-search quota with the audit.

    A price already captured by core/closing_line.py — off the OddsAPI scan
    feed, or off the exchange prices every scan already downloads —
    outranks anything this oracle can produce — it is the real Pinnacle
    number for the exact side, not a web-search estimate of the favourite —
    so it is only overwritten once it has gone properly stale
    (CLOSING_LINE_TIGHTEN_MIN rather than CLOSING_LINE_REFRESH_MIN). That
    also stops this job spending search budget on signals the free path has
    already measured.
    """
    if not sig.get("closing_pinnacle_price"):
        return True
    stamp = sig.get("closing_captured_at")
    if not stamp:
        return True   # pre-migration row, or capture predating the stamp
    try:
        taken = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    # Un prix EXACT — payload OddsAPI, ou prix d'exchange réel — surclasse
    # tout ce que cet oracle peut produire (le vrai nombre pour le côté exact,
    # contre une estimation web du favori) : il n'est écrasé qu'une fois
    # proprement périmé, et le budget de recherche va ailleurs.
    exact = sig.get("closing_source") in (CLOSING_SRC_ODDSAPI, CLOSING_SRC_EXCHANGE)
    hold_min = CLOSING_LINE_TIGHTEN_MIN if exact else CLOSING_LINE_REFRESH_MIN
    if (now - taken) < timedelta(minutes=hold_min):
        return False
    try:
        kickoff = datetime.fromisoformat(str(sig.get("match_time") or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True   # unparseable kickoff — refresh rather than freeze a stale price
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return (kickoff - now) <= timedelta(minutes=CLOSING_LINE_TIGHTEN_MIN)


def _lead_time_label(match_time, now: datetime) -> str:
    """"37min" / "2h14" — how far ahead of kickoff a price was taken. Logged
    on every capture so a sparse run is obvious in the job output rather than
    hidden behind a column called `closing_pinnacle_price`."""
    if not match_time:
        return "?"
    try:
        kickoff = datetime.fromisoformat(str(match_time).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "?"
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    mins = int((kickoff - now).total_seconds() // 60)
    if mins < 0:
        return "?"
    return f"{mins}min" if mins < 60 else f"{mins // 60}h{mins % 60:02d}"


def count_missed_closing_lines(sb) -> int:
    """Active signals whose kickoff has already passed with no closing price
    captured — i.e. signals nothing will ever be able to price again.

    Exists because the original bug was silent: the job found zero candidates
    and exited green for a month while capturing nothing. A non-zero count
    here is the symptom that the schedule is too sparse for the window.

    Counts every market, not just h2h: since core/closing_line.py prices
    totals/spreads off the scan feed, an unpriced totals signal is now a real
    miss rather than a structural impossibility."""
    now = datetime.now(timezone.utc)
    try:
        res = (sb.table("signals")
               .select("id")
               .eq("status", "active")
               .is_("closing_pinnacle_price", "null")
               .lt("match_time", now.isoformat())
               .limit(500)
               .execute())
        return len(res.data or [])
    except Exception as e:
        log.debug("count_missed_closing_lines: %s", e)
        return 0


def capture_closing_lines(sb, budget: int = CLOSING_LINE_BUDGET) -> int:
    """
    For each signal within CLOSING_LINE_WINDOW_MIN of kickoff, re-fetch the
    current Pinnacle consensus price and store it as closing_pinnacle_price
    — a real closing-line measurement, distinct from pinnacle_price
    (captured at scan time, possibly hours or days earlier). clv_pct_real
    is the bettor's true CLV — (xbet_odd / closing_price_of_the_same_side
    − 1) × 100, positive = the price we got beat the close — derived
    immediately, without waiting for the match outcome (that remains
    core/settlement.py's real WIN/LOSS job). Runs via run_closing_line.py,
    not as part of the 6h audit.

    Each run RE-prices every signal still ahead of kickoff (subject to
    CLOSING_LINE_REFRESH_MIN), so the stored price converges on the close as
    kickoff approaches and the last run before kickoff wins. closing_captured_at
    records when the surviving price was taken, so a consumer can tell a
    genuine T-10min close from a T-3h price instead of trusting the column
    name — see sql/migrate_v9_11_closing_captured_at.sql.

    THIS IS NOW THE FALLBACK PATH. core/closing_line.py captures the same
    columns for free off the OddsAPI scan feed — exact Pinnacle prices, every
    market, no web search — and marks them closing_source='oddsapi'. This
    oracle exists for what that path cannot reach: signals harvested outside
    OddsAPI (MMA, eSports, alt sports — see core/harvester.py), and OddsAPI
    signals whose event stopped being scanned before kickoff (quota
    exhausted, sport key dropped). _needs_refresh() will not overwrite a
    fresh scan-feed price with a web-search estimate.

    KNOWN LIMIT — get_pinnacle_price() only ever quotes the closing ML/DNB
    FAVORITE (with its team name), so a real CLV can only be computed here
    for h2h signals whose selection still IS that closing favorite (our h2h
    signals are always emitted on the scan-time favorite — see
    run_engine._process_h2h). Everything else is never guessed:
      - totals/spreads candidates are skipped before any oracle budget is
        spent (the oracle has no price for those markets at all — they are
        core/closing_line.py's job now);
      - an h2h signal whose favorite flipped by kickoff, or whose side
        can't be resolved (selection and oracle team are each resolved
        against the match's own two team names — exact equality first, see
        core.paim_engine.resolve_selection_side), stores the raw fetched
        price with clv_pct_real=None.
    core/learning_layer.py's _clv_stats() excludes None rows, so unresolved
    sides simply don't participate in threshold decisions.
    """
    now = datetime.now(timezone.utc)
    candidates = fetch_closing_line_candidates(sb)
    remaining = [budget]
    captured = 0
    for sig in candidates:
        if (sig.get("market_key") or "") != "h2h":
            continue
        if not _needs_refresh(sig, now):
            continue
        if remaining[0] <= 0:
            log.warning("CLOSING LINE — oracle budget exhausted, %d signal(s) left for next run",
                        len(candidates) - captured)
            break
        remaining[0] -= 1
        try:
            price, team = get_pinnacle_price(sig["match"], sport=sig.get("sport", "soccer"),
                                             league=sig.get("league", ""))
        except Exception as e:
            log.warning("capture_closing_lines oracle [%s]: %s", sig["match"], e)
            continue
        if not price or price <= 1.01:
            continue

        # Side check — resolve BOTH the signal's selection and the oracle's
        # returned team against this match's own two team names (exact
        # equality first — see resolve_selection_side; a bare fuzzy match
        # between selection and team would conflate shared-token clubs like
        # "America MG"/"America RN"). Only an identical resolved side means
        # the fetched price is the price of the thing we actually bet.
        home, _, away = (sig.get("match") or "").partition(" vs ")
        sel_side  = resolve_selection_side(sig.get("selection_name") or "", home, away)
        team_side = resolve_selection_side(team or "", home, away)
        same_side = sel_side is not None and team_side is not None and sel_side == team_side
        xbet_odd = sig.get("xbet_odd") or 0.0
        clv_real = round((xbet_odd / price - 1) * 100, 2) if same_side and xbet_odd > 1.01 else None

        # UPDATE en place — cette écriture se répète tous les
        # CLOSING_LINE_REFRESH_MIN sur un signal encore vivant. C'est ce
        # chemin-là qui avait montré le défaut du DELETE+INSERT, supprimé
        # partout depuis (B1, 2026-08-27).
        ok = update_signal_fields(sb, sig["id"], {
            "closing_pinnacle_price": float(price),
            "clv_pct_real":           clv_real,
            "closing_captured_at":    now.isoformat(),
            "closing_source":         CLOSING_SRC_ORACLE,
        }, optional_cols=_CLOSING_LINE_COLS)
        if ok:
            captured += 1
            log.info("CLOSING LINE | %s | bet %.3f -> close %.3f | CLV_real %s | T-%s",
                     sig["match"], xbet_odd, price,
                     f"{clv_real:+.2f}%" if clv_real is not None else "n/a (side unresolved)",
                     _lead_time_label(sig.get("match_time"), now))
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

    # A green run that captured nothing used to be indistinguishable from a
    # green run that had nothing to do — that is how this job stayed broken
    # for a month. Surface the signals it can no longer ever price.
    missed = count_missed_closing_lines(sb)
    if missed:
        log.warning("CLOSING LINE — %d active h2h signal(s) passed kickoff with no "
                    "closing price: the schedule is firing too rarely for a %d-min "
                    "window. Check the closing_line.yml cadence and the post-scan "
                    "pass in scan.yml.",
                    missed, CLOSING_LINE_WINDOW_MIN)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")



# ── Un audit stérile doit CRIER (2026-08-26) ──────────────────────────
# « Audit done: 0 settled | 0 closed | 0 expired | 52 skipped » sortait en
# log.info, le run GitHub était VERT, et rien n'alertait. La régression du
# 24 août (taux de résolution 65 % → 11 %) a donc vécu deux jours sans être
# vue. C'est la même panne que le job de closing line resté vert un mois en ne
# capturant rien : un travail nul est indiscernable d'un travail sans objet
# tant que personne ne dit la différence.
SETTLEMENT_STARVED_KEY = "settlement_starved_at"


def _alerte_telegram(texte: str) -> None:
    """Envoi best-effort. Une alerte qui plante ne doit pas tuer l'audit."""
    jeton = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (jeton and chat):
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{jeton}/sendMessage", timeout=10,
                      json={"chat_id": chat, "text": texte, "parse_mode": "Markdown"})
    except Exception as e:                                       # noqa: BLE001
        log.warning("alerte Telegram: %s", e)


def _signaler_audit_sterile(sb, counts: dict, total: int) -> None:
    """Alerte + marqueur en base quand un audit n'a RIEN pu régler.

    Le marqueur `meta.settlement_starved_at` sert à run_engine._purge_old_signals :
    tant qu'il est frais, la purge laisse aux signaux non réglés plus que les
    48 h habituelles. Sans cela, une panne de recherche ne retarde pas
    l'apprentissage — elle détruit l'échantillon, puisqu'un signal purgé part
    en `expired` et que learning_layer._clv_stats exclut ces lignes.
    """
    if counts.get("settled") or not total:
        return
    msg = (f"⚠️ *Audit stérile* — 0 signal réglé sur {total} éligibles "
           f"({counts.get('skipped', 0)} sautés).\n"
           "Les deux chemins de score sont probablement indisponibles : "
           "api-sports (clé/budget) et la recherche web (quota Groq/Tavily). "
           "Chercher « compound-mini KO » et « HTTP 432 » dans le log du job.")
    log.error("AUDIT STÉRILE — 0 réglé sur %d éligibles ; les signaux restent "
              "actifs et la purge est repoussée", total)
    _alerte_telegram(msg)
    try:
        sb.table("meta").upsert(
            {"key": SETTLEMENT_STARVED_KEY,
             "value": datetime.now(timezone.utc).isoformat(),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:                                       # noqa: BLE001
        log.warning("marqueur %s: %s", SETTLEMENT_STARVED_KEY, e)


def _effacer_marqueur_sterile(sb) -> None:
    """Un audit qui règle quelque chose lève la famine."""
    try:
        sb.table("meta").delete().eq("key", SETTLEMENT_STARVED_KEY).execute()
    except Exception as e:                                       # noqa: BLE001
        log.debug("effacement %s: %s", SETTLEMENT_STARVED_KEY, e)

def _relancer_expires(sb) -> None:
    """Reprend un lot de lignes expirées (core/relance_expires.py).

    Import TARDIF et échec avalé : ce lot améliore un état déjà écrit, il ne
    doit jamais pouvoir faire échouer un audit qui a par ailleurs réglé des
    signaux — ni entrer dans le contrat de fin, qui juge le settlement frais.
    """
    try:
        from core.relance_expires import relancer
        relancer(sb)
    except Exception as e:                                       # noqa: BLE001
        log.warning("Relance des expirés: %s", e)


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

    # Fail loudly BEFORE touching any signal. Without GROQ_API_KEY the entire
    # audit is a no-op that would otherwise mass-expire every pending signal
    # (see audit_one's guard) — better to exit non-zero so the GitHub Actions
    # run goes red and the missing secret is visible, than to burn the batch.
    if not ai_available():
        log.critical("GROQ_API_KEY absente — recherche de score impossible. "
                     "Audit interrompu, les signaux restent 'active'. "
                     "Ajoute le secret GROQ_API_KEY (repo → Settings → "
                     "Secrets and variables → Actions) pour réactiver le settlement.")
        raise SystemExit(1)

    pending = fetch_pending(sb)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("PAIM AUDIT v8.5 — %d signals pending", len(pending))

    if not pending:
        log.info("Nothing to audit.")
        # Rien de frais à régler ne veut pas dire rien à faire : les lignes
        # EXPIRÉES attendent toujours leur score. Un audit à vide est même le
        # meilleur moment pour les reprendre — tout le budget est disponible.
        _relancer_expires(sb)
        log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return

    now = datetime.now(timezone.utc)
    oracle_budget = [ORACLE_BUDGET]
    settle_budget = [SETTLE_BUDGET]
    counts = {"settled": 0, "closed": 0, "expired": 0, "skipped": 0}

    for i, sig in enumerate(pending):
        if gemini_quota_dead():
            counts["skipped"] += len(pending) - i
            log.warning("Quota IA (Groq) épuisé — %d signals left active "
                        "for the next audit run (post quota reset ~07:00 UTC)",
                        len(pending) - i)
            break
        status = audit_one(sb, sig, oracle_budget, settle_budget, now)
        counts[status] = counts.get(status, 0) + 1

    log.info("Audit done: %d settled | %d closed | %d expired | %d skipped",
             counts["settled"], counts["closed"], counts["expired"], counts["skipped"])
    if counts["settled"]:
        _effacer_marqueur_sterile(sb)
    else:
        _signaler_audit_sterile(sb, counts, len(pending))
    log.info("Oracle: %d/%d | Settlement: %d/%d calls used",
             ORACLE_BUDGET - oracle_budget[0], ORACLE_BUDGET,
             SETTLE_BUDGET - settle_budget[0], SETTLE_BUDGET)

    # RELANCE DES EXPIRÉS — ICI et pas plus haut. `expired` était terminal :
    # une ligne perdue faute d'avoir PU chercher (quota mort, historique
    # api-sports fermé) ne repassait jamais devant un moteur de recherche.
    # Ce lot la reprend, mais APRÈS le settlement frais : la réserve IA du
    # settlement est tenue en négatif depuis le 2026-08-02 et un signal du
    # jour vaut plus qu'un match d'il y a deux semaines.
    _relancer_expires(sb)

    log.info("--- Learning Layer ---")
    try:
        _learn(sb)
    except Exception as e:
        log.error("Learning layer: %s", e)

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # CONTRAT DE FIN (B5). La troisième condition du contrat — « settlement
    # éligible ET 0 réglé » — vit ICI et non dans `run_engine.run()`, qui ne
    # règle rien : le settlement appartient à ce module, appelé par
    # `run_audit.py`. Le demander au moteur de scan aurait produit un garde
    # incapable d'observer ce qu'il prétend garder.
    #
    # L'alerte Telegram de `_signaler_audit_sterile` existait déjà depuis le
    # 2026-08-26 ; c'est le CODE DE SORTIE qui manquait. « 0 settled | 52
    # skipped » est resté vert deux jours entiers, du 24 au 26 août, pendant
    # que les deux quotas de recherche étaient à terre.
    #
    # Le verdict est posé APRÈS la couche d'apprentissage, à dessein : un
    # audit stérile doit quand même avoir tenté d'apprendre de ce qu'il a, et
    # sortir avant le priverait de ce tour-là.
    _terminer_run(verdict_de_fin(settlement_eligible=len(pending),
                                 settlement_regles=counts["settled"]),
                  contexte="audit")
