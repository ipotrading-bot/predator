"""
tests/test_book_execution.py — le book d'EXÉCUTION (2026-09-07).

Le 2026-09-07, la fiche « PLACER LE PARI — 1XBET » proposait Al-Adalah +0.5
@ 1.85 ; 1xbet ne cotait que +0.25 et +0.75. Le prix venait d'un line
shopping Bet365 + 1xbet, l'étiquette était écrite en dur. Ce qui est
verrouillé ici : UN nom de book, `core.constants.EXECUTION_BOOK`, dont
dérivent la reconnaissance des books chez les sources, le log du moteur et
le dashboard — jamais une deuxième copie tenue à la main (règle 6).
"""
import pathlib

from core.constants import EXECUTION_BOOK
from core.odds_api import XBET_KEY
from core.source_adapter import est_book_execution

RACINE = pathlib.Path(__file__).resolve().parent.parent


def test_le_book_d_execution_est_1xbet_decision_operateur_du_2026_09_07():
    assert EXECUTION_BOOK == "1xbet"


def test_les_graphies_du_book_sont_reconnues_pas_sa_famille():
    for nom in ("1xbet", "1xBet", "1XBET", "1x Bet", "1x-bet"):
        assert est_book_execution(nom), nom
    for nom in ("Bet365", "melbet", "MelBet", "1xBit", "BetWinner", "Pinnacle", ""):
        assert not est_book_execution(nom), nom


def test_la_cle_oddsapi_designe_le_meme_book():
    """Deux vocabulaires pour un seul book : OddsAPI dit « onexbet ». Si l'un
    change, l'autre doit suivre dans le même commit."""
    assert (XBET_KEY, EXECUTION_BOOK) == ("onexbet", "1xbet")


def test_le_moteur_et_le_dashboard_ne_nomment_plus_le_book_en_dur():
    moteur = (RACINE / "run_engine.py").read_text(encoding="utf-8")
    assert "Melbet=" not in moteur
    page = (RACINE / "templates" / "index.html").read_text(encoding="utf-8")
    assert "{{ execution_book" in page
    assert "1XBET" not in page and "1XBet" not in page
