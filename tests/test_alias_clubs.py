"""
tests/test_alias_clubs.py — « CD Inca » et « Inca Aruba » sont le même club (2026-10-02).

« CD Inca vs Municipal Limeno » (Primera salvadorienne, bet365) est resté
ACTIF : LiveScore publiait le match, terminé 2-1, sous « Inca Aruba vs
Municipal Limeno ». Le club porte son nom de sponsor chez la source de
scores ; la sonde `_journal_alias` s'est tue (5 événements à un seul camp
reconnu) et l'audit est sorti stérile. Même jour : « Reilac Shiga FC »
(1xbet) est « MIO Biwako Shiga » chez LiveScore.

Ce que ces tests tiennent : la table d'alias AJOUTE une variante du nom servi
par la source, sur le nom ENTIER, et n'assouplit AUCUNE garde — les deux
camps, un candidat unique, un match terminé. Le pont d'alias APPRIS reste
interdit (INCIDENTS.md « Le pont d'alias ») : chaque ligne est promue à la
main, après avoir vu les deux écritures sur la même affiche.
"""
import unicodedata

import pytest

from core import score_sources as ss
from core.score_sources import _ALIAS_CLUBS, _EXONYMES_BIDIR, _apparie, _variantes


@pytest.fixture(autouse=True)
def _caches_neufs():
    ss.reset_cache()
    yield
    ss.reset_cache()


def _ls_payload(events, ligue="El Salvador - Primera Division: Apertura"):
    """Une réponse LiveScore : Stages[] > Events[], camps en listes."""
    return {"Stages": [{"Cnm": ligue.split(" - ")[0], "Snm": ligue.split(" - ")[-1],
                        "Events": [{"Eid": eid, "T1": [{"Nm": h}], "T2": [{"Nm": a}],
                                    "Tr1": hs, "Tr2": as_, "Eps": st}
                                   for (eid, h, a, hs, as_, st) in events]}]}


class TestVariantes:
    def test_les_deux_sens(self):
        assert "cd inca" in _variantes("Inca Aruba")
        assert "inca aruba" in _variantes("CD Inca")
        assert "reilac shiga fc" in _variantes("MIO Biwako Shiga")

    def test_le_nom_dorigine_reste_en_tete(self):
        assert _variantes("Inca Aruba")[0] == "inca aruba"

    def test_le_nom_est_remplace_ENTIER(self):
        # Une réserve, une section féminine ou un homonyme partiel ne sont pas
        # le club de la table : seul le libellé exact reçoit la variante.
        for nom in ("Inca Aruba II", "Inca Aruba Women", "Real Inca Aruba",
                    "Shiga United", "Inca"):
            assert _variantes(nom) == [nom.lower()], nom

    def test_un_nom_hors_table_se_comporte_comme_avant(self):
        assert _variantes("Municipal Limeno") == ["municipal limeno"]


class TestAppariement:
    def test_les_cas_du_02_10(self):
        assert _apparie("CD Inca", "Inca Aruba")
        assert _apparie("Reilac Shiga FC", "MIO Biwako Shiga")

    def test_sans_la_table_le_nom_ne_passe_pas(self, monkeypatch):
        # Ce que l'alias répare : rien d'autre ne rapproche ces deux écritures.
        monkeypatch.setattr(ss, "_ALIAS_CLUBS_BIDIR", {})
        assert not _apparie("CD Inca", "Inca Aruba")
        assert not _apparie("Reilac Shiga FC", "MIO Biwako Shiga")

    def test_l_alias_ne_rapproche_pas_un_autre_club(self):
        assert not _apparie("Municipal Limeno", "Inca Aruba")
        assert not _apparie("Roasso Kumamoto", "MIO Biwako Shiga")


class TestContratDeReglement:
    """L'alias ne règle rien seul : LiveScore exige toujours les DEUX camps,
    sur un candidat UNIQUE, terminé."""

    # La journée réelle du 2026-10-02 autour du match : les voisins sont ceux
    # que la sonde citait (« China vs Palestine », « Mixco vs CSD Municipal »).
    _JOURNEE = [("1", "Inca Aruba", "Municipal Limeno", 2, 1, "FT"),
                ("2", "China", "Palestine", 1, 0, "FT"),
                ("3", "Mixco", "CSD Municipal", 0, 0, "FT")]

    def test_le_match_du_02_10_est_regle(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload(self._JOURNEE))
        r = ss.result_from_livescore("CD Inca vs Municipal Limeno", "soccer", "2026-10-02")
        assert r == {"home_score": 2, "away_score": 1, "completed": True,
                     "source": "livescore"}

    def test_un_seul_camp_ne_regle_toujours_pas(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Inca Aruba", "Alianza FC", 2, 1, "FT")]))
        assert ss.result_from_livescore("CD Inca vs Municipal Limeno", "soccer",
                                        "2026-10-02") is None

    def test_un_match_en_cours_ne_regle_toujours_pas(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Inca Aruba", "Municipal Limeno", 2, 1, "82'")]))
        assert ss.result_from_livescore("CD Inca vs Municipal Limeno", "soccer",
                                        "2026-10-02") is None

    def test_deux_candidats_ne_reglent_rien(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Inca Aruba", "Municipal Limeno", 2, 1, "FT"),
                                         ("2", "CD Inca", "Municipal Limeno", 0, 3, "FT")]))
        assert ss.result_from_livescore("CD Inca vs Municipal Limeno", "soccer",
                                        "2026-10-02") is None


class TestTable:
    def test_aucun_aller_retour_ambigu(self):
        # Un nom à la fois clé et valeur ferait dépendre le résultat du sens
        # de lecture ; deux clés vers la même valeur aussi.
        assert set(_ALIAS_CLUBS) & set(_ALIAS_CLUBS.values()) == set()
        assert len(set(_ALIAS_CLUBS.values())) == len(_ALIAS_CLUBS)

    def test_tout_est_plie_en_minuscules_et_sans_espace_double(self):
        for nom in list(_ALIAS_CLUBS) + list(_ALIAS_CLUBS.values()):
            assert nom == nom.lower()
            assert nom == " ".join(nom.split())
            assert all(not unicodedata.combining(c)
                       for c in unicodedata.normalize("NFKD", nom)), nom

    def test_jamais_un_mot_nu(self):
        # Un alias d'un seul mot s'apparierait par containment à tout voisin
        # qui le porte (« Inca » ⊂ « Inca Garcilaso ») : deux mots au moins.
        for nom in list(_ALIAS_CLUBS) + list(_ALIAS_CLUBS.values()):
            assert len(nom.split()) >= 2, nom

    def test_un_alias_n_est_pas_un_exonyme(self):
        # Deux tables, deux mécanismes : un mot de ville traduit n'a rien à
        # faire dans la table des clubs.
        assert set(_ALIAS_CLUBS) & set(_EXONYMES_BIDIR) == set()
        assert set(_ALIAS_CLUBS.values()) & set(_EXONYMES_BIDIR) == set()
