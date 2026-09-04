"""
core/audit_engine.py — PAIM v8.5 — Settlement + CLV Audit
Runs every 6h via GitHub Actions (run_audit.py entry point).

Pipeline for each signal whose match kicked off > SETTLEMENT_GRACE_H hours ago
(or, for legacy rows with no match_time, scanned > AUDIT_LAG_H hours ago) with
status='active':
  1. Settlement pass — chaîne DÉTERMINISTE de scores (api-sports, MLB statsapi,
     TheSportsDB — core/settlement.py + core/score_sources.py, ZÉRO appel IA
     depuis le 2026-09-02) → status='settled', outcome = WIN | LOSS | PUSH | UNKNOWN
  2. CLV pass — ONLY once the match is > EXPIRE_AFTER_H old. Before that, a
     failed settlement leaves the signal 'active' for the next run to retry;
     'closed'/'expired' are terminal and must never be spent on a signal we
     merely failed to look up.
     → status='closed' (une closing line RÉELLE a été capturée par les scans —
     colonne closing_pinnacle_price, sources oddsapi/exchange) ou 'expired'
     (proxy prix d'origine). L'oracle web-search qui « estimait » une ligne de
     clôture a été SUPPRIMÉ le 2026-09-02 avec Groq/Tavily : une cote générée
     par un LLM n'est pas une observation (cf. l'ancien core/oracle.py).
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
# Task 3 — real closing-line capture (run_closing_line.py). The window
# constants and the optional-column set are shared with core/closing_line.py,
# which captures the same fields for free off the OddsAPI scan feed; they
# live in core/constants.py so the two paths can never drift apart.
from core.closing_line import capture_from_exchange
from core.constants import CLOSING_LINE_WINDOW_MIN
from core.learning_layer import compute_and_save as _learn
from core.run_contract import terminer as _terminer_run, verdict_de_fin
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
# Heures après le coup d'envoi au-delà desquelles le repli TheSportsDB (budget
# journalier de 150) n'est plus tenté. DOIT rester bien SOUS EXPIRE_AFTER_H :
# la ligne continue d'être réessayée via ESPN, seule la source à budget étroit
# décroche. Au-dessus d'EXPIRE_AFTER_H, ce réglage n'aurait aucun effet.
TSDB_RETRY_WINDOW_H = int(os.environ.get("TSDB_RETRY_WINDOW_H", 12))
SETTLE_BUDGET = 25     # Max settlement lookups per audit run (score APIs)

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


def _age_h(sig: dict, now: datetime) -> float | None:
    """Heures écoulées depuis le coup d'envoi, ou None si indatable."""
    raw = sig.get("match_time") or ""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def _tsdb_encore_utile(sig: dict, now: datetime) -> bool:
    """Le repli TheSportsDB vaut-il encore une requête pour ce signal ?

    TheSportsDB a un budget JOURNALIER de 150 requêtes, et il est le dernier
    étage de la chaîne : tout signal qu'ESPN ne couvre pas y tombe. Comme un
    échec laisse la ligne `active` jusqu'à EXPIRE_AFTER_H (36 h), une poignée
    de matchs que personne ne peut régler le consommait ~6 fois chacun —
    150/150 dès 10:38 le 2026-09-03, puis deux `AUDIT STÉRILE` qui bloquaient
    le règlement de matchs parfaitement réglables.

    Une source qui n'a pas publié le score 12 h après le coup d'envoi ne le
    publiera pas au 6e essai. Au-delà, ESPN (large, 22/200 mesuré) continue
    seul jusqu'à l'expiration : on ne perd donc AUCUNE voie de règlement,
    seulement des requêtes gaspillées.

    Un signal indatable garde son repli : c'est le cas rare, et le refuser
    supprimerait une voie sur une simple absence de `match_time`.
    """
    age = _age_h(sig, now)
    return age is None or age < TSDB_RETRY_WINDOW_H


