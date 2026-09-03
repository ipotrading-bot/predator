"""
tests/test_perimetre.py — périmètre d'émission (2026-09-03, décision opérateur).

« Ligues sans sources fiables, artefacts et marchés morts à bannir ; garder
prix sharps réels avec liquidité et ligues réglables avec nos outils. »
Deux gardes DÉRIVÉES, aucune liste de ligues à la main :
  - MARCHÉ VIVANT : un match du Tier 2 n'entre que si un exchange confirme
    son prix sharp (`odds_exchange` ou `_exchange`) — la liquidité mesurée
    par core/matchbook.py ; le Tier 1 OddsAPI passe (Pinnacle réel) ;
  - RÉGLABLE : api-sports (ses propres fixtures), MLB statsapi (baseball), ou
    ESPN qui LISTE le match (à venir compris) ; sinon refus, ESPN muet
    compris (panne = refus, le tick suivant réessaie).
Chaque refus est loggé (tests/test_tier2_toujours.py).
"""
import logging

import run_engine as eng
from core import score_sources as ss


def _m(match="A FC vs B FC", sport="soccer", soft="odds-api.io", **extra):
    d = {"match": match, "home": match.split(" vs ")[0], "away": match.split(" vs ")[1],
         "sport": sport, "league": "L", "commence_time": "2026-09-04T18:00:00Z",
         "odds_pinnacle": {"1": 2.0, "X": 3.4, "2": 3.6}}
    if soft:
        d["_soft_source"] = soft
    d.update(extra)
    return d


def _ev(home, away, home_away=True):
    if home_away:
        comps = [{"homeAway": "home", "team": {"displayName": home}},
                 {"homeAway": "away", "team": {"displayName": away}}]
    else:
        comps = [{"athlete": {"displayName": home}}, {"athlete": {"displayName": away}}]
    return {"competitions": [{"competitors": comps, "status": {"type": {"state": "pre", "completed": False}}}]}


class TestMarcheVivant:
    def test_tier_1_passe(self):
        assert eng._marche_vivant(_m(soft=None)) is True

    def test_copie_pinnacle_sans_exchange_est_un_marche_mort(self):
        assert eng._marche_vivant(_m(soft="api-sports/soccer")) is False
        assert eng._marche_vivant(_m(soft="titan007")) is False

    def test_exchange_confirme_ou_bouche_trou_passe(self):
        assert eng._marche_vivant(_m(odds_exchange={"1": 2.0, "X": 3.4, "2": 3.6})) is True
        assert eng._marche_vivant(_m(_exchange="matchbook")) is True


class TestReglable:
    def test_api_sports_et_baseball_sont_reglables_par_construction(self):
        assert eng._reglable(_m(soft="api-sports/soccer"), {}) is True
        assert eng._reglable(_m(sport="baseball", soft="titan007"), {}) is True

    def test_espn_doit_lister_le_match(self):
        fx = {"soccer": [_ev("A FC", "B FC")]}
        assert eng._reglable(_m(), fx) is True
        assert eng._reglable(_m(match="C FC vs D FC"), fx) is False

    def test_espn_muet_ou_sport_sans_source_refuse(self):
        assert eng._reglable(_m(), {"soccer": []}) is False        # panne ESPN
        assert eng._reglable(_m(sport="tennis"), {"tennis": None}) is False
        assert eng._reglable(_m(sport="boxing"), {}) is False

    def test_mma_sapparie_sur_les_athletes_dans_les_deux_ordres(self):
        fx = {"mma": [_ev("Cam Nelson", "Ding Meng", home_away=False)]}
        assert eng._reglable(_m(match="Ding Meng vs Cam Nelson", sport="mma"), fx) is True


class TestFiltre:
    def test_une_requete_espn_par_sport_sur_la_fenetre_du_run(self, monkeypatch, caplog):
        appels = []
        def fake(sport, a, b):
            appels.append((sport, a, b)); return [_ev("A FC", "B FC")]
        monkeypatch.setattr(eng, "_fixtures_espn", fake)
        matches = [_m(_exchange="matchbook", commence_time="2026-09-04T18:00:00Z"),
                   _m(match="C FC vs D FC", _exchange="matchbook", commence_time="2026-09-05T10:00:00Z"),
                   _m(match="E FC vs F FC", soft="titan007"),                 # marché mort
                   _m(match="G vs H", sport="tennis", soft=None)]            # tier 1 mais irréglable
        with caplog.at_level(logging.INFO, logger="PREDATOR"):
            gardes = eng._filtrer_perimetre(matches, logging.getLogger("PREDATOR"))
        assert [g["match"] for g in gardes] == ["A FC vs B FC"]
        assert appels == [("soccer", "2026-09-04", "2026-09-05")] or ("tennis", "2026-09-04", "2026-09-04") in appels
        texte = caplog.text
        assert "MARCHÉ MORT | E FC vs F FC" in texte
        assert "NON RÉGLABLE | C FC vs D FC" in texte and "NON RÉGLABLE | G vs H" in texte
        assert "PÉRIMÈTRE | 4 matchs → 3 marchés vivants → 1 réglables" in texte

    def test_une_panne_espn_ne_fait_pas_tomber_le_run(self, monkeypatch, caplog):
        def boom(sport, a, b):
            raise RuntimeError("réseau")
        monkeypatch.setattr(eng, "_fixtures_espn", boom)
        with caplog.at_level(logging.INFO, logger="PREDATOR"):
            assert eng._filtrer_perimetre([_m(_exchange="matchbook")], logging.getLogger("PREDATOR")) == []
        assert "ESPN muet" in caplog.text

    def test_le_filtre_precede_la_photographie_du_slate(self):
        import inspect
        src = inspect.getsource(eng.run)
        assert src.index("_filtrer_perimetre(matches, log)") < src.index('_set_cached(sb, "cache_soft_slate"')


