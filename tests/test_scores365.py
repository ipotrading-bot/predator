"""Gardien — l'étage 365scores, qui remplace la recherche web Ollama.

Le 2026-11-13 l'API de recherche d'Ollama est devenue payante (HTTP 402) ;
décision opérateur : la retirer, la remplacer par une source gratuite. Voir
`core.score_sources` « 2bis. 365scores » et INCIDENTS.md.

Charges au FORMAT RÉEL de `webws.365scores.com/web/games/allscores/` :
scores en flottants, −1 sans score, statut en clair (`statusText`). Aucun
appel réseau : `_get_json` est remplacé.
"""
import inspect

import pytest

from core import audit_engine, score_sources as ss, settlement


@pytest.fixture(autouse=True)
def _cache_vide():
    ss.reset_cache()
    yield
    ss.reset_cache()


def _match(dom, ext, hs, as_, statut="Ended", ident=1):
    return {"id": ident, "statusText": statut, "statusGroup": 4,
            "homeCompetitor": {"name": dom, "score": float(hs)},
            "awayCompetitor": {"name": ext, "score": float(as_)}}


def _servir(monkeypatch, par_jour: dict) -> list:
    """`par_jour` : {"JJ/MM/AAAA": [matchs]} ; rend la liste des URL lues."""
    lues = []

    def faux(url, bucket, budget, source=None):
        lues.append(url)
        assert bucket == ss._S365_BUCKET and budget == ss.SCORES365_DAILY_BUDGET
        for jour, matchs in par_jour.items():
            if f"startDate={jour.replace('/', '%2F')}" in url:
                return {"games": matchs}
        return {"games": []}
    monkeypatch.setattr(ss, "_get_json", faux)
    return lues


JOUR = "2026-09-07"
CLE = "07/09/2026"


class TestRegistre:
    def test_le_football_seul(self):
        """Règle 13 : un sport n'entre ici qu'avec sa mesure, et l'étage ne
        sort qu'avec son critère relevé (revue du 2026-12-13)."""
        assert ss._S365_SPORTS == {"soccer": 1}

    def test_le_budget_et_le_critere_de_retrait_sont_ecrits(self):
        src = inspect.getsource(ss)
        bloc = src[src.index("2bis. 365scores"):src.index("_S365_URL =")]
        assert "BUDGET CHIFFRÉ" in bloc and "CRITÈRE DE RETRAIT DATÉ" in bloc
        assert "2026-12-13" in bloc
        assert ss.SCORES365_DAILY_BUDGET >= 168         # borne haute chiffrée

    def test_la_recherche_web_ollama_n_existe_plus(self):
        """Devenue payante le 2026-11-13 ; ne pas la rebrancher sans décision."""
        for module in (ss, settlement, audit_engine):
            src = inspect.getsource(module)
            assert "api/web_search" not in src and "web_ok" not in src
        for nom in ("result_from_web", "WEB_SEARCH_URL", "score_du_titre"):
            assert not hasattr(ss, nom)

    def test_aucune_ia_ne_lit_le_score(self):
        src = inspect.getsource(ss)
        assert "ai_router" not in src and "ai_search" not in src


