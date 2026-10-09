"""Gardien — NBA en saison régulière seulement (décision opérateur, 2026-10-09).

odds-api.io étiquette la présaison « USA - NBA Preseason » : le libellé
suffit. Sans ce mot, le type de saison vient du scoreboard ESPN
(`event.season.type`, 1 = présaison), comme pour la NHL
(tests/test_nhl_saison_reguliere.py). Saison inconnue → refus.
"""
import logging

import run_engine as eng

_DAL = "Dallas Mavericks vs Houston Rockets"


def _ev(home, away, saison):
    comps = [{"homeAway": "home", "team": {"displayName": home}},
             {"homeAway": "away", "team": {"displayName": away}}]
    ev = {"competitions": [{"competitors": comps,
                            "status": {"type": {"state": "pre", "completed": False}}}]}
    if saison is not None:
        ev["season"] = {"year": 2027, "type": saison}
    return ev


def _m(match=_DAL, league="NBA", sport="basketball", **extra):
    home, away = match.split(" vs ")
    d = {"match": match, "home": home, "away": away, "sport": sport, "league": league,
         "commence_time": "2026-10-09T12:00:00Z"}
    d.update(extra)
    return d


class TestGardeNba:
    def _fx(self, saison):
        return {"basketball": [_ev("Dallas Mavericks", "Houston Rockets", saison)]}

    def test_le_libelle_preseason_suffit(self):
        """Le libellé réel d'odds-api.io, relevé en base le 2026-10-09 : il
        écarte même quand ESPN est muet ou se trompe de saison."""
        m = _m(league="USA - NBA Preseason")
        assert eng._nba_hors_saison(m, {}) == "présaison NBA"
        assert eng._nba_hors_saison(m, self._fx(2)) == "présaison NBA"
        assert eng._nba_hors_saison(_m(league="NBA Pre-Season"), {}) == "présaison NBA"

    def test_presaison_espn_ecartee(self):
        assert eng._nba_hors_saison(_m(), self._fx(1)) == "présaison NBA"

    def test_saison_reguliere_et_series_passent(self):
        assert eng._nba_hors_saison(_m(), self._fx(2)) is None
        assert eng._nba_hors_saison(_m(league="USA - NBA"), self._fx(3)) is None

    def test_saison_inconnue_ecartee(self):
        assert eng._nba_hors_saison(_m(), {"basketball": []}) == "saison NBA non confirmée par ESPN"
        assert eng._nba_hors_saison(_m(), {}) == "saison NBA non confirmée par ESPN"

    def test_les_autres_ligues_de_basket_ne_sont_pas_touchees(self):
        assert eng._nba_hors_saison(_m(league="WNBA"), {}) is None
        assert eng._nba_hors_saison(_m(league="Spain - Liga ACB"), {}) is None
        euro = _m(league="Basketball Euroleague", sport="euroleague_basketball")
        assert eng._nba_hors_saison(euro, {}) is None

    def test_la_nhl_garde_sa_propre_garde(self):
        assert eng._nba_hors_saison(_m(league="NHL", sport="hockey"), {}) is None


class TestDansLePerimetre:
    def test_le_match_de_presaison_est_ecarte_et_logge(self, monkeypatch, caplog):
        monkeypatch.setattr(eng, "_fixtures_espn",
                            lambda sport, a, b: [_ev("Dallas Mavericks", "Houston Rockets", 1)])
        with caplog.at_level(logging.INFO, logger="PREDATOR"):
            gardes = eng._filtrer_perimetre([_m(league="USA - NBA Preseason")],
                                            logging.getLogger("PREDATOR"))
        assert gardes == []
        assert ("HORS PÉRIMÈTRE | Dallas Mavericks vs Houston Rockets "
                "(USA - NBA Preseason) — présaison NBA") in caplog.text

    def test_le_match_de_saison_reguliere_passe(self, monkeypatch):
        monkeypatch.setattr(eng, "_fixtures_espn",
                            lambda sport, a, b: [_ev("Dallas Mavericks", "Houston Rockets", 2)])
        assert eng._filtrer_perimetre([_m()], logging.getLogger("PREDATOR")) == [_m()]
