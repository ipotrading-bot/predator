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

from core.score_sources import fetch_score, SPORTS_SCORE_DRAPEAU
from core.db import log_to_ledger, update_signal_fields
from core.paim_engine import resolve_selection_side, nom_avec_etage, ligne_en_quart

log = logging.getLogger("PREDATOR.settlement")

_SETTLEMENT_OPTIONAL = frozenset({"outcome", "settled_at"})


def fetch_match_result(match_name: str, sport: str, match_date: str = "",
                       tsdb_ok: bool = True) -> dict | None:
    """
    Score final d'un match terminé — chaîne déterministe de core/score_sources
    (MLB statsapi, ESPN, TheSportsDB).
    Returns {"home_score": int, "away_score": int, "completed": True} or None.
    None veut dire « pas trouvé aujourd'hui », jamais un état terminal.

    `tsdb_ok=False` coupe le repli TheSportsDB, dont le budget journalier est
    étroit — voir core.score_sources.fetch_score.
    """
    return fetch_score(match_name, sport, match_date, tsdb_ok=tsdb_ok)


def _paire_comptable(sport: str, home_score: int, away_score: int,
                     decompte: tuple | None) -> tuple[int, int] | None:
    """Le couple de scores COMPTABLE pour un total ou un handicap.

    Pour un sport dont le score est un DRAPEAU de vainqueur — tennis, MMA :
    ESPN ne publie pas de score chiffré, `_espn_candidat` rend 1-0 — il faut
    le décompte réel (les JEUX d'un match de tennis, `decompte` de
    core.score_sources.result_from_espn). Sans lui : None, donc UNKNOWN.

    POURQUOI (mesuré le 2026-09-17) : 1 + 0 = 1, donc tout « Moins de » sortait
    GAGNANT et tout « Plus de » PERDANT, quelle que soit la ligne et quel que
    soit le match. Les trois totaux tennis du ledger portaient déjà cette
    signature (Under 34.5 WIN, Under 39.5 WIN, Over 22.5 LOSS) ; recalculés
    sur les jeux réels d'ESPN ils tombaient juste par coïncidence — les lignes
    étaient hautes. Une ligne basse aurait été enregistrée gagnante à tort.

    Le h2h, lui, garde le drapeau : en tennis le vainqueur du match peut avoir
    MOINS de jeux que son adversaire (6-0 6-7 6-7), donc on ne déduit jamais
    un vainqueur d'un décompte, ni un décompte d'un vainqueur."""
    if (sport or "").lower() in SPORTS_SCORE_DRAPEAU:
        if not decompte or len(decompte) != 2:
            return None
        try:
            return (int(decompte[0]), int(decompte[1]))
        except (TypeError, ValueError):
            return None
    return (home_score, away_score)


def determine_outcome(sport: str, market_key: str, selection_name: str,
                      home: str, away: str,
                      home_score: int, away_score: int,
                      decompte: tuple | None = None) -> str:
    """Returns 'WIN', 'LOSS', 'PUSH', or 'UNKNOWN'.

    `decompte` : le compte réel des deux camps quand le score n'en est pas un
    (tennis, MMA — voir `_paire_comptable`). Son absence rend UNKNOWN sur un
    total ou un handicap de ces sports : c'est VOULU, un appelant qui ne peut
    pas compter ne doit pas pouvoir inventer une issue."""
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
        paire = _paire_comptable(sport, home_score, away_score, decompte)
        if paire is None:
            return "UNKNOWN"
        total = paire[0] + paire[1]
        try:
            line = float(re.search(r'[\d.]+', sel).group())
        except Exception:
            return "UNKNOWN"
        # Ligne en quart : demi-issue possible, jamais un WIN/LOSS plein
        # (2026-09-15, voir paim_engine.ligne_en_quart).
        if ligne_en_quart(line):
            return "UNKNOWN"
        if total == line:
            return "PUSH"
        return "WIN" if ("over" in sel and total > line) or ("under" in sel and total < line) else "LOSS"

    if "spreads" in market_key:
        try:
            point = float(re.search(r'[-+]?[\d.]+', sel).group())
        except Exception:
            return "UNKNOWN"
        if ligne_en_quart(point):
            return "UNKNOWN"
        paire = _paire_comptable(sport, home_score, away_score, decompte)
        if paire is None:
            return "UNKNOWN"
        hs, as_ = paire
        adjusted = (hs if "spreads_home" in market_key else as_) + point
        opp = as_ if "spreads_home" in market_key else hs
        if adjusted == opp:
            return "PUSH"
        return "WIN" if adjusted > opp else "LOSS"

    return "UNKNOWN"


