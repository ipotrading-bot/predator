"""
core/settlement.py — PAIM — Match Settlement Engine
Trouve le score réel d'un match → WIN/LOSS/PUSH → `status='settled'`.

CHAÎNE 100 % DÉTERMINISTE (2026-09-02), portée par `core/score_sources.fetch_score` :
MLB statsapi (officiel, sans clé), ESPN (ouvert, sans clé), puis TheSportsDB
(voie par équipe) — deux noms appariés strictement, candidat unique, match
terminé, sinon None. api-sports en était le premier étage jusqu'au
2026-09-03 : deux comptes gratuits suspendus (INCIDENTS.md « api-sports, deux
comptes suspendus »), décision opérateur « vivre sans » — la source est
retirée du dépôt.

IL N'Y A PLUS DE RECHERCHE WEB. Jusqu'au 2026-09-02 le dernier recours était
Groq compound-mini + Tavily : deux quotas gratuits qui ont lâché ENSEMBLE deux
fois en une semaine (26/08 et 01/09 — « AUDIT STÉRILE — 0 réglé »), pour une
information qui existe en champ dans des API gratuites. Décision opérateur du
2026-09-02 : Groq et Tavily sont supprimés du pipeline. Un score introuvable
laisse la ligne repasser au prochain audit — l'attente n'est pas définitive,
un WIN/LOSS faux l'est.
"""
import logging
import re

from core.score_sources import fetch_score
from core.db import log_to_ledger, update_signal_fields
from core.paim_engine import resolve_selection_side

log = logging.getLogger("PREDATOR.settlement")

_SETTLEMENT_OPTIONAL = frozenset({"outcome", "settled_at"})


def fetch_match_result(match_name: str, sport: str, match_date: str = "") -> dict | None:
    """
    Score final d'un match terminé — chaîne déterministe de core/score_sources
    (MLB statsapi, ESPN, TheSportsDB).
    Returns {"home_score": int, "away_score": int, "completed": True} or None.
    None veut dire « pas trouvé aujourd'hui », jamais un état terminal.
    """
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
