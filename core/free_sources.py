"""
core/free_sources.py — branchement des sources gratuites de la mission 3.

POURQUOI UNE COUCHE DE COORDINATION
------------------------------------
`core/odds500.py` rend des matchs dont les noms d'équipes sont EN CHINOIS et
qui portent `_needs_alias: True`. Les brancher tels quels dans le harvester
enverrait « 曼联 » dans `signals`, puis dans `ai_learning_ledger`, et le
settlement chercherait six heures plus tard le score d'un match qu'il ne sait
pas nommer. Entre l'adaptateur et le moteur il faut donc trois étapes, et
c'est ce module qui les tient :

  1. APPRENDRE — apparier le calendrier chinois (500.com) et le calendrier
     anglais (7M) par temps + ligue + structure, sans lire un seul nom, et en
     déduire les alias (core/team_aliases.py) ;
  2. RÉSOUDRE — remplacer les libellés chinois par les noms canoniques. Un
     match dont UNE des deux équipes ne se résout pas est ÉCARTÉ, jamais émis
     avec un nom brut ;
  3. MESURER puis DÉCIDER — comparer les prix aux sources de confiance,
     alimenter le scorecard, et ne rendre des matchs misables QUE si la source
     est sortie du mode ombre.

LE MODE OMBRE N'EST PAS UNE OPTION
-----------------------------------
Tant que `odds500` n'a pas 100 matchs appariés avec une divergence médiane
≤ 2 points face à une source de confiance, `fetch_odds500()` rend **[]** : ses
prix sont enregistrés et comparés, ils ne créent aucun signal. On ne coupe pas
la collecte — sinon on n'apprendrait jamais si la source est bonne (c'est la
logique de tests/test_shadow_mode.py, appliquée à une SOURCE plutôt qu'à un
segment). La promotion est écrite dans `meta`, jamais silencieuse.

Conséquence à connaître : **au premier déploiement, ce module ne produit
aucun signal, par construction.** Ce n'est pas une panne.
"""
import logging
import os

from core import odds500, sevenm, team_aliases
from core.source_adapter import (Fixture, cross_check, evaluate_promotion,
                                 load_scorecard, pair_fixtures,
                                 record_observation, save_scorecard)

log = logging.getLogger("PREDATOR.free_sources")

SOCCER_SPORT_ID = 1

# Nombre d'identifiants 7M interrogés par run pour nourrir le dictionnaire.
# Volontairement bas : un alias appris ne périme jamais, le dictionnaire se
# remplit sur plusieurs jours et n'a plus jamais besoin d'être rempli. À 2 s
# par requête (cadence annoncée), 30 identifiants = une minute par run.
SEVENM_PER_RUN = int(os.environ.get("FREE_SOURCES_SEVENM_PER_RUN", "30"))

# Coupe-circuit d'urgence : `FREE_SOURCES=0` débranche tout sans redéploiement.
ENABLED = os.environ.get("FREE_SOURCES", "1") == "1"


# Curseur de balayage du sitemap 7M, partagé entre les runs (table `meta`).
#
# POURQUOI IL EXISTE — constaté en live le 2026-08-22 : sans lui, chaque run
# réinterrogeait les 30 PREMIERS identifiants du sitemap, qui sont des coupes
# mineures sans recoupement avec le slate 500.com. Résultat : 0 alias appris,
# 25 matchs écartés, et un dictionnaire qui ne se serait jamais rempli. Le
# curseur fait balayer les ~936 identifiants en une trentaine de runs ; comme
# un alias appris ne périme jamais, ce balayage n'a lieu qu'une fois.
_CURSOR_KEY = "sevenm_sitemap_cursor"


def _cursor_get() -> int:
    try:
        from core.db import get_db
        row = (get_db(write=True).table("meta").select("value")
               .eq("key", _CURSOR_KEY).maybe_single().execute())
        return int((row.data or {}).get("value") or 0) if row and row.data else 0
    except Exception as e:
        log.debug("free_sources: curseur illisible (%s)", e)
        return 0


