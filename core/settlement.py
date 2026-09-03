"""
core/settlement.py — PAIM v8.5 — Match Settlement Engine
Trouve le score réel d'un match → WIN/LOSS/PUSH → `status='settled'`.

CHAÎNE 100 % DÉTERMINISTE, DANS CET ORDRE (2026-09-02) :

1. `core/api_sports.fetch_results` — le score est un CHAMP de la réponse
   `/fixtures?date=`, déterministe et gratuit, UNE requête par journée quel que
   soit le nombre de matchs. Les résultats d'une journée sont mémorisés le
   temps du run : régler 52 signaux du même jour coûte 1 requête, pas 52.
2. `core/score_sources.fetch_score` — MLB statsapi (officiel, sans clé) puis
   TheSportsDB (voie par équipe), mêmes gardes.

IL N'Y A PLUS DE RECHERCHE WEB. Jusqu'au 2026-09-02 le dernier recours était
Groq compound-mini + Tavily : deux quotas gratuits qui ont lâché ENSEMBLE deux
fois en une semaine (26/08 et 01/09 — « AUDIT STÉRILE — 0 réglé »), pour une
information qui existe en champ dans des API gratuites. Décision opérateur du
2026-09-02 : Groq et Tavily sont supprimés du pipeline. Un score introuvable
laisse la ligne repasser au prochain audit — l'attente n'est pas définitive,
un WIN/LOSS faux l'est.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from core.api_sports import fetch_results
from core.score_sources import fetch_score
from core.paim_engine import strict_team_match
from core.db import log_to_ledger, update_signal_fields
from core.paim_engine import resolve_selection_side

log = logging.getLogger("PREDATOR.settlement")

_SETTLEMENT_OPTIONAL = frozenset({"outcome", "settled_at"})

# Résultats api-sports déjà téléchargés pendant CE run : {(sport, jour): [...]}.
# Un audit règle des dizaines de matchs de la même journée ; sans ce cache,
# chaque signal coûterait une requête et le budget de 100/jour partirait en
# une passe.
_CACHE_RESULTATS: dict[tuple, list] = {}


def reset_cache() -> None:
    """Vide le cache de résultats (tests, et runs longs)."""
    _CACHE_RESULTATS.clear()


def _resultats_du_jour(sport: str, jour: str) -> list:
    cle = (sport, jour)
    if cle not in _CACHE_RESULTATS:
        _CACHE_RESULTATS[cle] = fetch_results(jour, sport)
    return _CACHE_RESULTATS[cle]


# Fenêtre du PLAN GRATUIT api-sports autour d'aujourd'hui, en jours. Mesuré le
# 2026-09-03 (audit 33774472425) : « Free plans do not have access to this
# date, try from 2026-09-02 to 2026-09-04 » — soit J-1 … J+1. Chaque appel
# hors fenêtre brûlait un lookup du budget journalier pour un refus certain
# (15/25 consommés pour 0 réglé). Au-delà, on passe DIRECTEMENT aux sources
# ouvertes (core/score_sources). Un plan payant relève la valeur par l'env.
API_SPORTS_FREE_WINDOW_DAYS = int(os.environ.get("API_SPORTS_FREE_WINDOW_DAYS", "1"))


def _hors_fenetre_api_sports(match_date: str, now: datetime | None = None) -> bool:
    """True si `match_date` est plus vieux que J-API_SPORTS_FREE_WINDOW_DAYS.
    Une date illisible n'est pas jugée hors fenêtre (on laisse la voie
    normale décider)."""
    try:
        d = datetime.fromisoformat(match_date[:10]).date()
    except (TypeError, ValueError):
        return False
    aujourdhui = (now or datetime.now(timezone.utc)).date()
    return (aujourdhui - d).days > API_SPORTS_FREE_WINDOW_DAYS


def result_from_api_sports(match_name: str, sport: str, match_date: str) -> dict | None:
    """Score final depuis le calendrier api-sports, sans aucune IA.

    Apparie sur les DEUX noms d'équipe avec `strict_team_match` — le même
    rapprochement que partout ailleurs dans ce dépôt — et n'accepte qu'un
    candidat UNIQUE : deux prétendants signifient qu'on ne sait pas lequel est
    le bon, et régler le mauvais match écrirait un WIN/LOSS faux et
    définitif dans le ledger. Le refus est le comportement correct.

    Le match peut avoir été joué la veille en UTC (coup d'envoi tardif) : on
    regarde `match_date` ET le lendemain, qui est déjà en cache si un autre
    signal l'a demandé.
    """
    if not match_date or " vs " not in match_name:
        return None
    if _hors_fenetre_api_sports(match_date):
        log.info("SETTLE api-sports SAUTÉ | %s — %s hors de la fenêtre du plan gratuit "
                 "(J-%d) : sources ouvertes directement", match_name, match_date,
                 API_SPORTS_FREE_WINDOW_DAYS)
        return None
    home, away = (p.strip() for p in match_name.split(" vs ", 1))
    if len(home) < 3 or len(away) < 3:
        return None

    jours = [match_date]
    try:
        from datetime import datetime as _dt
        veille = _dt.fromisoformat(match_date) - timedelta(days=1)
        lendemain = _dt.fromisoformat(match_date) + timedelta(days=1)
        jours += [lendemain.strftime("%Y-%m-%d"), veille.strftime("%Y-%m-%d")]
    except ValueError:
        pass

    for jour in jours:
        candidats = [r for r in _resultats_du_jour(sport, jour)
                     if strict_team_match(home, r["home"]) and strict_team_match(away, r["away"])]
        if len(candidats) == 1:
            r = candidats[0]
            log.info("SETTLE api-sports | %s | %d-%d (0 appel IA)",
                     match_name, r["home_score"], r["away_score"])
            return {"home_score": r["home_score"], "away_score": r["away_score"],
                    "completed": True, "source": "api_sports"}
        if len(candidats) > 1:
            log.info("SETTLE SKIP | %s — %d matchs api-sports correspondent, on ne devine pas",
                     match_name, len(candidats))
            return None
    return None


def fetch_match_result(match_name: str, sport: str, match_date: str = "") -> dict | None:
    """
    Score final d'un match terminé — api-sports d'abord (dans la fenêtre de
    son plan), puis la chaîne déterministe de core/score_sources (MLB
    statsapi, ESPN, TheSportsDB).
    Returns {"home_score": int, "away_score": int, "completed": True} or None.
    None veut dire « pas trouvé aujourd'hui », jamais un état terminal.
    """
    exact = result_from_api_sports(match_name, sport, match_date)
    if exact:
        return exact
    return fetch_score(match_name, sport, match_date)


def determine_outcome(sport: str, market_key: str, selection_name: str,
                      home: str, away: str,
                      home_score: int, away_score: int) -> str:
    """Returns 'WIN', 'LOSS', 'PUSH', or 'UNKNOWN'."""
    sel = (selection_name or "").lower().strip()

    if market_key == "h2h" and sport == "soccer":
        # Resolve the side BEFORE the draw check would otherwise mask an
        # unresolvable selection — an ambiguous selection on a genuine draw
        # happens to be PUSH regardless of side, but a draw is not the only
        # score that reaches this branch.
        is_home = resolve_selection_side(selection_name, home, away)
        if is_home is None:
            return "UNKNOWN"
        if home_score == away_score:
            return "PUSH"
        won = (is_home and home_score > away_score) or (not is_home and away_score > home_score)
        return "WIN" if won else "LOSS"

    if market_key == "h2h":
        is_home = resolve_selection_side(selection_name, home, away)
        if is_home is None:
            return "UNKNOWN"
        won = (is_home and home_score > away_score) or (not is_home and away_score > home_score)
        return "WIN" if won else "LOSS"

    if "totals" in market_key:
        total = home_score + away_score
        try:
            line = float(re.search(r'[\d.]+', sel).group())
        except Exception:
            return "UNKNOWN"
        if total == line:
            return "PUSH"
        return "WIN" if ("over" in sel and total > line) or ("under" in sel and total < line) else "LOSS"

    if "spreads" in market_key:
        try:
            point = float(re.search(r'[-+]?[\d.]+', sel).group())
        except Exception:
            return "UNKNOWN"
        if "spreads_home" in market_key:
            adjusted = home_score + point
        else:
            adjusted = away_score + point
        opp = away_score if "spreads_home" in market_key else home_score
        if adjusted == opp:
            return "PUSH"
        return "WIN" if adjusted > opp else "LOSS"

    return "UNKNOWN"


def settle_signal(sb, sig: dict, now_iso: str) -> bool:
    """
    Try to settle one signal using real match score.
    Returns True if settled, False if score not found.
    """
    match   = sig["match"]
    sport   = sig.get("sport", "soccer")

    # Use match_time date for Gemini search accuracy (not scanned_at)
    match_date = (sig.get("match_time") or sig.get("scanned_at") or "")[:10]

    result = fetch_match_result(match, sport, match_date)
    if not result or not result.get("completed"):
        return False

    hs  = result["home_score"]
    as_ = result["away_score"]
    home = match.split(" vs ")[0].strip() if " vs " in match else ""
    away = match.split(" vs ")[1].strip() if " vs " in match else ""
    outcome = determine_outcome(
        sport, sig.get("market_key", "h2h"),
        sig.get("selection_name", ""),
        home, away, hs, as_,
    )

    orig_pin = sig.get("pinnacle_price") or 0.0
    # NOT real CLV: xbet_odd/pinnacle_price are the exact same two values
    # already used to compute edge_pct at scan time (see paim_engine.compute_alpha),
    # so this is a re-derivation of the entry edge, not a closing-line
    # comparison — it is already stored honestly as `initial_edge` in the
    # ledger (core/db.py:log_to_ledger). Kept here only to populate the
    # legacy `signals.clv_pct` display column until core/audit_engine.py's
    # real closing-line pipeline (Task 3) lands. Never treat this as CLV.
    entry_edge_pct = round((sig["xbet_odd"] / orig_pin - 1) * 100, 2) if orig_pin > 1.01 else 0.0

    # UPDATE en place. C'était un DELETE + INSERT jusqu'au 2026-08-27, justifié
    # par un « RLS blocks UPDATE outright » devenu faux — la policy
    # `service_role_update` existe depuis migrate_v9_3. Le détour perdait le
    # signal si le processus mourait entre les deux ordres, et lui donnait un
    # `id` NEUF à chaque règlement, ce qui laissait le `signal_id` déjà
    # recopié dans `ai_learning_ledger` pointer vers une ligne disparue.
    # On ne patche QUE les champs qui changent : réécrire `{**sig, **patch}`
    # renvoyait à la base des colonnes qu'on n'avait aucune raison de toucher,
    # et pouvait écraser une capture de closing line posée entre-temps.
    patch = {
        "status":    "settled",
        "clv_pct":   float(entry_edge_pct),
        "closed_at": now_iso,
        "outcome":   outcome,
    }
    if not update_signal_fields(sb, sig["id"], patch,
                                optional_cols=_SETTLEMENT_OPTIONAL):
        return False
    log.info("SETTLED  | %s %d-%d | outcome=%s | entry edge %+.2f%%", match, hs, as_, outcome, entry_edge_pct)

    # Feed ai_learning_ledger with the real settled outcome — this is what
    # core/learning_layer.py must key off of (never the clv_final/entry-edge
    # value below, which cannot vary with the actual match result).
    log_to_ledger(sb, sig, float(entry_edge_pct), outcome)

    return True
