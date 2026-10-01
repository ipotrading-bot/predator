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
            cible = par_point.setdefault(point, {"point": point, a: 0.0, b: 0.0, "books": {},
                                                 "prix_books": {a: {}, b: {}}})
            if marche == "spreads":
                cible["away_point"] = -point
            for cote in (a, b):
                prix = float(row.get(cote) or 0)
                if prix > 1.01:
                    # TOUS les prix, pas seulement le meilleur (2026-09-27) :
                    # voir prix_du_book.
                    cible["prix_books"][cote][book] = prix
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


def prix_du_book(bloc: dict, cote: str, book: str) -> float:
    """Le prix que CE book affiche sur ce côté du barreau retenu, 0.0 s'il ne
    le cote pas (ou si la ligne vient d'une source non fusionnée).

    Pourquoi (2026-09-27) : la fusion ne gardait que le MEILLEUR prix par
    côté. Bet365 à 1,95 contre 1xbet à 1,90 sur la même ligne, et le signal
    partait chez Bet365 — que l'opérateur ne joue pas — alors que 1,90
    passait largement le seuil. 48 recommandés sur 141 (34 %) sont sortis
    chez Bet365 du 09/09 au 27/09. Garder chaque prix permet au moteur de
    proposer d'abord le book de référence (run_engine._emit_book_prefere)."""
    try:
        return float(((bloc or {}).get("prix_books") or {}).get(cote, {}).get(book) or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def prix_reference(bloc: dict, cote: str) -> tuple[str, float]:
    """(book de référence, son prix sur ce côté du barreau) — le book de
    référence est le premier de EXECUTION_BOOKS, celui que l'opérateur joue
    (2026-09-27). Prix 0.0 s'il ne cote pas ce côté de cette ligne. Seul ce
    module connaît la liste : le moteur ne nomme aucun book (voir
    tests/test_book_execution.py)."""
    reference = EXECUTION_BOOKS[0]
    return reference, prix_du_book(bloc, cote, reference)


# ── Temps réglementaire (2026-10-01) ─────────────────────────────────────
# Un book qui règle un sport sur le TEMPS RÉGLEMENTAIRE ne vend pas le pari
# que le sharp cote : Pinnacle, Matchbook et Bet365 comptent la prolongation
# et les tirs au but sur la NHL. Règlement de 1xbet : « bets are settled on
# regular time », le marché prolongation comprise étant libellé à part
# (« Including Overtime »). Mesuré le 2026-10-01 :
#   · 1X2 — bloc à trois issues, écarté par `math_engine.nul_cote` ;
#   · totaux — un 3-3 à la 60e est Under 6.5 chez 1xbet, Over chez le sharp
#     (le but de prolongation compte). Les 4 signaux NHL totals émis du 29/09
#     au 01/10 étaient des Over 1xbet à +7,6..+12 %, les Under sortaient à
#     −12..−15 % quand Bet365, à la même ligne, sortait à −3,9 % ;
#   · handicaps — équivalents à partir de ±1,5 seulement : une prolongation
#     se gagne d'un but, donc un écart de deux buts ou plus est acquis à la
#     60e dans les deux lectures ; à 0 et ±1 le nul réglementaire les sépare.
# Écarter ces prix n'est pas un choix de périmètre : c'est le refus de
# comparer deux paris différents, la règle de `run_engine._meme_ligne`.
# INCIDENTS.md « 1xbet cote le hockey en temps réglementaire ».
TEMPS_REGLEMENTAIRE: dict[str, frozenset] = {"1xbet": frozenset({"hockey"})}
HANDICAP_EQUIVALENT_MIN = 1.5


def books_temps_reglementaire(sport: str) -> frozenset:
    """Books d'exécution qui règlent ce sport sur le temps réglementaire."""
    s = (sport or "").lower()
    return frozenset(b for b, sports in TEMPS_REGLEMENTAIRE.items() if s in sports)


def retirer_temps_reglementaire(bloc: dict | None, marche: str,
                                sport: str) -> tuple[dict | None, list[str]]:
    """Le marché à ligne fusionné, SANS les prix qu'un book règle sur le temps
    réglementaire quand le sharp compte la prolongation.

    Rend (bloc, books retirés). Bloc inchangé si le sport n'est pas concerné
    ou si aucun prix ne vient d'un tel book ; None s'il ne reste aucun barreau
    à deux côtés. Un barreau sans attribution (`books` absent : slate d'avant
    le 2026-09-08, repli sharp) est laissé tel quel — on ne retire pas ce
    qu'on ne sait pas attribuer. Pur."""
    exclus = books_temps_reglementaire(sport)
    if not bloc or not exclus:
        return bloc, []
    a, b = _cotes(marche)
    retires: set[str] = set()
    ladder = []
    for row in _barreaux(bloc):
        try:
            point = float(row.get("point"))
        except (TypeError, ValueError):
            continue
        if marche == "spreads" and abs(point) >= HANDICAP_EQUIVALENT_MIN:
            ladder.append(row)
            continue
        neuf = {**row, "books": dict(row.get("books") or {})}
        prix_books = row.get("prix_books")
        if prix_books is not None:
            neuf["prix_books"] = {a: {}, b: {}}
        for cote in (a, b):
            if prix_books is not None:
                tous = prix_books.get(cote) or {}
                retires.update(set(tous) & exclus)
                gardes = {bk: float(p) for bk, p in tous.items() if bk not in exclus}
                neuf["prix_books"][cote] = gardes
                # Meilleur prix, premier de EXECUTION_BOOKS à égalité — la
                # règle de `fusionner_lignes`.
                bk = min(gardes, key=lambda k: (-gardes[k], ordre(k))) if gardes else None
                neuf[cote] = gardes[bk] if bk else 0.0
                neuf["books"].pop(cote, None)
                if bk:
                    neuf["books"][cote] = bk
            elif neuf["books"].get(cote) in exclus:
                retires.add(neuf["books"].pop(cote))
                neuf[cote] = 0.0
        if float(neuf.get(a) or 0) > 1.01 and float(neuf.get(b) or 0) > 1.01:
            ladder.append(neuf)
    if not retires:
        return bloc, []
    if not ladder:
        return None, sorted(retires, key=ordre)
    ladder.sort(key=lambda r: abs(float(r[a]) - float(r[b])))
    return {**ladder[0], "ladder": ladder}, sorted(retires, key=ordre)


def avertissement_hors_reference(book: str | None) -> str | None:
    """« ⚠️ bet365 seulement — absent ou insuffisant chez 1xbet » pour un
    signal émis chez un autre book que celui de référence ; None sinon (ou
    book inconnu : on n'avertit pas sur ce qu'on ne sait pas).

    Depuis `run_engine._emit_book_prefere` (2026-09-27), un signal ne sort
    chez un second book QUE si le book de référence ne cote pas cette ligne
    ou n'y passe pas les barrières : l'avertissement dit exactement cela.
    Pas de `*` ni de `_` : il s'insère dans du Markdown Telegram."""
    canon = book_canonique(book or "")
    if canon is None or canon == EXECUTION_BOOKS[0]:
        return None
    return f"⚠️ {canon} seulement — absent ou insuffisant chez {EXECUTION_BOOKS[0]}"