def _cursor_set(value: int) -> None:
    try:
        from datetime import datetime, timezone
        from core.db import get_db
        get_db(write=True).table("meta").upsert(
            {"key": _CURSOR_KEY, "value": str(int(value)),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:
        log.debug("free_sources: curseur non persisté (%s)", e)


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


def learn_aliases(cn_fixtures: list, budget: int | None = None) -> dict:
    """Enrichit le dictionnaire en appariant 500.com et 7M.

    N'interroge 7M que si des noms inconnus subsistent : quand le dictionnaire
    est complet, ce module ne coûte plus une seule requête. Best-effort — toute
    panne rend un compte vide, jamais une exception.
    """
    inconnus = [f for f in cn_fixtures
                if not team_aliases.canonical("odds500", f.home, f.team_ids[0] if f.team_ids else None, f.league)
                or not team_aliases.canonical("odds500", f.away, f.team_ids[1] if len(f.team_ids) > 1 else None, f.league)]
    if not inconnus:
        log.info("free_sources: dictionnaire complet pour ce slate — aucun appel 7M")
        return {"appris": 0, "confirmés": 0, "contredits": 0}

    log.info("free_sources: %d match(s) avec un nom inconnu — interrogation 7M",
             len(inconnus))
    cap = budget or SEVENM_PER_RUN
    cursor = _cursor_get()
    try:
        en_fixtures = sevenm.fetch_fixtures(max_matches=cap, offset=cursor)
    except Exception as e:
        log.warning("free_sources: 7M indisponible (%s) — dictionnaire inchangé", e)
        return {"appris": 0, "confirmés": 0, "contredits": 0}
    # Le curseur avance MÊME si ce run n'a rien appris : c'est justement quand
    # une tranche du sitemap ne recoupe pas le slate qu'il faut passer à la
    # suivante, pas la réinterroger indéfiniment.
    _cursor_set(cursor + cap)
    if not en_fixtures:
        return {"appris": 0, "confirmés": 0, "contredits": 0}

    pairs = pair_fixtures(inconnus, en_fixtures)
    return team_aliases.apply_pairing("odds500", pairs)


def resolve_names(matches: list) -> tuple:
    """Remplace les libellés chinois par les noms canoniques.

    Rend (matchs_résolus, nb_écartés). Un match dont UNE équipe ne se résout
    pas est écarté — jamais émis avec un libellé brut. C'est le seul
    comportement sûr : un nom non résolu casserait le settlement six heures
    plus tard, et un nom MAL résolu produirait un signal sur le mauvais match.
    """
    out, dropped = [], 0
    for m in matches:
        if not m.get("_needs_alias"):
            out.append(m)
            continue
        ids = m.get("_alias_team_ids") or (None, None)
        league = m.get("league") or ""
        home = team_aliases.canonical("odds500", m.get("home", ""),
                                      ids[0] if len(ids) > 0 else None, league)
        away = team_aliases.canonical("odds500", m.get("away", ""),
                                      ids[1] if len(ids) > 1 else None, league)
        if not home or not away:
            dropped += 1
            log.debug("free_sources: %s écarté — alias manquant (%s / %s)",
                      m.get("id"), m.get("home"), m.get("away"))
            continue
        m = dict(m)
        m["home"], m["away"] = home, away
        m["match"] = f"{home} vs {away}"
        m["_needs_alias"] = False
        m["_alias_resolved"] = True
        out.append(m)
    if dropped:
        log.info("free_sources: %d match(s) écarté(s) faute d'alias fiable", dropped)
    return out, dropped


def measure_against(matches: list, trusted: list, card: dict) -> dict:
    """Alimente le scorecard : divergence vs sources de confiance, fraîcheur.

    C'est la mesure qui décide de la promotion — et la seule chose que la
    source produise tant qu'elle est en ombre.
    """
    if not trusted:
        return card
    left = [f for f in (_as_fixture(m, "odds500") for m in matches) if f]
    right = [f for f in (_as_fixture(m, "trusted") for m in trusted) if f]
    pairs = pair_fixtures(left, right)

    by_id = {m["id"]: m for m in matches}
    for a, b, _ev in pairs:
        src = by_id.get(a.match_id)
        ok, worst, _detail = cross_check({"odds500": a.odds, "trusted": b.odds})
        card = record_observation(
            card, divergence_pts_value=worst if worst >= 0 else None,
            freshness_s=(src or {}).get("_freshness_s"), matched=1)
        if not ok:
            log.warning("free_sources: SUSPECT_DATA sur %s — %.2f pts d'écart "
                        "avec la source de confiance", a.match_id, worst)
    return card


def fetch_odds500(sport_id: int, trusted: list | None = None) -> list:
    """Point d'entrée unique du harvester.

    Rend les matchs 500.com **prêts à être fusionnés** (noms canoniques), ou
    [] si la source est en mode ombre, débranchée, ou muette. Ne lève jamais.
    """
    if not ENABLED:
        return []
    if sport_id != SOCCER_SPORT_ID:
        return []                       # 500.com = football uniquement

    card = load_scorecard("odds500")
    try:
        matches = odds500.fetch_matches()
    except Exception as e:               # best-effort, comme titan007
        log.warning("free_sources: odds500 indisponible (%s)", e)
        save_scorecard(record_observation(card, errors=1))
        return []
    if not matches:
        return []

    # 1. apprendre — sur les fixtures brutes, avant toute résolution
    try:
        learn_aliases([f for f in (_as_fixture(m, "odds500") for m in matches) if f])
    except Exception as e:
        log.warning("free_sources: apprentissage d'alias impossible (%s)", e)

    # 2. résoudre — un nom non résolu = match écarté
    resolved, dropped = resolve_names(matches)

    # 3. mesurer, puis décider
    card = measure_against(resolved, trusted or [], card)
    shadow, verdict, detail = evaluate_promotion(card, odds500.SPEC)
    was_shadow = bool(card.get("shadow", True))
    card["shadow"] = shadow
    if shadow != was_shadow:
        log.warning("free_sources: odds500 %s — %s", verdict, detail.get("reason"))
        card["promoted_at" if not shadow else "demoted_at"] = detail.get("reason")
    save_scorecard(card)

    if shadow:
        log.info("free_sources: odds500 en MODE OMBRE (%s) — %d matchs mesurés, "
                 "0 émis", detail.get("reason"), len(resolved))
        return []
    log.info("free_sources: odds500 PROMUE — %d matchs émis (%d écartés)",
             len(resolved), dropped)
    return resolved


def probe() -> tuple:
    """(prêt ?, détail) — pour scripts/ops.py sources."""
    card = load_scorecard("odds500")
    shadow, verdict, detail = evaluate_promotion(card, odds500.SPEC)
    stats = team_aliases.stats()
    return (not shadow), (f"odds500 {verdict} ({detail.get('reason')}) | "
                          f"dictionnaire {stats.get('utilisables', 0)}/{stats.get('total', 0)} alias")
