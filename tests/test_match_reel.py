"""Gardien — le même match de tennis vu par deux sources est UN match (2026-10-02).

« Xinran Sun vs Cristina Bucsa » (odds-api.io, 03/10 03:00 UTC) et « Sun
Xinran vs Cristina Bucsa » (OddsAPI, 03/10 07:30 UTC) : deux cartes sur le
dashboard, « 5 matchs » pour 4, et une garde d'émission aveugle à ce jumeau.
Un tournoi publie un ORDRE DE JEU, pas un coup d'envoi (une source donne
l'ouverture de la session, l'autre une estimation), et une source écrit le
nom de famille en premier. Voir core/match_reel.py.
"""
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.match_reel import (JUMEAU_FENETRE_MIN, JUMEAU_FENETRE_MIN_PAR_SPORT,
                             fenetre_jumeau_min, meme_match_reel)


def _s(match, mt, mid, sport="tennis"):
    return {"match": match, "match_time": mt, "match_id": mid, "sport": sport}


_SUN_IO = _s("Xinran Sun vs Cristina Bucsa", "2026-10-03T03:00:00+00:00", "oai_75051548")
_SUN_ODDSAPI = _s("Sun Xinran vs Cristina Bucsa", "2026-10-03T07:30:00+00:00",
                  "c666871f9f247e6d225d808cf0a40c51")

# Les 11 matchs de tennis présents dans `signals` le 2026-10-02.
_TENNIS_EN_BASE = [
    _s("Alexander Zverev vs Botic van de Zandschulp", "2026-09-09T23:30:00+00:00", "a1"),
    _s("Anna Bondár vs Renata Zarazua", "2026-09-30T12:15:00+00:00", "a2"),
    _s("Arthur Fils vs Frances Tiafoe", "2026-10-02T08:10:00+00:00", "a3"),
    _s("Aryna Sabalenka vs Jessica Pegula", "2026-09-10T23:00:00+00:00", "a4"),
    _s("Ben Shelton vs Stefanos Tsitsipas", "2026-09-06T23:15:00+00:00", "a5"),
    _s("Iga Swiatek vs Qinwen Zheng", "2026-09-07T15:00:00+00:00", "a6"),
    _s("Iva Jović vs Coco Gauff", "2026-09-07T23:15:00+00:00", "a7"),
    _s("Jaume Munar vs Jaime Faria", "2026-10-02T02:00:00+00:00", "a8"),
    _SUN_ODDSAPI, _SUN_IO,
    _s("Yunchaokete Bu vs Novak Djokovic", "2026-10-02T12:30:00+00:00", "a9"),
]


class TestLeCasDu02_10:
    def test_les_deux_sources_voient_le_meme_match(self):
        assert meme_match_reel(_SUN_IO, _SUN_ODDSAPI)
        assert meme_match_reel(_SUN_ODDSAPI, _SUN_IO)

    def test_le_rejeu_sur_la_base_ne_trouve_que_cette_paire(self):
        paires = [(a["match"], b["match"])
                  for i, a in enumerate(_TENNIS_EN_BASE) for b in _TENNIS_EN_BASE[i + 1:]
                  if meme_match_reel(a, b)]
        assert paires == [("Sun Xinran vs Cristina Bucsa", "Xinran Sun vs Cristina Bucsa")]


