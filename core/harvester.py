"""
core/harvester.py — Tier 2 : sources soft gratuites, line shopping
Soft source : odds-api.io + titan007 ; consensus Kalshi/Polymarket (mesure seule)
Sharp source: Pinnacle via api-sports, exchange (Matchbook/Smarkets)
All timestamps : UTC/GMT.

2026-09-03 : le LineFeed 1xbet/Melbet/22bet est RETIRÉ (décision opérateur :
« si une source est inutilisable, il faut la dégager »). Bloqué par IP depuis
les runners GitHub depuis août (HTTP 203 / 404 sur chaque URL, mesuré encore
le 2026-09-03 19:44), il coûtait pourtant à chaque scan 9 requêtes × 2-5 s de
sommeil PAR SPORT — ~36 s de budget moteur pour rien, quatre fois par scan.
Les books soft 1xbet/Bet365 arrivent par odds-api.io (authentifié, non
filtré par IP) ; la clé `odds_1xbet` garde son nom historique.

2026-09-02 : la recherche web (Groq compound-mini + Tavily) est SUPPRIMÉE du
harvest — décision opérateur, avec le retrait de Groq/Tavily de tout le
pipeline. Trois fonctions sont parties avec elle : `_fetch_from_gemini`
(demandait à un LLM d'« estimer des cotes 1XBet réalistes » sur des matchs
qu'il pouvait halluciner), `fetch_pinnacle_prices` (recherche groupée d'une
« cote Pinnacle » par LLM — le chemin dominant du prix sharp GÉNÉRÉ, celui
que l'en-tête de l'ancien core/oracle.py désignait comme hors de portée de
MAX_ORACLE) et `fetch_estimated_prices` (cotes de mémoire d'entraînement).
Toutes trois fabriquaient des prix qu'aucun book n'a affichés : une cote
sous-estimée fabrique un edge, et ces signaux-là sont précisément ceux que
le moteur émet (cf. A6). Un match sans prix sharp RÉEL est écarté, point.
"""
import hashlib
import logging

from core.odds_api_io import SPORTS as _OAI_SPORTS, fetch_sport as _oai_fetch_sport
from core.titan007 import SPORT_ID as _T7_SPORT_ID, fetch_matches as _t7_fetch
from core.paim_engine import strict_team_match

# ── UTC sub-logger (inherits handler from PREDATOR root) ─────────────
log = logging.getLogger("PREDATOR.harvester")

SPORT_IDS = {1: "soccer", 3: "tennis", 4: "basketball", 5: "mma"}

def _odd(val):
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _stable_id(prefix: str, home: str, away: str, when: str = "") -> str:
    """ID déterministe : même match réel → même id à chaque scan.

    L'ancien schéma `f"gemini_{sport}_{i}"` utilisait l'index de position dans
    le JSON renvoyé par l'IA. L'ordre variant d'un scan à l'autre, le même
    match changeait d'id à chaque tick — run_engine._save() dédoublonne par
    (match_id, market_key), son delete ne trouvait donc jamais la version
    précédente et les copies s'empilaient en base. Pire : deux matchs
    différents pouvaient se partager un id d'un scan à l'autre, et le delete
    frappait alors le signal d'un autre match.
    """
    raw = f"{prefix}|{home.lower().strip()}|{away.lower().strip()}|{(when or '')[:10]}"
    return f"ai_{prefix}_" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def _meilleur_par_issue(cible: dict, autre: dict) -> bool:
    """Meilleur prix par issue entre deux observations DU MÊME book (ou de
    provenance inconnue) — deux sources qui lisent le même book donnent deux
    lectures d'un seul prix, pas deux books. True si `cible` a bougé."""
    improved = False
    for key in ("1", "X", "2"):
        new_odd = float(autre.get(key, 0.0) or 0.0)
        if new_odd > float(cible.get(key, 0.0) or 0.0):
            cible[key] = new_odd
            improved = True
    return improved


def _fusionner_h2h(existing: dict, cand: dict) -> bool:
    """Fusion du 1X2 d'un même match vu par deux sources.

    Depuis le 2026-09-08 chaque source pose `h2h_par_book` (bloc par book
    d'exécution, core/execution_books). Les blocs se fusionnent BOOK PAR
    BOOK — jamais une issue de Bet365 dans un bloc 1xbet : un DNB
    synthétique engage deux jambes chez le même book. `odds_1xbet` reste le
    bloc du book de référence ; sans attribution des deux côtés (sources
    d'avant, tests), on retombe sur le meilleur prix par issue."""
    from core.execution_books import ordre
    ex_pb, ca_pb = existing.get("h2h_par_book"), cand.get("h2h_par_book")
    if not ex_pb or not ca_pb:
        return _meilleur_par_issue(existing["odds_1xbet"], cand["odds_1xbet"])
    improved = False
    for book, bloc in ca_pb.items():
        if book in ex_pb:
            improved = _meilleur_par_issue(ex_pb[book], bloc) or improved
        else:
            ex_pb[book] = dict(bloc)
            improved = True
    existing["odds_1xbet"] = ex_pb[min(ex_pb, key=ordre)]
    return improved


def _fuzzy_match_event(candidate: dict, pool: list[dict]) -> dict | None:
    """Find `candidate`'s counterpart in `pool` by team-name fuzzy match
    (core.paim_engine.strict_team_match) — used to line up the same
    real-world match across different soft books before comparing prices."""
    for other in pool:
        if strict_team_match(candidate["home"], other["home"]) and \
           strict_team_match(candidate["away"], other["away"]):
            return other
    return None


# sport_id harvester -> nom de sport odds-api.io (core/odds_api_io.py).
_ODDS_API_IO_BY_ID = {cfg[1]: name for name, cfg in _OAI_SPORTS.items()}