class TestESPNFixtures:
    def test_fixtures_espn_une_fenetre_par_chemin(self, monkeypatch):
        appels = []
        def fake(url, bucket, budget, source=None):
            appels.append(url); return {"events": [_ev("A FC", "B FC")]}
        monkeypatch.setattr(ss, "_get_json", fake)
        ss.reset_cache()
        evs = ss.fixtures_espn("soccer", "2026-09-04", "2026-09-05")
        assert len(evs) == 1 and appels[0].endswith("dates=20260903-20260906&limit=1000")
        assert ss.fixtures_espn("tennis", "2026-09-04", "2026-09-05") is None
        assert ss.fixture_connue("A FC vs B FC", evs) and not ss.fixture_connue("X vs Y", evs)

    def test_combat_mma_regle_par_le_vainqueur(self, monkeypatch):
        ev = {"id": "card", "competitions": [
            {"status": {"type": {"state": "post", "completed": True}},
             "competitors": [{"athlete": {"displayName": "Ding Meng"}, "winner": False},
                             {"athlete": {"displayName": "Cam Nelson"}, "winner": True}]},
            {"status": {"type": {"state": "post", "completed": True}},
             "competitors": [{"athlete": {"displayName": "A B"}, "winner": True},
                             {"athlete": {"displayName": "C D"}, "winner": False}]}]}
        monkeypatch.setattr(ss, "_get_json", lambda *a, **k: {"events": [ev]})
        ss.reset_cache()
        r = ss.result_from_espn("Ding Meng vs Cam Nelson", "mma", "2026-08-30")
        assert (r["home_score"], r["away_score"]) == (0, 1)
        ss.reset_cache()
        r = ss.result_from_espn("Cam Nelson vs Ding Meng", "mma", "2026-08-30")
        assert (r["home_score"], r["away_score"]) == (1, 0)

    def test_combat_sans_vainqueur_ne_regle_pas(self, monkeypatch):
        ev = {"id": "card", "competitions": [
            {"status": {"type": {"state": "post", "completed": True}},
             "competitors": [{"athlete": {"displayName": "Ding Meng"}, "winner": False},
                             {"athlete": {"displayName": "Cam Nelson"}, "winner": False}]}]}
        monkeypatch.setattr(ss, "_get_json", lambda *a, **k: {"events": [ev]})
        ss.reset_cache()
        assert ss.result_from_espn("Ding Meng vs Cam Nelson", "mma", "2026-08-30") is None


class TestDepenseSurSportsReglables:
    def test_sports_reglables_derives_des_voies_de_reglement(self):
        r = ss.sports_reglables()
        assert {"soccer", "basketball", "baseball", "hockey", "mma", "americanfootball"} <= r
        assert "tennis" not in r and "boxing" not in r

    def test_la_politique_de_depense_ne_paie_pas_un_sport_non_reglable(self):
        from datetime import datetime, timezone
        from core.scan_windows import SpendPolicy
        p = SpendPolicy(lambda _k: None, lambda _k: None, allowance=240,
                        reglables={"soccer", "basketball"})
        now = datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc)
        ok, why = p.allow("tennis_atp_us_open", "tennis", now, 2000, cost=3)
        assert not ok and "non réglable" in why and p.engaged == 0
        assert p.skipped and p.skipped[0][0] == "tennis_atp_us_open"
        assert p.allow("soccer_epl", "soccer", now, 2000, cost=3)[0]

    def test_build_spend_policy_derive_les_non_reglables(self, monkeypatch):
        from datetime import datetime, timezone
        from tests.test_engine_circuit_breaker import FakeSB
        monkeypatch.setattr(eng, "_odds_pool_total_remaining", lambda: 2400)
        monkeypatch.setattr(eng, "_sports_with_imminent_signals", lambda _sb, _now: set())
        pol = eng._build_spend_policy(FakeSB(), datetime.now(timezone.utc))
        assert pol.reglables == set(ss.sports_reglables())
        assert "tennis" not in pol.reglables and "boxing" not in pol.reglables
        assert {"soccer", "mma"} <= pol.reglables
