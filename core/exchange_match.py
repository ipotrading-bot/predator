"""
core/exchange_match.py — apparier un match du slate avec un marché d'exchange.

POURQUOI UN MODULE. Ces deux fonctions vivaient dans run_engine.py, où seul
l'enrichissement des prix les appelait. Depuis le 2026-08-26 la capture de
closing line en a besoin AUSSI (core/closing_line.capture_from_exchange) —
et `core` ne doit pas importer la racine. Elles sont donc ici, sans réseau ni
état, testables telles quelles.

POURQUOI PAS DANS core/source_adapter.py. Ce module-là pose une doctrine
explicite — apparier par temps + ligue + STRUCTURE de cotes, JAMAIS par nom.
`_lookup_exchange` fait exactement l'inverse : c'est l'appariement historique
par nom du chemin Betfair/Matchbook, qui n'a ni le coup d'envoi ni la ligue
côté exchange. Les mélanger rendrait la doctrine de source_adapter illisible.

Mesuré le 2026-08-20 sur 13 matchs odds-api.io contre 53 marchés Matchbook :
la clé exacte en appariait 0, le rapprochement flou 8.
"""
import difflib
import logging
from datetime import datetime

from core.math_engine import nul_cote
from core.paim_engine import _normalize_team, strict_team_match

log = logging.getLogger("PREDATOR.exchange")


def flip_exchange_prices(row: dict) -> dict:
    """Retourne les prix d'exchange d'un match trouvé dans l'autre sens.

    L'exchange peut nommer le match « B vs A » là où la source soft dit
    « A vs B ». Inverser 1 et 2 ne suffit pas : le handicap porte le SIGNE de
    l'équipe qui le concède, donc il s'inverse aussi. Un handicap laissé tel
    quel donnerait un edge calculé contre la mauvaise ligne — faux, et
    silencieux. Les totals, eux, sont symétriques et se recopient.
    """
    out = {"1": row["2"], "X": row.get("X", 0.0), "2": row["1"],
           "_source": row.get("_source", "exchange")}
    if row.get("totals"):
        out["totals"] = row["totals"]
    sp = row.get("spreads")
    if sp:
        out["spreads"] = _retourner_spread(sp)
        # L'échelle se retourne ligne par ligne : la laisser telle quelle
        # rendrait à `_aligner_sur_meme_ligne` des handicaps du point de vue
        # de l'AUTRE équipe — le signe faux que cette fonction existe pour
        # éviter, réintroduit un cran plus bas.
        if sp.get("ladder"):
            out["spreads"]["ladder"] = [_retourner_spread(r) for r in sp["ladder"]]
    return out


def _retourner_spread(sp: dict) -> dict:
    """Un handicap vu de l'autre équipe : les côtés s'échangent, le signe de
    la ligne s'inverse."""
    return {"home": sp["away"], "away": sp["home"],
            "point": sp.get("away_point", -sp["point"]),
            "away_point": sp["point"]}



def lookup_exchange(m: dict, prices: dict) -> dict | None:
    """Retrouve un match dans les prix d'exchange, malgré les noms.

    Le rapprochement par clé EXACTE ne marche pratiquement jamais entre deux
    fournisseurs : mesuré le 2026-08-20 sur 13 matchs odds-api.io contre 53
    marchés Matchbook, la clé exacte en appariait **0**, le rapprochement
    flou **8**. « Cde Juventud Italiana » contre « Club Juventud Italiana »,
    « CSD Macara » contre « Deportivo Macara »… C'est ce seul détail qui
    tenait le pipeline à zéro signal malgré deux sources en bon état.

    Ordre : clé exacte, clé exacte inversée, puis `strict_team_match` (le
    rapprochement déjà utilisé partout dans ce projet). En flou, on n'accepte
    qu'un candidat UNIQUE : deux prétendants signifient qu'on ne sait pas
    lequel est le bon, et poser le mauvais prix sharp donnerait un edge faux
    sans rien casser de visible.
    """
    h = m.get("home", "").strip()
    a = m.get("away", "").strip()
    if not h or not a:
        return None
    hl, al = h.lower(), a.lower()

    hit = prices.get(f"{hl}_{al}")
    if hit:
        return hit
    hit = prices.get(f"{al}_{hl}")
    if hit:
        return flip_exchange_prices(hit)

    forward, reverse = [], []
    for row in prices.values():
        rh, ra = str(row.get("home", "")).strip(), str(row.get("away", "")).strip()
        # `strict_team_match` renvoie True dès qu'un nom est VIDE (voir
        # core/paim_engine.py) : sans ce garde, une ligne de prix sans
        # home/away s'apparierait à n'importe quel match. Le seuil de
        # longueur écarte de même les fragments trop courts, qu'un simple
        # test d'inclusion ferait matcher avec tout ("a" est dans
        # "barcelona").
        if len(rh) < 3 or len(ra) < 3 or len(h) < 3 or len(a) < 3:
            continue
        if strict_team_match(h, rh) and strict_team_match(a, ra):
            forward.append(row)
        elif strict_team_match(h, ra) and strict_team_match(a, rh):
            reverse.append(row)
    if len(forward) == 1 and not reverse:
        return forward[0]
    if len(reverse) == 1 and not forward:
        return flip_exchange_prices(reverse[0])
    # Plusieurs candidats : UN match écrit de plusieurs façons (deux exchanges,
    # voir « Comblement » plus bas) n'est pas une ambiguïté. Deux matchs
    # différents en restent une.
    if forward and not reverse and _meme_match(forward):
        return _prefere(forward)
    if reverse and not forward and _meme_match(reverse):
        return flip_exchange_prices(_prefere(reverse))
    return None