def _fetch_from_odds_api_io(sport_id: int) -> list:
    """odds-api.io — accès AUTHENTIFIÉ aux books soft (1xbet & co.), donc
    non filtré par IP — c'est par ici que 1xbet/Bet365 arrivent depuis le
    retrait du LineFeed direct. Fournit h2h + spreads + totals, et un prix
    sharp si un exchange fait partie des books sélectionnés."""
    sport = _ODDS_API_IO_BY_ID.get(sport_id)
    if not sport:
        return []
    return _oai_fetch_sport(sport)


def _fetch_multi_book(sport_id: int) -> list:
    """
    Task 6 — line shopping: fetch every configured soft source (odds-api.io,
    titan007) for this sport and, for each real-world
    match found on 2+ of them, keep
    the BEST (highest) price per outcome across all of them — not just
    whichever book happened to respond first. `_soft_source` on the
    returned match records which book contributed each surviving price
    (or a "+" joined list when outcomes came from different books), for
    display/debugging attribution.

    Falls back gracefully: if only one book responds, its prices are used
    as-is (identical behavior to the old single-book fetch).
    """
    per_book: dict[str, list] = {}
    # (LineFeed 1xbet/Melbet/22bet et api-sports retirés le 2026-09-03 —
    # voir l'en-tête ; api-sports : deux comptes gratuits suspendus.)
    oai_matches = _fetch_from_odds_api_io(sport_id)
    if oai_matches:
        per_book["odds_api_io"] = oai_matches

    # Titan007 : foot uniquement, mais c'est la seule source gratuite qui
    # couvre les ligues sud-américaines et secondaires où l'exchange est
    # riche — et elle apporte le prix sharp ET le prix soft d'un coup.
    if sport_id == _T7_SPORT_ID:
        t7_matches = _t7_fetch()
        if t7_matches:
            per_book["titan007"] = t7_matches

    # (odds.500.com — « mission 3 », mode ombre puis promue — est RETIRÉE le
    # 2026-09-03 avec 7M et le dictionnaire d'alias : mur anti-bot EdgeOne
    # servi en HTTP 200 depuis le 1er septembre, décision opérateur.)

    if not per_book:
        return []

    books_in_order = list(per_book.keys())
    merged: list[dict] = list(per_book[books_in_order[0]])
    for m in merged:
        m["_soft_source"] = books_in_order[0]

    for book in books_in_order[1:]:
        for cand in per_book[book]:
            existing = _fuzzy_match_event(cand, merged)
            if existing is None:
                cand["_soft_source"] = book
                merged.append(cand)
                continue
            # Same real-world match found on another book — keep the
            # better price per outcome (line shopping), track provenance.
            sources = set(existing["_soft_source"].split("+"))
            # Champs que le line shopping ne doit PAS perdre : un prix sharp
            # ou une heure de coup d'envoi n'existent que chez certaines
            # sources, et l'ancienne fusion ne recopiait que les prix soft —
            # un match trouvé d'abord sur le LineFeed perdait donc le prix
            # Pinnacle qu'une autre source apportait, c'est-à-dire le signal.
            for extra in ("odds_pinnacle", "commence_time", "league",
                          "spreads_1xbet", "totals_1xbet",
                          "spreads_pinnacle", "totals_pinnacle"):
                if cand.get(extra) and not existing.get(extra):
                    existing[extra] = cand[extra]
            improved = _fusionner_h2h(existing, cand)
            if improved:
                sources.add(book)
                existing["_soft_source"] = "+".join(sorted(sources))

    # Marchés de prédiction (Kalshi/Polymarket) — rôle CONSENSUS, jamais sharp.
    # Ils ne modifient ni un prix ni un signal : ils confrontent le slate à un
    # avis qui ne recopie aucun bookmaker, et crient quand un « edge » ressemble
    # à un prix périmé. Best-effort, jamais bloquant.
    _measure_consensus(sport_id, merged)

    return merged


def _measure_consensus(sport_id: int, merged: list) -> None:
    """Confronte le slate aux marchés de prédiction — mesure seule.

    Import PARESSEUX et jamais bloquant : un harvester qui ne sert pas le
    football n'a aucune raison de payer cet import.
    """
    try:
        from core.free_sources import measure_slate_consensus
        measure_slate_consensus(sport_id, merged)
    except Exception as e:
        log.warning("consensus: %s — ignoré ce cycle", e)


def fetch_matches():
    """Fetch matches for all configured sports, line-shopping the best
    price per outcome across every soft source (Task 6). Returns combined list.

    Un sport dont aucun book ne rend rien reste VIDE : le repli « demande à
    un LLM d'inventer le slate et ses cotes » a été supprimé le 2026-09-02
    avec Groq/Tavily. Rien trouvé veut dire rien, pas « demande à un modèle »."""
    all_matches = []
    # L'union des sports que les sources soft savent servir — dérivée de
    # leurs propres tables, jamais recopiée ici. Le foot (titan007) est
    # dans les deux premières de toute façon.
    ids = set(_ODDS_API_IO_BY_ID) | {_T7_SPORT_ID}
    for sport_id in sorted(ids):
        all_matches.extend(_fetch_multi_book(sport_id))
    return all_matches

# Betfair (Tier 1.5 historique) RETIRÉ le 2026-09-09 — décision opérateur,
# règle 13 : 0 marché chargé depuis le 2026-07-09, compte banni, nouveau
# compte sans clé d'application. Matchbook (core/matchbook.py) et Smarkets
# (core/smarkets.py) tiennent l'exchange. Voir INCIDENTS.md « Betfair retiré ».
