"""
core/execution_books.py — les books où l'opérateur MISE, et le line shopping
entre eux, à ligne égale, avec le book d'origine attaché à chaque prix.

POURQUOI CE MODULE (2026-09-08)
-------------------------------
Le 2026-09-07, un line shopping Bet365 + 1xbet fusionnait les échelles
barreau par barreau sans retenir QUI cotait quoi : « Al-Adalah +0.5 @ 1.85 »
est sorti sous une fiche « 1XBET » alors que 1xbet ne cotait que +0.25 et
+0.75. L'incident a posé la condition du retour d'un second book : le book
d'origine STOCKÉ par ligne et affiché sur la fiche. C'est ce que ce module
garantit, en un seul endroit, pour les trois sources soft (OddsAPI,
odds-api.io, titan007) et pour le moteur.

DEUX RÈGLES, TOUTES DEUX DÉJÀ ÉCRITES AILLEURS ET RÉUNIES ICI
-------------------------------------------------------------
1. Marchés à ligne (handicaps, totaux) : le meilleur prix se compare À LIGNE
   ÉGALE, jamais toutes lignes confondues — choisir la ligne la mieux payée
   revient à choisir un AUTRE pari (A6, `run_engine._meme_ligne`). Chaque
   barreau fusionné porte `books` : le book qui a fourni chaque côté.
2. 1X2 : le bloc soft appartient à UN seul book. Un DNB synthétique engage
   deux jambes chez le même book (`core.math_engine.to_binary`) ; mélanger
   les issues de deux books donnerait une cote que personne n'affiche. Le
   line shopping se départage donc sur le PRIX FINAL exécutable, bloc contre
   bloc, et le bloc gagnant désigne le book.

Aucun réseau, aucun état : tout est testable tel quel
(`tests/test_second_book_execution.py`).
"""
from core.constants import EXECUTION_BOOKS


def _normaliser(nom: str) -> str:
    return "".join(ch for ch in str(nom).lower() if ch.isalnum())


_CANON = {_normaliser(b): b for b in EXECUTION_BOOKS}


def book_canonique(nom: str) -> str | None:
    """« Bet 365 », « BET365 », « 1x Bet » → l'entrée de EXECUTION_BOOKS
    qu'ils désignent, None si ce n'est aucun book d'exécution. MelBet, 1xBit,
    BetWinner sont de la famille 1xbet mais ne SONT PAS 1xbet : leurs lignes
    ne sont pas garanties identiques au moment de miser (2026-09-07)."""
    return _CANON.get(_normaliser(nom))


def est_book_execution(nom: str) -> bool:
    return book_canonique(nom) is not None


def ordre(book: str) -> int:
    """Rang dans EXECUTION_BOOKS — priorité à prix égal, et ordre stable."""
    canon = book_canonique(book) or book
    return EXECUTION_BOOKS.index(canon) if canon in EXECUTION_BOOKS else len(EXECUTION_BOOKS)


def _cotes(marche: str) -> tuple[str, str]:
    return ("home", "away") if marche == "spreads" else ("over", "under")


def _barreaux(bloc: dict) -> list[dict]:
    """Un marché à ligne vu comme liste de barreaux : son échelle si la source
    en a une (odds-api.io), sinon lui-même (OddsAPI ne rend qu'un barreau)."""
    if not bloc:
        return []
    ladder = bloc.get("ladder")
    return list(ladder) if ladder else [bloc]


def fusionner_lignes(par_book: dict[str, dict], marche: str) -> dict | None:
    """Fusionne un marché à ligne (« spreads » ou « totals ») entre books
    d'exécution, À LIGNE ÉGALE, chaque côté gardant son book.

    `par_book` : {book canonique: bloc du marché tel que la source le rend
    (`point`, deux côtés, `ladder` facultative)}. Rend un bloc de la même
    forme — la ligne principale en tête, `ladder` complète — où chaque
    barreau porte `books = {côté: book}`. Deux books qui cotent la même
    ligne : le meilleur prix par côté, le premier de EXECUTION_BOOKS à
    égalité. Une ligne qu'un seul book cote entre telle quelle, attribuée à
    lui — c'est précisément ce qui manquait le 2026-09-07.
    """
    a, b = _cotes(marche)
    par_point: dict[float, dict] = {}
    for book in sorted(par_book, key=ordre):
        for row in _barreaux(par_book[book]):
            try:
                point = float(row.get("point"))
            except (TypeError, ValueError):
                continue
            cible = par_point.setdefault(point, {"point": point, a: 0.0, b: 0.0, "books": {}})
            if marche == "spreads":
                cible["away_point"] = -point
            for cote in (a, b):
                prix = float(row.get(cote) or 0)
                if prix > 1.01 and prix > cible[cote]:
                    cible[cote] = prix
                    cible["books"][cote] = book
    ladder = [r for r in par_point.values() if r[a] > 1.01 and r[b] > 1.01]
    if not ladder:
        return None
    ladder.sort(key=lambda r: abs(r[a] - r[b]))
    return {**ladder[0], "ladder": ladder}


def book_du_cote(bloc: dict, cote: str) -> str | None:
    """Le book qui fournit ce côté du barreau retenu (après alignement sur la
    ligne du sharp). None si la ligne n'a pas d'attribution — source unique
    sans book d'exécution (repli sharp), ou slate d'avant ce module."""
    return (bloc or {}).get("books", {}).get(cote)


def choisir_bloc_h2h(par_book: dict[str, dict], sport: str, home: str, away: str):
    """Le bloc 1X2 d'UN book, celui dont le prix FINAL exécutable est le
    meilleur — jamais un maximum par issue.

    Rend (book, bloc, prix_exécutable, favori). Le favori est celui du book
    de référence (premier de EXECUTION_BOOKS qui cote) : un book qui voit
    l'autre équipe favorite n'entre pas en concurrence — ce serait comparer
    deux paris différents. Sans aucun prix exécutable : (None, {}, 0.0, "").
    """
    from core.math_engine import to_binary
    meilleur = (None, {}, 0.0, "")
    fav_ref = None
    for book in sorted(par_book, key=ordre):
        bloc = par_book[book] or {}
        prix, _, fav = to_binary(bloc, sport, home, away)
        if prix <= 1.01:
            continue
        if fav_ref is None:
            fav_ref = fav
        elif fav != fav_ref:
            continue
        if prix > meilleur[2]:
            meilleur = (book, bloc, prix, fav)
    return meilleur
