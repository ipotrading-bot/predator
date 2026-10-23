"""
core/mesure_books.py — MESURE SEULE, jamais une émission (2026-10-23) :
quel book, s'il était joué, rendrait des 1X2 de football exécutables ?

POURQUOI
--------
Jusqu'au 2026-09-07, `titan007._soft_price` prenait le MEILLEUR prix parmi
une douzaine de books (line shopping borné par la médiane). C'est ce qui
portait le 1X2 foot d'août : ~2 recommandés par jour, sur des prix que
l'opérateur ne pouvait pas prendre chez 1xbet. Depuis la décision « 1xbet
seul » (règle 11), le DNB de 1xbet ne bat presque jamais Pinnacle : au scan
du 2026-10-23 09:11, 20 DNB évalués, EV max +0,38 %. Rouvrir le line
shopping, c'est ouvrir des comptes — une décision opérateur qui ne se prend
pas à l'aveugle. Cette mesure dit, book par book, combien de matchs
passeraient la porte d'émission.

CE QUI EST MESURÉ
-----------------
Pour chaque match de football que titan007 cote déjà (zéro requête de
plus), dans la zone jouable (T-2h…T-24h) et au périmètre (ni jeunes, ni
ligue exclue) : le DNB du FAVORI chez chaque book de `BOOKS_CANDIDATS`,
contre la probabilité Pinnacle dévigorisée — le calcul de `_process_h2h`.
Le prix d'un book passe d'abord le plafond anti-book-figé de titan007
(médiane des books soft × MAX_SOFT_OUTLIER) : sans lui, le meilleur prix
parmi des dizaines de books est presque toujours un book en retard.

Le meilleur EV de la journée par (match, book) est gardé dans
`meta.mesure_books_AAAAMMJJ` — une clé par jour, scans standard seulement
(le reprice relit un slate en cache : ses prix soft ne sont pas frais).
Deux scans simultanés peuvent perdre une écriture : c'est une mesure, pas
une comptabilité.

LA PORTE APPLIQUÉE À LA LECTURE (`passe`)
-----------------------------------------
Celle de `_emit` pour un DNB foot : EV ≥ max(seuil, EV_EDGE_FLOOR), où le
seuil vaut 0,8 au-dessus de AH0_VALUE_THRESHOLD (règle AH0) et le seuil
appris du football en dessous ; EV ≤ SUSPECT_EDGE ; probabilité sharp ≥
SHARP_PROB_BY_MARKET["h2h_soccer"] ; EV positif sous le devig le plus
défavorable. Les plafonds APPRIS (edge, cote) ne sont pas rejoués : la
mesure surestime donc légèrement le volume.

Lecture : `python scripts/ops.py candidats [jours]`. Échéance : 2026-10-30
(7 jours). Aucun book n'entre dans `EXECUTION_BOOKS` sans instruction
opérateur (règle 11).
"""
from core.math_engine import calc_dnb, devig_bounds, to_binary

# Books mesurés : nom EXACT dans le flux titan007 (en minuscules) → famille.
# Choisis pour être ouvrables depuis l'Afrique de l'Ouest francophone (mises
# en XOF) ou pour servir de témoin. La famille compte : les clones de 1xbet
# reprennent largement ses cotes, ils n'apportent pas un prix INDÉPENDANT.
# Ajouter un book ici ne le rend pas jouable : il est seulement mesuré.
BOOKS_CANDIDATS: dict[str, str] = {
    "1xbet": "1xbet", "melbet": "1xbet", "betwinner": "1xbet",
    "22bet": "1xbet", "megapari": "1xbet", "paripesa": "1xbet",
    "888starz": "1xbet", "1xbit": "1xbet",
    "bet 365": "bet365",
    "betway": "betway",
    "parimatch": "parimatch",
    "mostbet": "mostbet",
    "sportybet": "sportybet",
    "betking": "betking",
    "bet9ja": "bet9ja",
    "stake": "stake",
    "marathonbet": "marathonbet",
    "sbobet": "sbobet",
    "betano": "betano",
    "mozzart": "mozzart",
    "unibet": "unibet",
    "william hill": "william hill",
    "bwin": "bwin",
    "betsson": "betsson",
}

META_PREFIX = "mesure_books_"


def cle_du_jour(now) -> str:
    """Clé meta du jour UTC : `mesure_books_AAAAMMJJ`."""
    return META_PREFIX + now.strftime("%Y%m%d")