def audit_one(sb, sig: dict, settle_calls: list, now: datetime) -> str:
    """
    Audit a single signal.
    Pass 1: settlement (real score) → 'settled'
    Pass 2: CLV only             → 'closed' or 'expired'
    Returns the new status string.
    """
    match  = sig["match"]
    now_iso = now.isoformat()

    # ── Pass 1 : Settlement via real score ───────────────────────────
    if settle_calls[0] > 0:
        settle_calls[0] -= 1
        if settle_signal(sb, sig, now_iso, tsdb_ok=_tsdb_encore_utile(sig, now)):
            return "settled"
        log.info("No score yet for %s — falling back to CLV audit", match)

    # A failed Pass 1 is NOT proof the score is unavailable. Historiquement
    # c'était « on n'a pas PU chercher » (quotas Groq/Tavily morts — run
    # 29854918520 : 6 signaux tamponnés 'expired' pour des scores publics) ;
    # depuis la chaîne déterministe c'est le plus souvent « la source n'a pas
    # ENCORE le score » (saisie en retard chez api-sports/TheSportsDB, statut
    # non terminé). Même remède : only let a failure become terminal once the
    # match is old enough that retrying is genuinely pointless. Before
    # EXPIRE_AFTER_H, stay 'active' and let the next 6h run try again.
    if not _past_expiry(sig, now):
        log.info("RETRY LATER | %s — settlement failed, signal left active", match)
        return "skipped"

    # ── Pass 2 : CLV — depuis la closing line DÉJÀ CAPTURÉE ──────────
    # Plus aucun appel ici (2026-09-02). Les scans capturent la vraie ligne de
    # clôture sur les prix qu'ils téléchargent déjà (core/closing_line.py :
    # payload OddsAPI à l'arrêt, exchange Matchbook chaque tick) — colonne
    # closing_pinnacle_price. L'ancien oracle web-search « estimait » une cote
    # par LLM : une génération plausible, pas une observation, précisément le
    # défaut qui a mis MAX_ORACLE_DEFAULT à 0 le 2026-08-27.
    closing_price = float(sig.get("closing_pinnacle_price") or 0.0)
    if closing_price > 1.01:
        deja = sig.get("clv_pct_real")
        clv = float(deja) if deja is not None else round(
            (sig["xbet_odd"] / closing_price - 1) * 100, 2)
        status = "closed"
        log.info("CLV %+.2f%% %s | %s (closing %s)", clv, "✓" if clv >= 0 else "✗",
                 match, sig.get("closing_source") or "?")
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


def _matches_from_signals(signals: list[dict]) -> list[dict]:
    """Pseudo-matchs pour `capture_from_exchange`, dérivés des signaux mêmes.

    Le chemin scan passe les matchs du slate ; ici, hors scan, les signaux
    portent déjà tout ce que l'appariement exige (les deux noms, le coup
    d'envoi, le match_id sous lequel les signaux se retrouvent). L'appariement
    flou vers l'exchange reste celui de `lookup_exchange` — candidat unique.
    """
    out = []
    for sig in signals:
        name = sig.get("match") or ""
        if " vs " not in name or not sig.get("match_id"):
            continue
        home, away = (p.strip() for p in name.split(" vs ", 1))
        out.append({"id": sig["match_id"], "match": name, "home": home,
                    "away": away, "sport": sig.get("sport") or "soccer",
                    "commence_time": sig.get("match_time") or ""})
    return out


def count_missed_closing_lines(sb) -> int:
    """Active signals whose kickoff has already passed with no closing price
    captured — i.e. signals nothing will ever be able to price again.

    Exists because the original bug was silent: the job found zero candidates
    and exited green for a month while capturing nothing.

    Counts every market, not just h2h. ⚠️ Ce compte est un STOCK, pas un
    flux : une ligne reste `active` après kickoff tant que le settlement ne
    l'a pas réglée, donc les mêmes signaux sont recomptés à chaque run — un
    chiffre stable qui revient n'est PAS « 4-5 pertes par scan ». Causes
    réelles mesurées le 2026-09-02 (cadence du cron vérifiée SAINE) :
    (a) signaux Tier 2 hors SPORT_KEYS — invisibles au payload OddsAPI, et
    la voie exchange est h2h-only, donc leurs totals/spreads n'ont AUCUNE
    voie de capture (limite structurelle, 0/14 sur 7 jours) ;
    (b) signaux émis à moins de ~20 min du kickoff, nés après la dernière
    passe de capture ;
    (c) famine de settlement qui fait stagner le stock."""
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