class TestLesNomsDeJoueurs:
    """Les mêmes mots dans un autre ordre sont la même personne — libellés
    relevés dans le scan standard de 11:10 le 2026-10-02."""

    @pytest.mark.parametrize("a,b", [
        ("Bu Yunchaokete vs Novak Djokovic", "Yunchaokete Bu vs Novak Djokovic"),
        ("Iga Swiatek vs Zheng Qinwen", "Iga Swiatek vs Qinwen Zheng"),
        ("Xinyu Gao vs Iga Swiatek", "Gao Xinyu vs Iga Swiatek"),
        ("Coco Gauff vs Maria Camila Osorio Serrano", "Coco Gauff vs Camila Osorio"),
        ("Iva Jović vs Harriet Dart", "Iva Jovic vs Harriet Dart"),
    ])
    def test_les_deux_ecritures_sont_le_meme_match(self, a, b):
        mt = "2026-10-02T06:00:00+00:00"
        assert meme_match_reel(_s(a, mt, "x"), _s(b, mt, "y"))

    def test_un_autre_adversaire_reste_un_autre_match(self):
        mt = "2026-10-02T06:00:00+00:00"
        assert not meme_match_reel(_s("Zheng Qinwen vs Iga Swiatek", mt, "x"),
                                   _s("Qinwen Zheng vs Anna Kalinskaya", mt, "y"))

    def test_l_ordre_des_mots_ne_vaut_que_pour_les_joueurs(self):
        # « Wanderers Bolton » n'est pas « Bolton Wanderers » par décret : pour
        # un club, seul `strict_team_match` juge, comme avant.
        from core.paim_engine import strict_team_match
        a, b = "Sporting Real FC vs Tigres UANL", "Real Sporting FC vs Tigres UANL"
        attendu = strict_team_match("Sporting Real FC", "Real Sporting FC")
        mt = "2026-10-02T18:00:00+00:00"
        assert meme_match_reel(_s(a, mt, "x", "soccer"), _s(b, mt, "y", "soccer")) is attendu


class TestLaFenetre:
    def test_le_tennis_a_douze_heures_les_autres_trente_minutes(self):
        assert fenetre_jumeau_min("tennis") == 720 == JUMEAU_FENETRE_MIN_PAR_SPORT["tennis"]
        assert fenetre_jumeau_min("soccer") == fenetre_jumeau_min(None) == JUMEAU_FENETRE_MIN == 30

    def test_au_dela_de_douze_heures_ce_sont_deux_matchs(self):
        # Un report au lendemain, ou la même affiche au tour suivant d'un
        # autre tournoi : pas le même match.
        tard = dict(_SUN_ODDSAPI, match_time="2026-10-03T15:01:00+00:00")
        assert not meme_match_reel(_SUN_IO, tard)

    def test_le_doubleheader_de_baseball_reste_deux_matchs(self):
        assert not meme_match_reel(
            _s("Yankees vs Red Sox", "2026-09-02T17:00:00+00:00", "a", "baseball"),
            _s("Yankees vs Red Sox", "2026-09-02T17:31:00+00:00", "b", "baseball"))

    def test_la_lecture_des_actifs_prend_la_fenetre_la_plus_large(self):
        import run_engine
        src = inspect.getsource(run_engine._actifs_des_matchs)
        assert "max(_fenetre_jumeau_min(sp) for sp in sports)" in src


class TestUneSeuleDefinition:
    def test_le_moteur_et_le_dashboard_partagent_la_fonction(self):
        import api.index as dash
        import run_engine
        assert run_engine._meme_match_reel is meme_match_reel
        assert dash._meme_match_reel is meme_match_reel
        assert "def _meme_match_reel" not in inspect.getsource(run_engine)


class TestLaGardeDEmission:
    def test_le_meme_pari_venu_de_l_autre_source_est_refuse(self):
        import logging
        from run_engine import _sans_contradiction
        tenu = dict(_SUN_IO, id=1, market_key="h2h", selection_name="Cristina Bucsa")
        jumeau = dict(_SUN_ODDSAPI, market_key="h2h", selection_name="Cristina Bucsa")
        assert _sans_contradiction([jumeau], [tenu], logging.getLogger("t")) == []

    def test_une_autre_famille_sur_le_meme_match_passe(self):
        import logging
        from run_engine import _sans_contradiction
        tenu = dict(_SUN_IO, id=1, market_key="h2h", selection_name="Cristina Bucsa")
        total = dict(_SUN_ODDSAPI, market_key="totals_under", selection_name="Under 20.5")
        assert _sans_contradiction([total], [tenu], logging.getLogger("t")) == [total]
