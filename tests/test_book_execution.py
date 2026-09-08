"""
tests/test_book_execution.py — les books d'EXÉCUTION (2026-09-07, élargi le
2026-09-08).

Le 2026-09-07, la fiche « PLACER LE PARI — 1XBET » proposait Al-Adalah +0.5
@ 1.85 ; 1xbet ne cotait que +0.25 et +0.75. Le prix venait d'un line
shopping Bet365 + 1xbet, l'étiquette était écrite en dur. Le 2026-09-08
l'opérateur a rouvert Bet365 : ce qui est verrouillé ici, c'est qu'UNE liste,
`core.constants.EXECUTION_BOOKS`, gouverne la reconnaissance des books chez
les sources, les clés OddsAPI, le log du moteur et le dashboard — jamais une
deuxième copie tenue à la main (règle 6) — et que le book de chaque ligne
voyage dans `soft_book` (voir tests/test_second_book_execution.py).
"""
import pathlib

from core.constants import EXECUTION_BOOK, EXECUTION_BOOKS
from core.execution_books import book_canonique
from core.odds_api import ODDS_API_BOOK_KEYS, XBET_KEY
from core.source_adapter import est_book_execution

RACINE = pathlib.Path(__file__).resolve().parent.parent


def test_les_books_d_execution_sont_1xbet_et_bet365_decision_operateur_du_2026_09_08():
    assert EXECUTION_BOOKS == ("1xbet", "bet365")
    assert EXECUTION_BOOK == EXECUTION_BOOKS[0]          # référence dérivée, pas une copie


def test_les_graphies_des_books_sont_reconnues_pas_leur_famille():
    for nom, canon in (("1xbet", "1xbet"), ("1xBet", "1xbet"), ("1XBET", "1xbet"),
                       ("1x Bet", "1xbet"), ("1x-bet", "1xbet"),
                       ("Bet365", "bet365"), ("bet 365", "bet365"), ("BET365", "bet365")):
        assert est_book_execution(nom), nom
        assert book_canonique(nom) == canon, nom
    for nom in ("melbet", "MelBet", "1xBit", "BetWinner", "Pinnacle", "Bet365 AU", ""):
        assert not est_book_execution(nom), nom


def test_les_cles_oddsapi_derivent_de_la_liste():
    """Deux vocabulaires pour un seul book : OddsAPI dit « onexbet ». Chaque
    clé désigne un book de la liste ; Bet365 n'y a pas de clé (seule la
    version australienne existe chez OddsAPI, lignes différentes)."""
    assert ODDS_API_BOOK_KEYS == {"1xbet": XBET_KEY} and XBET_KEY == "onexbet"
    assert set(ODDS_API_BOOK_KEYS) <= set(EXECUTION_BOOKS)


def test_le_moteur_et_le_dashboard_ne_nomment_plus_le_book_en_dur():
    moteur = (RACINE / "run_engine.py").read_text(encoding="utf-8")
    assert "Melbet=" not in moteur and "EXECUTION_BOOK" not in moteur
    page = (RACINE / "templates" / "index.html").read_text(encoding="utf-8")
    assert "{{ execution_book" in page                      # repli, pas étiquette
    assert "soft_book" in page                               # le book de la LIGNE
    for nom in ("1XBET", "1XBet", "BET365", "Bet365"):
        assert nom not in page, nom
