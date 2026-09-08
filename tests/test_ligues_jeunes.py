"""
tests/test_ligues_jeunes.py — un match de jeunes ne se règle JAMAIS sur le
score du club senior (2026-09-08).

Le cas mesuré : odds-api.io a émis « Borussia Dortmund vs Villarreal CF » en
« International Youth - UEFA Youth League » (U19, 14:00 UTC, perdu 2-3). Le
même soir les seniors jouaient en Ligue des champions (3-2, sur ESPN). La
garde d'étage de `strict_team_match` ne lit que les NOMS ; quand la source
les livre sans marqueur, seule la LIGUE sait. Trois gardes :
  - `section_jeunes` / `nom_avec_etage` : la ligue qualifie les noms ;
  - périmètre : une ligue de jeunes est refusée AVANT la garde ESPN, que
    l'homonyme senior y figure ou non (décision opérateur 2026-09-08) ;
  - règlement : le nom cherché est le nom qualifié, donc ESPN (seniors) ne
    règle plus et LiveScore (« … U19 ») règle sur le bon match.
Aucun réseau : `_get_json` est monkeypatché.
"""
import logging

import pytest

import run_engine as eng
from core import score_sources as ss
from core import settlement
from core.paim_engine import section_jeunes, nom_avec_etage

LIGUE = "International Youth - UEFA Youth League"
NOM = "Borussia Dortmund vs Villarreal CF"


@pytest.fixture(autouse=True)
def _caches_neufs():
    ss.reset_cache()
    yield
    ss.reset_cache()


class TestLaLigueQualifieLesNoms:
    def test_youth_league_vaut_u19(self):
        assert section_jeunes(LIGUE) == "u19"

    def test_la_categorie_chiffree_prime(self):
        assert section_jeunes("England Amateur - U21 Professional Development League") == "u21"
        assert section_jeunes("Argentina - Reserve League U20") == "u20"

    def test_une_ligue_senior_ne_porte_rien(self):
        for ligue in ("Germany - Bundesliga", "UEFA - Champions League",
                      "Colombia - Primera A", "USA - National Women's Soccer League"):
            assert section_jeunes(ligue) == ""

    def test_les_deux_camps_recoivent_letage(self):
        assert nom_avec_etage(NOM, LIGUE) == "Borussia Dortmund U19 vs Villarreal CF U19"

    def test_idempotent_et_neutre_en_senior(self):
        deja = "Borussia Dortmund U19 vs Villarreal U19"
        assert nom_avec_etage(deja, LIGUE) == deja
        assert nom_avec_etage(NOM, "UEFA - Champions League") == NOM
        assert nom_avec_etage("", LIGUE) == ""

    def test_un_camp_deja_qualifie_ne_lest_pas_deux_fois(self):
        assert nom_avec_etage("Lyon U19 vs Villarreal CF", LIGUE) == "Lyon U19 vs Villarreal CF U19"


def _m(league=LIGUE, match=NOM):
    return {"match": match, "home": match.split(" vs ")[0], "away": match.split(" vs ")[1],
            "sport": "soccer", "league": league, "commence_time": "2026-09-08T14:00:00Z",
            "odds_pinnacle": {"1": 1.43, "X": 4.0, "2": 6.0},
            "_soft_source": "odds-api.io", "_exchange": "matchbook"}


def _espn(home, away, hs, as_, eid="e1"):
    return {"id": eid, "date": "2026-09-08T19:00Z", "competitions": [{
        "id": eid, "status": {"type": {"completed": True, "state": "post"}},
        "competitors": [{"homeAway": "home", "score": str(hs), "team": {"displayName": home}},
                        {"homeAway": "away", "score": str(as_), "team": {"displayName": away}}]}]}


