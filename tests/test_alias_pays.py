"""
tests/test_alias_pays.py — « Czech Republic » et « Czechia » sont le même pays (2026-10-02).

Règle opérateur du 2026-10-02 : toujours vérifier les noms entre sources, y
compris leur orthographe anglaise et française. Mesuré le même jour sur les
journées réelles du 01 au 06/10 (trêve internationale) : les books écrivent
« Czech Republic », « USA », « United Arab Emirates » ; ESPN et LiveScore
écrivent « Czechia », ESPN « United States », LiveScore « UAE ». Aucun de ces
matchs ne se serait réglé.

Ce que ces tests tiennent : la table AJOUTE des écritures au nom servi par la
source, sur le nom ENTIER, sans assouplir le contrat — deux camps, candidat
unique, terminé.
"""
import unicodedata

import pytest

from core import score_sources as ss
from core.score_sources import (_ALIAS_PAYS, _apparie, _variantes, _variantes_pays,
                                diagnostic_noms)


@pytest.fixture(autouse=True)
def _caches_neufs():
    ss.reset_cache()
    yield
    ss.reset_cache()


class TestLesEcartsMesures:
    """Paires relevées sur les affiches réelles : nom du book ↔ nom de la source."""

    @pytest.mark.parametrize("book,source", [
        ("Czech Republic", "Czechia"),              # ESPN et LiveScore
        ("USA", "United States"),                   # ESPN
        ("United Arab Emirates", "UAE"),            # LiveScore
        ("Kyrgyzstan", "Kyrgyz Republic"),          # ESPN
        ("Turkey", "Türkiye"), ("Turkey", "Turkiye"),
        ("South Korea", "Republic of Korea"),
    ])
    def test_le_book_et_la_source_designent_le_meme_pays(self, book, source):
        assert _apparie(book, source)

    @pytest.mark.parametrize("book,source", [
        ("Czech Republic", "Czechia"), ("USA", "United States"),
        ("United Arab Emirates", "UAE"), ("Kyrgyzstan", "Kyrgyz Republic"),
    ])
    def test_sans_la_table_ces_noms_ne_passent_pas(self, monkeypatch, book, source):
        monkeypatch.setattr(ss, "_PAYS_GROUPE", {})
        assert not _apparie(book, source)


class TestLeFrancais:
    """Un titre de page lu par l'étage web peut être en français."""

    @pytest.mark.parametrize("anglais,francais", [
        ("Germany", "Allemagne"), ("England", "Angleterre"), ("Netherlands", "Pays-Bas"),
        ("Switzerland", "Suisse"), ("Norway", "Norvège"), ("Wales", "Pays de Galles"),
        ("Scotland", "Écosse"), ("Hungary", "Hongrie"), ("USA", "États-Unis"),
        ("Czech Republic", "République tchèque"), ("Ivory Coast", "Côte d'Ivoire"),
        ("Turkey", "Turquie"),
    ])
    def test_le_nom_francais_designe_le_meme_pays(self, anglais, francais):
        assert _apparie(anglais, francais)

    def test_un_titre_francais_regle(self):
        assert ss.score_du_titre("Allemagne 2-1 Écosse | L'Équipe", "Germany", "Scotland") == (2, 1)
        assert ss.score_du_titre("Pays-Bas - Suisse 0:0", "Netherlands", "Switzerland") == (0, 0)


class TestLeNomEntier:
    def test_un_club_qui_porte_le_mot_n_est_pas_le_pays(self):
        for nom in ("Czechia Praha", "Holland Park Hawks", "Deutschland Allemagne FC"):
            assert _variantes_pays(ss._fold(nom)) == [], nom
        assert not _apparie("Czech Republic", "Czechia Praha")

    def test_l_etage_est_reporte_jamais_devine(self):
        assert _apparie("Czech Republic U21", "Czechia U21")
        assert _apparie("England W", "Angleterre W")
        assert not _apparie("Czech Republic", "Czechia U21")
        assert not _apparie("Czech Republic U21", "Czechia")

    def test_un_nom_hors_table_se_comporte_comme_avant(self):
        assert _variantes("Belgium") == ["belgium"]
        assert _variantes("Hamburger SV") == ["hamburger sv"]


class TestContratDeReglement:
    """Les affiches réelles de la trêve : Spain–Czechia (03/10), UAE–Oman."""

    def _ls(self, events):
        return {"Stages": [{"Cnm": "UEFA Nations League", "Snm": "League A",
                            "Events": [{"Eid": e, "T1": [{"Nm": h}], "T2": [{"Nm": a}],
                                        "Tr1": hs, "Tr2": as_, "Eps": st}
                                       for (e, h, a, hs, as_, st) in events]}]}

    def test_le_match_se_regle_sous_le_nom_du_book(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None: self._ls(
            [("1", "Spain", "Czechia", 3, 1, "FT"), ("2", "Slovakia", "Slovenia", 0, 0, "FT")]))
        r = ss.result_from_livescore("Spain vs Czech Republic", "soccer", "2026-10-03")
        assert r == {"home_score": 3, "away_score": 1, "completed": True, "source": "livescore"}

    def test_un_seul_camp_ne_regle_toujours_pas(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None: self._ls(
            [("1", "England", "Czechia", 2, 0, "FT")]))
        assert ss.result_from_livescore("Spain vs Czech Republic", "soccer", "2026-10-03") is None


class TestDiagnosticDesNoms:
    """`ops.py noms` : combien de candidats à deux camps, et qui n'en a qu'un."""

    _LS = [{"home": ["Inca Aruba"], "away": ["Municipal Limeno"], "id": "1"},
           {"home": ["Mixco"], "away": ["CSD Municipal"], "id": "2"}]

    def test_un_candidat_unique(self):
        d = diagnostic_noms("CD Inca vs Municipal Limeno", None, self._LS)
        assert (d["espn"], d["livescore"]) == (0, 1)

    def test_un_nom_inconnu_est_nomme_par_ses_proches(self, monkeypatch):
        monkeypatch.setattr(ss, "_ALIAS_CLUBS_BIDIR", {})
        d = diagnostic_noms("CD Inca vs Municipal Limeno", None, self._LS)
        assert d["livescore"] == 0
        assert "livescore : Inca Aruba vs Municipal Limeno" in d["proches"]

    def test_espn_compte_les_deux_camps(self):
        ev = {"competitions": [{"id": "9", "competitors": [
            {"homeAway": "home", "team": {"displayName": "Spain"}},
            {"homeAway": "away", "team": {"displayName": "Czechia"}}]}]}
        assert diagnostic_noms("Spain vs Czech Republic", [ev], None)["espn"] == 1

    def test_un_libelle_illisible_ne_plante_pas(self):
        assert diagnostic_noms("Spain", [], []) == {"espn": 0, "livescore": 0, "proches": []}

    def test_la_commande_existe(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "scripts" / "ops.py").read_text(encoding="utf-8")
        assert 'elif cmd == "noms":' in src and "diagnostic_noms" in src
        assert "python scripts/ops.py noms" in src


class TestTable:
    def test_aucune_ecriture_dans_deux_pays(self):
        toutes = [e for groupe in _ALIAS_PAYS for e in groupe]
        assert len(toutes) == len(set(toutes))

    def test_tout_est_plie_sans_tiret_ni_espace_double(self):
        for groupe in _ALIAS_PAYS:
            assert len(groupe) >= 2
            for e in groupe:
                assert e == e.lower() == " ".join(e.split()) and "-" not in e, e
                assert all(not unicodedata.combining(c)
                           for c in unicodedata.normalize("NFKD", e)), e
