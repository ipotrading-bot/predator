"""
core/free_sources.py — marchés de prédiction : un avis INDÉPENDANT de tout bookmaker.

CE QUI RESTE DE LA « MISSION 3 » (2026-09-03)
---------------------------------------------
Ce module coordonnait les sources gratuites asiatiques : odds.500.com (cotes,
noms chinois), 7M (noms anglais pour apprendre les alias) et le dictionnaire
`core/team_aliases.py`. odds500 est morte derrière un mur anti-bot Tencent
EdgeOne servi en HTTP 200 depuis le 2026-09-01 (INCIDENTS.md « 500.com sert
son mur anti-bot en HTTP 200 ») ; l'opérateur a tranché le 2026-09-03 :
retirée, avec 7M et le dictionnaire qui n'existaient que pour traduire ses
noms. Une source qu'on ne peut plus lire n'a pas de mode ombre, elle a une
date de sortie.

Il reste la mesure de CONSENSUS contre Kalshi/Polymarket, branchée dans
`core/harvester._measure_consensus` : elle ne dépend d'aucune de ces sources.

Kalshi et Polymarket ne recopient personne : c'est de l'argent réel posé par
des gens qui n'ont pas lu la même ligne. C'est ce qui en fait un arbitre
utile quand deux chemins « Pinnacle » divergent — l'un des deux est périmé,
et deux books ne le diront jamais.

RÔLE `consensus`, JAMAIS SHARP : ils MESURENT, ils n'émettent aucun signal
et ne modifient aucun prix. Le module existait depuis le 2026-08-22 et
n'était importé nulle part hors de ses tests (capacité morte en silence,
motif « listes qui divergent », INCIDENTS.md) — c'est ce branchement-ci.

COUVERTURE HONNÊTE : Kalshi et Polymarket ne cotent que 4 compétitions
(EPL, UCL, NFL, NBA). Le slate de Predator étant surtout composé de ligues
mineures, le recoupement est structurellement FAIBLE. Mesuré vivant le
2026-08-26 : kalshi epl=18 ucl=4, polymarket epl=52 ucl=30.
"""
import logging
import os

from core.source_adapter import (Fixture, cross_check, load_scorecard,
                                 pair_fixtures, record_observation,
                                 save_scorecard)

log = logging.getLogger("PREDATOR.free_sources")

SOCCER_SPORT_ID = 1

# Coupe-circuit d'urgence : `FREE_SOURCES=0` débranche la mesure sans
# redéploiement.
ENABLED = os.environ.get("FREE_SOURCES", "1") == "1"


def _as_fixture(m: dict, source: str) -> Fixture | None:
    """Match au format harvester → Fixture, pour l'appariement structurel."""
    odds = m.get("odds_pinnacle") or m.get("odds_1xbet") or {}
    vec = [odds.get("1", 0.0), odds.get("X", 0.0), odds.get("2", 0.0)]
    if not (vec[0] and vec[2]):
        vec = []
    elif not vec[1]:
        vec = [vec[0], vec[2]]           # moneyline à deux issues
    return Fixture(
        source=source, match_id=str(m.get("id") or ""),
        kickoff=m.get("commence_time") or "", league=m.get("league") or "",
        home=m.get("home") or "", away=m.get("away") or "",
        odds=vec, team_ids=tuple(m.get("_alias_team_ids") or ()),
    )


_CONSENSUS_LEAGUES = {SOCCER_SPORT_ID: ("epl", "ucl")}


def consensus_fixtures(sport_id: int) -> list:
    """Fixtures des marchés de prédiction pour ce sport. Best-effort."""
    from core import prediction_markets
    out = []
    for league in _CONSENSUS_LEAGUES.get(sport_id, ()):
        try:
            out.extend(prediction_markets.fetch_consensus(league))
        except Exception as e:
            log.warning("free_sources: marchés de prédiction (%s) — %s", league, e)
    return out


def measure_slate_consensus(sport_id: int, matches: list) -> int:
    """Confronte le slate aux marchés de prédiction. Rend le nb de paires.

    Ne modifie RIEN : aucun prix, aucun signal. Elle alimente le scorecard
    `prediction_markets` et crie quand un prix du slate s'écarte d'un marché
    indépendant — c'est-à-dire quand un « edge » est probablement un prix
    périmé plutôt qu'une occasion.
    """
    if not ENABLED or not matches or sport_id not in _CONSENSUS_LEAGUES:
        return 0
    card = load_scorecard("prediction_markets")
    try:
        right = consensus_fixtures(sport_id)
    except Exception as e:
        log.warning("free_sources: consensus indisponible (%s)", e)
        save_scorecard(record_observation(card, errors=1))
        return 0
    if not right:
        log.info("free_sources: marchés de prédiction — 0 marché coté")
        return 0

    left = [f for f in (_as_fixture(m, "slate") for m in matches) if f]
    pairs = pair_fixtures(left, right)
    if not pairs:
        log.info("free_sources: marchés de prédiction — %d marchés cotés, "
                 "0 appariés au slate (ils ne couvrent qu'EPL/UCL/NFL/NBA)",
                 len(right))
        return 0

    suspects = 0
    for a, b, _ev in pairs:
        ok, worst, _detail = cross_check({"slate": a.odds, "consensus": b.odds})
        card = record_observation(card, divergence_pts_value=worst if worst >= 0 else None,
                                  matched=1)
        if not ok:
            suspects += 1
            log.warning("free_sources: CONSENSUS DIVERGENT sur %s — %.2f pts "
                        "d'écart avec un marché indépendant (prix périmé ?)",
                        a.match_id, worst)
    save_scorecard(card)
    log.info("free_sources: marchés de prédiction — %d/%d apparié(s), %d divergent(s)",
             len(pairs), len(right), suspects)
    return len(pairs)