class TestLePerimetreRefuseLesJeunes:
    def test_refuse_meme_quand_espn_liste_lhomonyme_senior(self):
        fx = {"soccer": [_espn("Borussia Dortmund", "Villarreal", 3, 2)]}
        assert eng._reglable(_m(), fx) is False
        # Le même match en ligue senior passe : c'est bien la LIGUE qui refuse.
        assert eng._reglable(_m(league="UEFA - Champions League"), fx) is True

    def test_le_refus_est_logge_avec_sa_raison(self, monkeypatch, caplog):
        monkeypatch.setattr(eng, "_fixtures_espn",
                            lambda sport, a, b: [_espn("Borussia Dortmund", "Villarreal", 3, 2)])
        with caplog.at_level(logging.INFO, logger="PREDATOR"):
            assert eng._filtrer_perimetre([_m()], logging.getLogger("PREDATOR")) == []
        assert any("NON RÉGLABLE" in r.message and "ligue de jeunes" in r.message
                   for r in caplog.records)


def _sources(monkeypatch):
    """ESPN : seniors 3-2 (Ligue des champions). LiveScore : U19 2-3."""
    def fake(url, bucket, budget, source=None):
        if source == "espn":
            return {"events": [_espn("Borussia Dortmund", "Villarreal", 3, 2)]}
        if source == "livescore":
            return {"Stages": [{"Cnm": "UEFA Youth League", "Snm": "Champions League Path",
                                "Events": [{"Eid": "1886555",
                                            "T1": [{"Nm": "Borussia Dortmund U19"}],
                                            "T2": [{"Nm": "Villarreal U19"}],
                                            "Tr1": 2, "Tr2": 3, "Eps": "FT"}]}]}
        return None
    monkeypatch.setattr(ss, "_get_json", fake)


class TestLeReglementChercheLeBonMatch:
    def test_le_nom_nu_reglait_sur_les_seniors(self, monkeypatch):
        """Le piège, tel qu'il a mordu le 2026-09-08 : documenté, pas souhaité."""
        _sources(monkeypatch)
        assert ss.fetch_score(NOM, "soccer", "2026-09-08", tsdb_ok=False)["home_score"] == 3

    def test_le_nom_qualifie_regle_sur_les_u19(self, monkeypatch):
        _sources(monkeypatch)
        r = ss.fetch_score(nom_avec_etage(NOM, LIGUE), "soccer", "2026-09-08", tsdb_ok=False)
        assert (r["home_score"], r["away_score"], r["source"]) == (2, 3, "livescore")

    def test_settle_signal_regle_le_pari_des_jeunes_en_loss(self, monkeypatch):
        _sources(monkeypatch)
        patches = {}
        monkeypatch.setattr(settlement, "update_signal_fields",
                            lambda sb, sid, patch, optional_cols=None: patches.update(patch) or True)
        monkeypatch.setattr(settlement, "log_to_ledger", lambda *a, **k: None)
        sig = {"id": 10064, "match": NOM, "league": LIGUE, "sport": "soccer",
               "market_key": "h2h", "selection_name": "Borussia Dortmund",
               "match_time": "2026-09-08T14:00:00+00:00", "xbet_odd": 1.54,
               "pinnacle_price": 1.43}
        assert settlement.settle_signal(object(), sig, "2026-09-08T21:24:00Z", tsdb_ok=False)
        assert patches["outcome"] == "LOSS"

    def test_sans_ligue_de_jeunes_rien_ne_change(self, monkeypatch):
        _sources(monkeypatch)
        patches = {}
        monkeypatch.setattr(settlement, "update_signal_fields",
                            lambda sb, sid, patch, optional_cols=None: patches.update(patch) or True)
        monkeypatch.setattr(settlement, "log_to_ledger", lambda *a, **k: None)
        sig = {"id": 1, "match": NOM, "league": "UEFA - Champions League", "sport": "soccer",
               "market_key": "h2h", "selection_name": "Borussia Dortmund",
               "match_time": "2026-09-08T19:00:00+00:00", "xbet_odd": 1.54,
               "pinnacle_price": 1.43}
        assert settlement.settle_signal(object(), sig, "2026-09-08T21:24:00Z", tsdb_ok=False)
        assert patches["outcome"] == "WIN"
