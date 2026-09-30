"""Gardien — NHL en saison régulière seulement (décision opérateur, 2026-09-30).

OddsAPI sert présaison et saison sous la même clé `icehockey_nhl`, et
odds-api.io sous le même libellé « USA - NHL » : le type de saison vient du
scoreboard ESPN (`event.season.type`, 1 = présaison), déjà lu par le
périmètre. Saison inconnue → refus. Les séries passent.
"""
import logging

import run_engine as eng
from core import score_sources as ss

_TOR = "Toronto Maple Leafs vs Montreal Canadiens"


def _ev(home, away, saison):
    comps = [{"homeAway": "home", "team": {"displayName": home}},
             {"homeAway": "away", "team": {"displayName": away}}]
    ev = {"competitions": [{"competitors": comps,
                            "status": {"type": {"state": "pre", "completed": False}}}]}
    if saison is not None:
        ev["season"] = {"year": 2027, "type": saison}
    return ev


def _m(match=_TOR, league="NHL", sport="hockey", **extra):
    home, away = match.split(" vs ")
    d = {"match": match, "home": home, "away": away, "sport": sport, "league": league,
         "commence_time": "2026-09-29T23:00:00Z"}
    d.update(extra)
    return d


class TestSaisonEspn:
    def test_le_type_de_saison_du_match_apparie(self):
        evs = [_ev("Boston Bruins", "New York Rangers", 1),
               _ev("Toronto Maple Leafs", "Montreal Canadiens", 2)]
        assert ss.saison_espn(_TOR, evs) == 2

    def test_les_deux_noms_sont_exiges(self):
        """Un seul nom prêterait au match la saison d'un autre."""
        evs = [_ev("Toronto Maple Leafs", "Ottawa Senators", 1)]
        assert ss.saison_espn(_TOR, evs) is None

    def test_accent_replie(self):
        evs = [_ev("Toronto Maple Leafs", "Montreal Canadiens", 1)]
        assert ss.saison_espn("Toronto Maple Leafs vs Montréal Canadiens", evs) == 1

    def test_champ_absent(self):
        assert ss.saison_espn(_TOR, [_ev("Toronto Maple Leafs", "Montreal Canadiens", None)]) is None


class TestGardeNhl:
    def _fx(self, saison):
        return {"hockey": [_ev("Toronto Maple Leafs", "Montreal Canadiens", saison)]}

    def test_presaison_ecartee(self):
        assert eng._nhl_hors_saison(_m(), self._fx(1)) == "présaison NHL"

    def test_saison_reguliere_et_series_passent(self):
        assert eng._nhl_hors_saison(_m(), self._fx(2)) is None
        assert eng._nhl_hors_saison(_m(), self._fx(3)) is None

    def test_saison_inconnue_ecartee(self):
        assert eng._nhl_hors_saison(_m(), {"hockey": []}) == "saison NHL non confirmée par ESPN"
        assert eng._nhl_hors_saison(_m(), {}) == "saison NHL non confirmée par ESPN"

    def test_libelle_odds_api_io(self):
        assert eng._nhl_hors_saison(_m(league="USA - NHL"), self._fx(1)) == "présaison NHL"

    def test_les_autres_ligues_de_hockey_ne_sont_pas_touchees(self):
        m = _m(match="Mora IK vs Nybro Vikings IF", league="Sweden - Hockey Allsvenskan")
        assert eng._nhl_hors_saison(m, {"hockey": []}) is None

    def test_un_mot_contenant_nhl_ne_suffit_pas(self):
        assert eng._nhl_hors_saison(_m(league="KHL"), {"hockey": []}) is None
        assert eng._nhl_hors_saison(_m(league="Enhlanded Cup"), {"hockey": []}) is None


class TestDansLePerimetre:
    def test_le_match_de_presaison_est_ecarte_et_logge(self, monkeypatch, caplog):
        monkeypatch.setattr(eng, "_fixtures_espn",
                            lambda sport, a, b: [_ev("Toronto Maple Leafs", "Montreal Canadiens", 1)])
        with caplog.at_level(logging.INFO, logger="PREDATOR"):
            gardes = eng._filtrer_perimetre([_m()], logging.getLogger("PREDATOR"))
        assert gardes == []
        assert "HORS PÉRIMÈTRE | Toronto Maple Leafs vs Montreal Canadiens (NHL) — présaison NHL" in caplog.text
        assert "PÉRIMÈTRE | 1 matchs → 0 marchés vivants → 0 réglables" in caplog.text

    def test_le_match_de_saison_reguliere_passe(self, monkeypatch):
        monkeypatch.setattr(eng, "_fixtures_espn",
                            lambda sport, a, b: [_ev("Toronto Maple Leafs", "Montreal Canadiens", 2)])
        assert eng._filtrer_perimetre([_m()], logging.getLogger("PREDATOR")) == [_m()]