# ── Comblement : un second exchange derrière le premier ───────────────────
# Deux lignes pour UN match rendaient ce match introuvable. `lookup_exchange`
# n'acceptait en flou qu'un candidat UNIQUE ; or le comblement ajoute toute
# ligne dont la CLÉ exacte manque, y compris le même match écrit autrement
# (« santos fc_flamengo » chez Matchbook, « santos_flamengo » chez Smarkets).
# Mesuré le 2026-10-08 sur un slate de 62 matchs de foot d'odds-api.io :
# Santos–Flamengo, Fluminense–Coritiba, Heidenheim–Kaiserslautern,
# Moreirense–Gil Vicente et Montpellier–Grenoble sans prix sharp alors que
# les DEUX exchanges les cotaient — les matchs les plus liquides d'abord.
# Les deux orthographes sont GARDÉES (les retirer perdait Palmeiras–Bahia,
# que seule celle de Smarkets appariait) : c'est `lookup_exchange` qui
# reconnaît que ses candidats sont le même match, et prend le premier.
# Et à clé égale la première ligne gagnait toujours, même quand elle cotait
# le NUL hors football (1X2 de temps réglementaire, que le moteur ignore) et
# que la seconde portait la vraie moneyline : Islanders–Blackhawks et
# Flames–Avalanche sans prix sharp le même soir.
_MEME_AFFICHE_H = 12.0      # au-delà, même affiche = autre match (série MLB)


def _meme_match(rows: list) -> bool:
    """Ces lignes, toutes appariées au même match du slate, désignent-elles
    UN seul match ? Une ligne par exchange, camps appariés deux à deux avec
    la première, et coups d'envoi à moins de _MEME_AFFICHE_H quand ils sont
    connus."""
    # Un exchange ne liste pas deux fois le même match : deux lignes de la
    # MÊME source (ou de source inconnue) sont deux matchs, donc un doute.
    sources = [r.get("_source") for r in rows]
    if None in sources or len(set(sources)) != len(sources):
        return False
    ref = rows[0]
    t0 = _instant(ref.get("commence_time"))
    for r in rows[1:]:
        if not (strict_team_match(str(ref.get("home", "")), str(r.get("home", "")))
                and strict_team_match(str(ref.get("away", "")), str(r.get("away", "")))):
            return False
        t1 = _instant(r.get("commence_time"))
        if t0 and t1 and abs((t0 - t1).total_seconds()) > _MEME_AFFICHE_H * 3600:
            return False
    return True


def _prefere(rows: list) -> dict:
    """La première ligne (l'ordre d'insertion = l'ordre des exchanges), sauf
    si elle cote le nul et qu'une autre ne le cote pas : hors football c'est
    la moneyline, que le moteur sait lire. Au football toutes cotent le nul."""
    if nul_cote(rows[0]):
        for r in rows[1:]:
            if not nul_cote(r):
                return r
    return rows[0]


def combler(prices: dict, comblement: dict) -> int:
    """Ajoute à `prices` (modifié en place) les lignes de `comblement` et rend
    le nombre de matchs NOUVEAUX (qu'aucune ligne déjà là ne désignait, au
    sens du rapprochement flou). À clé égale la ligne en place reste, sauf si
    elle cote le nul et que le comblement porte la moneyline à deux issues."""
    nouveaux = 0
    for k, v in comblement.items():
        if not isinstance(v, dict):
            continue
        if k in prices:
            if nul_cote(prices[k]) and not nul_cote(v):
                prices[k] = v
            continue
        if lookup_exchange({"home": v.get("home", ""), "away": v.get("away", "")}, prices) is None:
            nouveaux += 1
        prices[k] = v
    return nouveaux