class TestReglement:
    def test_un_match_termine_regle(self, monkeypatch):
        """Ligne réelle restée sans score depuis le 2026-09-07."""
        _servir(monkeypatch, {CLE: [_match("Everton De Vina", "Universidad Catolica", 0, 1)]})
        r = ss.result_from_365scores(
            "CD Everton Vina del Mar vs CD Universidad Catolica", "soccer", JOUR)
        assert r == {"home_score": 0, "away_score": 1, "completed": True,
                     "source": "365scores"}

    @pytest.mark.parametrize("statut", ["After ET", "After Penalties", "Awarded",
                                        "Final Result Only", "Postponed", "Suspended",
                                        "Abandoned", "Cancelled", "2nd Half", ""])
    def test_seul_le_temps_reglementaire_termine_regle(self, monkeypatch, statut):
        """Fleetwood–Rochdale (07/11) : « After ET » 1-2, 1-1 à 90 minutes."""
        _servir(monkeypatch, {CLE: [_match("Fleetwood Town", "Rochdale", 1, 2, statut)]})
        assert ss.result_from_365scores("Fleetwood Town vs Rochdale", "soccer", JOUR) is None

    def test_un_match_sans_score_ne_regle_pas(self, monkeypatch):
        _servir(monkeypatch, {CLE: [_match("Mount Pleasant", "Portmore United", -1, -1)]})
        assert ss.result_from_365scores(
            "Mount Pleasant FC vs Portmore United", "soccer", JOUR) is None

    def test_une_affiche_rangee_a_l_envers_n_est_pas_reconnue(self, monkeypatch):
        """Mesuré le 07/11 : « Apollon Kalamaria vs Panserraikos » ici,
        « Panserraikos FC vs Kalamaria » chez LiveScore."""
        _servir(monkeypatch, {CLE: [_match("Apollon Kalamaria", "Panserraikos", 1, 2)]})
        assert ss.result_from_365scores("Panserraikos FC vs Kalamaria", "soccer", JOUR) is None

    def test_deux_candidats_ne_reglent_pas(self, monkeypatch):
        _servir(monkeypatch, {CLE: [_match("Rosario Central", "Gimnasia La Plata", 1, 2, ident=1)],
                              "08/09/2026": [_match("Rosario Central", "Gimnasia La Plata", 0, 0, ident=2)]})
        assert ss.result_from_365scores(
            "Rosario Central vs Gimnasia La Plata", "soccer", JOUR) is None

    def test_un_seul_camp_ne_regle_pas(self, monkeypatch):
        _servir(monkeypatch, {CLE: [_match("Rosario Central", "Boca Juniors", 1, 2)]})
        assert ss.result_from_365scores(
            "Rosario Central vs Gimnasia La Plata", "soccer", JOUR) is None

    def test_hors_football_aucune_requete(self, monkeypatch):
        lues = _servir(monkeypatch, {})
        assert ss.result_from_365scores("Lakers vs Celtics", "basketball", JOUR) is None
        assert lues == []

    def test_une_panne_rend_none_sans_lever(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda *a, **k: None)
        assert ss.result_from_365scores("Rosario Central vs Boca Juniors", "soccer", JOUR) is None

    def test_une_journee_n_est_lue_qu_une_fois_par_run(self, monkeypatch):
        lues = _servir(monkeypatch, {CLE: [_match("Juventus", "Parma", 2, 0)]})
        ss.result_from_365scores("Juventus Turin vs Parma Calcio", "soccer", JOUR)
        ss.result_from_365scores("Rosario Central vs Boca Juniors", "soccer", JOUR)
        assert len(lues) == 3                           # jour, lendemain, veille — une fois


class TestPlaceDansLaChaine:
    def _chaine(self, monkeypatch, espn=None, livescore=None, s365=None, tsdb=None):
        ordre = []

        def etage(nom, rendu):
            return lambda *a, **k: ordre.append(nom) or rendu
        monkeypatch.setattr(ss, "result_from_espn", etage("espn", espn))
        monkeypatch.setattr(ss, "result_from_livescore", etage("livescore", livescore))
        monkeypatch.setattr(ss, "result_from_365scores", etage("365scores", s365))
        monkeypatch.setattr(ss, "result_from_thesportsdb", etage("tsdb", tsdb))
        return ordre

    def test_apres_livescore_avant_thesportsdb(self, monkeypatch):
        ordre = self._chaine(monkeypatch)
        assert ss.fetch_score("A Club vs B Club", "soccer", JOUR) is None
        assert ordre == ["espn", "livescore", "365scores", "tsdb"]

    def test_il_ne_parle_que_si_livescore_se_tait(self, monkeypatch):
        """Montañeses–Los Cabos (08/11) : 2-0 chez LiveScore, 2-1 ici. Un
        désaccord ne peut pas changer une issue que LiveScore sait écrire."""
        ls = {"home_score": 2, "away_score": 0, "completed": True, "source": "livescore"}
        ici = {"home_score": 2, "away_score": 1, "completed": True, "source": "365scores"}
        ordre = self._chaine(monkeypatch, livescore=ls, s365=ici)
        assert ss.fetch_score("Montaneses vs Los Cabos United", "soccer", JOUR) == ls
        assert "365scores" not in ordre

    def test_il_epargne_le_budget_thesportsdb(self, monkeypatch):
        ici = {"home_score": 0, "away_score": 1, "completed": True, "source": "365scores"}
        ordre = self._chaine(monkeypatch, s365=ici)
        assert ss.fetch_score("A Club vs B Club", "soccer", JOUR) == ici
        assert "tsdb" not in ordre

    def test_il_sert_aussi_sans_le_repli_thesportsdb(self, monkeypatch):
        """Fantômes et lignes relancées : là qu'étaient 22 des 32 lignes."""
        ici = {"home_score": 0, "away_score": 1, "completed": True, "source": "365scores"}
        self._chaine(monkeypatch, s365=ici)
        assert ss.fetch_score("A Club vs B Club", "soccer", JOUR, tsdb_ok=False) == ici