def blocs_candidats(books: dict, medianes: dict, plafond: float) -> dict[str, dict]:
    """{book candidat: bloc 1X2} pour chaque book de BOOKS_CANDIDATS présent,
    chaque issue bornée par `médiane × plafond` (au-dessus : issue à 0, le
    book est figé sur ce côté). Un bloc sans les trois issues est écarté :
    le DNB se construit avec le nul. Pur."""
    out: dict[str, dict] = {}
    for nom, bloc in (books or {}).items():
        cle = str(nom).strip().lower()
        if cle not in BOOKS_CANDIDATS or cle in out:
            continue
        neuf = {}
        for k in ("1", "X", "2"):
            mine = float((bloc or {}).get(k) or 0)
            med = (medianes or {}).get(k)
            neuf[k] = mine if med and 1.01 < mine <= med * plafond else 0.0
        if all(neuf[k] > 1.01 for k in ("1", "X", "2")):
            out[cle] = neuf
    return out


def ev_par_book(blocs: dict, sharp: dict, home: str, away: str) -> dict[str, list]:
    """{book: [EV %, cote DNB, prob. sharp %, EV pire devig %]} du DNB du
    favori de chaque book, contre le 1X2 sharp dévigorisé comme dans
    `run_engine._process_h2h`. Book sans prix exécutable : absent. Pur."""
    s1, sx, s2 = (float((sharp or {}).get(k) or 0) for k in ("1", "X", "2"))
    if min(s1, sx, s2) <= 1.01:
        return {}
    out: dict[str, list] = {}
    for book, bloc in (blocs or {}).items():
        prix, _, fav = to_binary(bloc, "soccer", home, away)
        if prix <= 1.01 or not fav:
            continue
        own, other = (s1, s2) if fav == home else (s2, s1)
        prob, prob_cons = devig_bounds(calc_dnb(own, other, sx), calc_dnb(other, own, sx))
        if prob <= 0:
            continue
        out[book] = [round((prob * prix - 1) * 100, 2), round(prix, 3),
                     round(prob * 100, 1), round((prob_cons * prix - 1) * 100, 2)]
    return out


def accumuler(blob: dict | None, match_id: str, match: str, league: str,
              kickoff: str, majeure: bool, evs: dict) -> dict:
    """Ajoute la mesure d'un match au blob du jour, en gardant pour chaque
    book la meilleure EV vue dans la journée. Pur — rend un NOUVEAU blob."""
    out = {"matchs": dict((blob or {}).get("matchs") or {})}
    if not evs:
        return out
    prec = out["matchs"].get(match_id) or {}
    books = dict(prec.get("b") or {})
    for book, mesure in evs.items():
        if book not in books or mesure[0] > books[book][0]:
            books[book] = list(mesure)
    out["matchs"][match_id] = {"m": match, "l": league, "t": kickoff,
                               "maj": bool(majeure), "b": books}
    return out


def passe(mesure: list, seuil_foot: float, plancher: float, ah0_seuil: float,
          suspect: float, prob_min: float) -> bool:
    """La porte de `_emit` pour un DNB foot (voir l'en-tête). Pur."""
    ev, prix, prob, ev_cons = mesure[:4]
    seuil = 0.8 if prix > ah0_seuil else seuil_foot
    return (max(seuil, plancher) <= ev <= suspect
            and prob / 100 >= prob_min and ev_cons > 0)


def classement(blobs: list[dict], porte) -> list[dict]:
    """Par book : matchs cotés, matchs qui passent la porte, EV moyenne de
    ceux-là, dont majeures, et APPORT = matchs qui passent chez ce book et
    chez AUCUN book de la famille 1xbet (le seul gain réel d'un compte de
    plus). Trié par matchs qui passent. Pur."""
    stats: dict[str, dict] = {}
    for blob in blobs or []:
        for m in ((blob or {}).get("matchs") or {}).values():
            books = m.get("b") or {}
            famille_1x = any(porte(v) for b, v in books.items()
                             if BOOKS_CANDIDATS.get(b) == "1xbet")
            for book, mesure in books.items():
                s = stats.setdefault(book, {"book": book, "famille": BOOKS_CANDIDATS.get(book, "?"),
                                            "cotes": 0, "passent": 0, "majeures": 0,
                                            "apport": 0, "ev_total": 0.0})
                s["cotes"] += 1
                if porte(mesure):
                    s["passent"] += 1
                    s["ev_total"] += mesure[0]
                    s["majeures"] += bool(m.get("maj"))
                    s["apport"] += not famille_1x
    rows = []
    for s in stats.values():
        s["ev_moy"] = round(s.pop("ev_total") / s["passent"], 2) if s["passent"] else None
        rows.append(s)
    rows.sort(key=lambda r: (-r["passent"], -r["cotes"], r["book"]))
    return rows