def settle_signal(sb, sig: dict, now_iso: str, tsdb_ok: bool = True) -> bool:
    """
    Try to settle one signal using real match score.
    Returns True if settled, False if score not found.

    `tsdb_ok=False` : le repli TheSportsDB n'est pas tenté (budget journalier
    étroit, et une source qui n'a pas le score après 12 h ne l'aura pas).
    """
    match   = sig["match"]
    sport   = sig.get("sport", "soccer")

    # Use match_time date for Gemini search accuracy (not scanned_at)
    match_date = (sig.get("match_time") or sig.get("scanned_at") or "")[:10]

    # Le nom cherché porte l'étage que la LIGUE connaît et que la source a
    # tu : « Borussia Dortmund vs Villarreal CF » en Youth League se cherche
    # en « … U19 vs … U19 », sinon le score des seniors du même soir règle
    # le pari des jeunes (mesuré le 2026-09-08). Idempotent sur un nom déjà
    # qualifié, neutre sur une ligue senior.
    cherche = nom_avec_etage(match, sig.get("league") or "")
    result = fetch_match_result(cherche, sport, match_date, tsdb_ok=tsdb_ok)
    if not result or not result.get("completed"):
        return False

    hs  = result["home_score"]
    as_ = result["away_score"]
    # Décompte réel (jeux du tennis) quand la source en a un : seul lui règle
    # un total — voir _paire_comptable.
    decompte = result.get("decompte")
    home = match.split(" vs ")[0].strip() if " vs " in match else ""
    away = match.split(" vs ")[1].strip() if " vs " in match else ""
    outcome = determine_outcome(
        sport, sig.get("market_key", "h2h"),
        sig.get("selection_name", ""),
        home, away, hs, as_, decompte,
    )

    orig_pin = sig.get("pinnacle_price") or 0.0
    # NOT real CLV: xbet_odd/pinnacle_price are the exact same two values
    # already used to compute edge_pct at scan time (see paim_engine.compute_alpha),
    # so this is a re-derivation de l'edge d'entrée, pas une comparaison à la
    # clôture — il est déjà stocké honnêtement en `initial_edge` dans le
    # ledger (core/db.py:log_to_ledger). Gardé UNIQUEMENT pour la ligne de
    # log ci-dessous : depuis le 2026-09-09 il n'est plus ÉCRIT nulle part.
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
    # `clv_pct` N'EST PLUS ÉCRIT ICI (2026-09-09). Il valait `entry_edge_pct`,
    # qui est positif par construction — MIN_EDGE ne laisse jamais passer un
    # edge négatif. Résultat mesuré ce jour-là : 413 des 414 valeurs de
    # `signals.clv_pct` étaient >= 0, et le « hit rate CLV » du dashboard
    # affichait 99,8 % en croyant mesurer la clôture. Une colonne de CLV qui
    # ne peut pas être négative ne mesure rien. Elle appartient désormais au
    # SEUL pipeline qui observe un prix postérieur : core/audit_engine.py.
    # Sans capture de clôture, elle reste NULLE — un trou visible vaut mieux
    # qu'un chiffre faux. Voir INCIDENTS.md « Le CLV du dashboard ».
    patch = {
        "status":    "settled",
        "closed_at": now_iso,
        "outcome":   outcome,
    }
    if not update_signal_fields(sb, sig["id"], patch,
                                optional_cols=_SETTLEMENT_OPTIONAL):
        return False
    log.info("SETTLED  | %s %d-%d | outcome=%s | entry edge %+.2f%%", match, hs, as_, outcome, entry_edge_pct)

    # Feed ai_learning_ledger with the real settled outcome — this is what
    # core/learning_layer.py must key off of. `clv=None` : ce règlement n'a
    # observé AUCUN prix de clôture, donc il n'a pas de CLV à déclarer.
    log_to_ledger(sb, sig, None, outcome)

    return True