def capture_closing_lines(sb, budget: int | None = None) -> int:
    """
    Capture la closing line des signaux à moins de CLOSING_LINE_WINDOW_MIN du
    coup d'envoi, depuis les prix d'EXCHANGE (Matchbook gratuit et illimité,
    Betfair si la clé est posée) — plus aucun oracle web-search (2026-09-02).

    C'était un LLM à qui l'on demandait « la cote Pinnacle » : une génération
    plausible, pas une observation (le motif exact de MAX_ORACLE_DEFAULT = 0,
    2026-08-27), payée sur les quotas Groq/Tavily supprimés depuis. Le chemin
    exchange existait déjà dans chaque scan (`core/closing_line.py::
    capture_from_exchange`, closing_source='exchange') ; ce job le rejoue hors
    scan pour les créneaux où le scheduler n'a pas livré de tick. Prix réels,
    même côté, mêmes gardes (DNB exigé au football, refus sinon) — et
    `budget` n'a plus d'objet (gardé pour compatibilité d'appel).
    """
    del budget
    now = datetime.now(timezone.utc)
    candidates = [s for s in fetch_closing_line_candidates(sb)
                  if (s.get("market_key") or "") == "h2h"]
    if not candidates:
        return 0
    matches = _matches_from_signals(candidates)
    if not matches:
        return 0

    from core.matchbook import fetch_matchbook_prices
    sports = sorted({m["sport"] for m in matches})
    hours = max(1, -(-CLOSING_LINE_WINDOW_MIN // 60))
    prices = fetch_matchbook_prices(sports=sports, hours_ahead=hours) or {}
    if os.environ.get("BETFAIR_APP_KEY"):
        from core.harvester import fetch_betfair_prices
        # Betfair prioritaire quand il répond (prix ajustés de la commission),
        # même précédence que dans run_engine.
        bf = fetch_betfair_prices(sports=sports, hours_ahead=hours) or {}
        prices = {**prices, **bf}
    if not prices:
        log.info("CLOSING LINE — aucun prix d'exchange chargé (%d candidat(s))",
                 len(candidates))
        return 0
    return capture_from_exchange(sb, matches, prices, now=now)


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
        # Libellé corrigé le 2026-09-02 : l'ancien accusait la cadence du
        # cron (« firing too rarely ») et ne parlait que de h2h — les deux
        # étaient faux et orientaient le diagnostic vers la mauvaise piste.
        # Voir la docstring de count_missed_closing_lines pour les causes.
        log.warning("CLOSING LINE — %d signal(s) actifs (tous marchés) passés "
                    "kickoff sans clôture. C'est un STOCK recompté à chaque "
                    "run, pas un flux : causes probables — Tier 2 hors "
                    "payload (totals/spreads sans voie de capture), émission "
                    "née après la dernière passe de capture, ou settlement "
                    "en famine qui fait stagner ces lignes en `active`.",
                    missed)
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
           "Les chemins de score sont probablement indisponibles : api-sports "
           "(clé/budget), MLB statsapi, TheSportsDB. Chercher « score_sources » "
           "et « budget journalier atteint » dans le log du job.")
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
        faits = relancer(sb)
        # Le compte-rendu est ré-émis par le logger de CE module : celui de
        # `core.relance_expires` n'est pas configuré par run_audit.py, et sa
        # ligne de résumé n'apparaissait pas dans les logs Actions — une passe
        # qu'on ne voit pas travailler est une passe qu'on croit morte.
        log.info("RELANCE EXPIRÉS — %d signal(aux) et %d ligne(s) réglés | "
                 "%d sans score | %d marché indécidable",
                 faits.get("signaux", 0), faits.get("ledger", 0),
                 faits.get("sans_score", 0), faits.get("indecidable", 0))
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
    settle_budget = [SETTLE_BUDGET]
    counts = {"settled": 0, "closed": 0, "expired": 0, "skipped": 0}

    for sig in pending:
        status = audit_one(sb, sig, settle_budget, now)
        counts[status] = counts.get(status, 0) + 1

    log.info("Audit done: %d settled | %d closed | %d expired | %d skipped",
             counts["settled"], counts["closed"], counts["expired"], counts["skipped"])
    if counts["settled"]:
        _effacer_marqueur_sterile(sb)
    else:
        _signaler_audit_sterile(sb, counts, len(pending))
    log.info("Settlement: %d/%d lookups used",
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