# ── Diagnostic : le match écarté « Échec prix Sharp » avait-il un jumeau ? ──
# Mesure du 2026-09-29 (INCIDENTS.md, « que peut apporter l'IA ») : ~137
# matchs du Tier 2 écartés faute de prix sharp sur UN scan standard, loggés
# en total par sport seulement — impossible de dire combien étaient de vrais
# marchés sans sharp et combien un nom que `lookup_exchange` n'a pas su
# apparier (« Köln » / « Cologne »). Ce diagnostic ne pose AUCUN prix : il
# nomme le candidat le plus proche, pour qu'un humain tranche au log.
#
# Critère « nom probable » : un des deux camps ressemble à ≥ NOM_PROCHE_MIN,
# au même horaire (± FENETRE_NOM_H). Une équipe ne joue qu'un match par jour :
# un camp identique à la même heure désigne presque sûrement le même match,
# même si l'autre camp est écrit autrement — c'est exactement le cas qu'un
# ratio sur les DEUX noms (celui de lookup_exchange) refuse.
NOM_PROCHE_MIN = 0.90
FENETRE_NOM_H = 3.0


def _instant(valeur):
    try:
        return datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def preparer_candidats(prices) -> list[tuple]:
    """Lignes de prix sharp prêtes pour `candidat_proche` : (libellé, domicile
    normalisé, extérieur normalisé, coup d'envoi). À faire UNE fois par scan —
    normaliser à chaque comparaison coûtait le tiers du temps (profil du
    2026-09-29 : 180 000 appels pour 150 matchs × 600 lignes)."""
    lignes = []
    for row in (prices.values() if isinstance(prices, dict) else prices or ()):
        if not isinstance(row, dict):
            continue
        rh, ra = str(row.get("home", "")).strip(), str(row.get("away", "")).strip()
        if len(rh) < 3 or len(ra) < 3:
            continue
        lignes.append((row.get("match") or f"{rh} vs {ra}", _normalize_team(rh),
                       _normalize_team(ra), _instant(row.get("commence_time"))))
    return lignes


def candidat_proche(m: dict, lignes: list[tuple]) -> tuple[str, float, float] | None:
    """(libellé, meilleur camp, pire camp) du prix sharp le plus proche par
    le NOM du match `m`, parmi des lignes de `preparer_candidats`, ou None
    s'il n'y a aucun candidat comparable. Pur.

    Une ligne dont l'horaire est connu des deux côtés et s'écarte de plus de
    FENETRE_NOM_H est ignorée ; un horaire manquant ne l'exclut pas (le
    diagnostic préfère un candidat de trop à un jumeau manqué). Les deux sens
    (« A vs B » / « B vs A ») sont essayés, comme dans lookup_exchange."""
    h, a = str(m.get("home", "")).strip(), str(m.get("away", "")).strip()
    if len(h) < 3 or len(a) < 3:
        return None
    # Le nom du match est la séquence INDEXÉE (seq2) : difflib ne construit
    # son index qu'une fois par match, pas une fois par comparaison.
    sm_h, sm_a = difflib.SequenceMatcher(None), difflib.SequenceMatcher(None)
    sm_h.set_seq2(_normalize_team(h))
    sm_a.set_seq2(_normalize_team(a))
    t0 = _instant(m.get("commence_time"))
    meilleur, cle = None, (-1.0, -1.0)
    for libelle, nrh, nra, t1 in lignes:
        if t0 and t1 and abs((t0 - t1).total_seconds()) > FENETRE_NOM_H * 3600:
            continue
        for x, y in ((nrh, nra), (nra, nrh)):
            sm_h.set_seq1(x)
            sm_a.set_seq1(y)
            # quick_ratio() BORNE ratio() par le haut pour une fraction du
            # coût : une paire qui ne peut pas battre le meilleur camp déjà vu
            # n'est pas calculée.
            if max(sm_h.quick_ratio(), sm_a.quick_ratio()) < cle[0]:
                continue
            s1, s2 = sm_h.ratio(), sm_a.ratio()
            k = (max(s1, s2), min(s1, s2))
            if k > cle:
                cle, meilleur = k, libelle
    if meilleur is None:
        return None
    return meilleur, round(cle[0], 2), round(cle[1], 2)


def nom_probable(candidat: tuple[str, float, float] | None) -> bool:
    """Le candidat désigne-t-il probablement le même match ? Voir
    NOM_PROCHE_MIN."""
    return bool(candidat) and candidat[1] >= NOM_PROCHE_MIN
