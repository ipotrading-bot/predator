"""
tests/test_exonymes.py — « Köln » et « Cologne » sont la même ville (2026-09-19).

Hamburger SV vs 1. FC Köln (Bundesliga, 19/09) est resté ACTIF : ESPN
publiait « FC Cologne at Hamburg SV » — terminé, score 2-1 — et LiveScore
« FC Cologne ». Les sources de scores traduisent le nom de la ville, la
source de cotes ne le fait pas : aucun rapprochement textuel ne pouvait
franchir l'écart. L'audit s'est déclaré stérile et la purge a été repoussée.

Ce que ces tests tiennent : la table d'exonymes AJOUTE une variante du nom
servi par la source, elle n'assouplit AUCUNE garde. Le pont d'alias APPRIS,
lui, reste interdit (INCIDENTS.md « Le pont d'alias ») — celui-ci est fixe,
vérifiable mot à mot, sans apprentissage ni budget.
"""
from core.score_sources import _EXONYMES, _apparie, _meme_equipe, _variantes


class TestVariantes:
    def test_les_deux_sens(self):
        assert "1. fc koln" in _variantes("1. FC Cologne")
        assert "1. fc cologne" in _variantes("1. FC Köln")

    def test_le_nom_dorigine_est_toujours_rendu(self):
        assert _variantes("FC Cologne")[0] == "fc cologne"
        assert _variantes("Hamburger SV") == ["hamburger sv"]

    def test_le_mot_est_remplace_ENTIER(self):
        # « Colognes », « Romania », « Milanese » ne sont pas des exonymes :
        # une sous-chaîne traduite rapprocherait des clubs sans rapport.
        for nom in ("Colognes United", "Romania FC", "Milanese SC"):
            assert _variantes(nom) == [nom.lower()], nom

    def test_les_diacritiques_sont_deja_plies(self):
        assert all("ö" not in v for v in _variantes("1. FC Köln"))


class TestAppariement:
    def test_le_cas_du_19_09(self):
        assert _apparie("1. FC Köln", "FC Cologne")
        assert _apparie("Hamburger SV", "Hamburg SV")

    def test_deux_clubs_de_la_meme_ville_restent_refuses(self):
        # La traduction ne doit JAMAIS rapprocher des voisins : c'est le
        # faux règlement du 2026-09-13 (Real Sociedad réglé sur Real Oviedo).
        for voisin in ("Viktoria Cologne", "Fortuna Cologne", "Viktoria Köln"):
            assert not _apparie("1. FC Köln", voisin), voisin
        assert not _apparie("Bayern Munich", "1860 Munich")
        assert not _apparie("Bayern Munich", "TSV 1860 München")

    def test_un_nom_sans_exonyme_se_comporte_comme_avant(self):
        assert _apparie("Borussia Dortmund", "Borussia Dortmund")
        assert not _apparie("Borussia Dortmund", "Borussia Monchengladbach")

    def test_espn_apparie_par_ses_libelles(self):
        competitor = {"team": {"displayName": "FC Cologne", "shortDisplayName": "Cologne",
                               "name": "Cologne", "location": "FC Cologne"}}
        assert _meme_equipe("1. FC Köln", competitor)
        # Un club dont le nom PLEIN diffère n'est pas rapproché par la ville.
        assert not _meme_equipe("Hamburger SV", competitor)


class TestContratDeReglement:
    """Ce qui protège du faux règlement n'est PAS la finesse du nom : c'est le
    contrat des voies de score — les DEUX camps, sur un candidat UNIQUE, et
    terminé. Les exonymes ne le touchent pas. (Un reste-sigle comme « FC »
    laisse `strict_team_match` rapprocher « Viktoria Köln » de « FC Köln »
    depuis toujours, exonyme ou pas : c'est ce contrat qui rattrape.)"""

    def _ev(self, home: str, away: str, hs: int, as_: int, ident: str):
        return {"id": ident, "status": {"type": {"completed": True, "state": "post"}},
                "date": "2026-09-19T13:30Z",
                "competitions": [{"id": ident, "status": {"type": {"completed": True, "state": "post"}},
                                  "competitors": [
                                      {"homeAway": "home", "score": str(hs), "team": {"displayName": home}},
                                      {"homeAway": "away", "score": str(as_), "team": {"displayName": away}}]}]}

    def test_le_match_du_19_09_est_regle_par_exonyme(self, monkeypatch):
        import core.score_sources as ss

        monkeypatch.setattr(ss, "_espn_events",
                            lambda _p, _f: [self._ev("Hamburg SV", "FC Cologne", 2, 1, "A")])
        out = ss.result_from_espn("Hamburger SV vs 1. FC Köln", "soccer", "2026-09-19")
        assert out == {"home_score": 2, "away_score": 1, "completed": True, "source": "espn"}

    def test_deux_candidats_ne_reglent_rien(self, monkeypatch):
        import core.score_sources as ss

        monkeypatch.setattr(ss, "_espn_events", lambda _p, _f: [
            self._ev("Hamburg SV", "FC Cologne", 2, 1, "A"),
            self._ev("Hamburger SV", "1. FC Köln", 0, 3, "B")])
        assert ss.result_from_espn("Hamburger SV vs 1. FC Köln", "soccer", "2026-09-19") is None


class TestTable:
    def test_aucun_doublon_ni_aller_retour_ambigu(self):
        # Une ville traduite deux fois (a→b et b→c) ferait varier le résultat
        # selon l'ordre de lecture.
        assert set(_EXONYMES) & set(_EXONYMES.values()) == set()

    def test_tout_est_plie_et_en_minuscules(self):
        import unicodedata

        for mot in list(_EXONYMES) + list(_EXONYMES.values()):
            assert mot == mot.lower()
            assert all(not unicodedata.combining(c)
                       for c in unicodedata.normalize("NFKD", mot)), mot
