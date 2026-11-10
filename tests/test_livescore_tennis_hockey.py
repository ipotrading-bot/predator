"""Gardiens : LiveScore règle aussi le tennis et le hockey (2026-11-10).

DÉCISION OPÉRATEUR du 2026-11-10 : « inclus les autres ligues de tennis et de
hockey ». ESPN ne règle que les tableaux ATP/WTA et la NHL ; Challengers, AHL,
Allsvenskan… sortaient « NON RÉGLABLE ». Mesuré ce jour-là : LiveScore connaît
27 des 27 matchs de tennis et 7 des 7 matchs de hockey écartés par les quatre
scans standard.

Les charges ci-dessous reprennent des événements RÉELS lus le 2026-11-10
(champs `Tr1S1`, `Ewt`, `Tr1OR`, `Trp1`…), pas une structure supposée.
"""
import pytest

from core import score_sources as ss
from core.settlement import determine_outcome


@pytest.fixture(autouse=True)
def _cache_vide():
    ss._CACHE_LS.clear()
    yield
    ss._CACHE_LS.clear()


def _stage(ligue: str, *events: dict) -> dict:
    return {"Stages": [{"Cnm": ligue.split(" - ")[0], "Snm": ligue.split(" - ")[-1],
                        "Events": list(events)}]}


def _servir(monkeypatch, charge: dict) -> list:
    appels: list = []

    def get(url, bucket, budget, source=None):
        appels.append(url)
        return charge
    monkeypatch.setattr(ss, "_get_json", get)
    return appels


# WTA Finals, 2026-11-09 : Sabalenka b. Muchova 7-6(2) 7-5.
SABALENKA = {"Eid": "1915560", "Tr1": "2", "Tr2": "0", "Tr1S1": "7", "Tr2S1": "6",
             "Tr1S2": "7", "Tr2S2": "5", "Tr1S1T": "7", "Tr2S1T": "2",
             "T1": [{"Nm": "Aryna Sabalenka"}], "T2": [{"Nm": "Karolina Muchova"}],
             "Eps": "FT", "Ewt": 1}
# Challenger d'Orléans, 2026-11-10 : abandon à 4-2 au premier set.
ABANDON = {"Eid": "1917040", "Tr1": "0", "Tr2": "0", "Tr1S1": "4", "Tr2S1": "2",
           "T1": [{"Nm": "Buvaysar Gadamauri"}], "T2": [{"Nm": "Dusan Lajovic"}],
           "Eps": "Ret.", "Ewt": 1}
# ATP Stockholm, 2026-11-10 : forfait avant le match.
FORFAIT = {"Eid": "1917074", "Tr1": "0", "Tr2": "0",
           "T1": [{"Nm": "Tomas Etcheverry"}], "T2": [{"Nm": "Thiago Agustin Tirante"}],
           "Eps": "W.O.", "Ewt": 2}
# WTA Finals, double, 2026-11-10.
DOUBLE = {"Eid": "1915577", "Tr1": "2", "Tr2": "0", "Tr1S1": "6", "Tr2S1": "3",
          "Tr1S2": "7", "Tr2S2": "6",
          "T1": [{"Nm": "Gabriela Dabrowski"}, {"Nm": "Luisa Stefani"}],
          "T2": [{"Nm": "Elise Mertens"}, {"Nm": "Shuai Zhang"}], "Eps": "FT", "Ewt": 1}
# NHL, 2026-11-09 : Calgary b. Philadelphia 4-3 après prolongation.
PROLONGATION = {"Eid": "1835162", "Tr1": "4", "Tr2": "3", "Tr1OR": "3", "Tr2OR": "3",
                "Tr1OT": "1", "Tr2OT": "0",
                "T1": [{"Nm": "Calgary Flames"}], "T2": [{"Nm": "Philadelphia Flyers"}],
                "Eps": "OT", "Ewt": 1}
# NHL, 2026-11-10 : San Jose b. NY Islanders 3-2 aux tirs au but.
TIRS_AU_BUT = {"Eid": "1835172", "Tr1": "3", "Tr2": "2", "Tr1OR": "2", "Tr2OR": "2",
               "Trp1": "1", "Trp2": "0",
               "T1": [{"Nm": "San Jose Sharks"}], "T2": [{"Nm": "New York Islanders"}],
               "Eps": "AP", "Ewt": 1}


class TestRegistre:
    def test_les_trois_sports_cables(self):
        """Règle 13 : un segment ne quitte ce registre qu'avec son critère de
        retrait relevé (revue du 2026-12-10, voir le commentaire de
        `_LS_SPORTS`) et la ligne d'INCIDENTS.md du jour."""
        assert ss._LS_SPORTS == {"soccer": "soccer", "tennis": "tennis", "hockey": "hockey"}

    def test_le_budget_et_le_critere_de_retrait_sont_ecrits(self):
        import inspect
        src = inspect.getsource(ss)
        bloc = src[src.index("TENNIS ET HOCKEY câblés le 2026-11-10"):src.index("_LS_SPORTS = {")]
        assert "CRITÈRE DE RETRAIT" in bloc and "2026-12-10" in bloc
        assert ss.LIVESCORE_DAILY_BUDGET >= 264        # borne haute chiffrée des trois sports


class TestTennis:
    def test_un_match_termine_regle_le_vainqueur_et_les_jeux(self, monkeypatch):
        _servir(monkeypatch, _stage("WTA Finals - Group B", SABALENKA))
        r = ss.result_from_livescore("Aryna Sabalenka vs Karolina Muchova", "tennis", "2026-11-09")
        assert r == {"home_score": 1, "away_score": 0, "completed": True,
                     "source": "livescore", "decompte": (14, 11)}

    def test_l_ordre_inverse_du_signal_retourne_score_et_decompte(self, monkeypatch):
        _servir(monkeypatch, _stage("WTA Finals - Group B", SABALENKA))
        r = ss.result_from_livescore("Karolina Muchova vs Aryna Sabalenka", "tennis", "2026-11-09")
        assert (r["home_score"], r["away_score"], r["decompte"]) == (0, 1, (11, 14))

    def test_le_total_se_regle_sur_les_jeux_jamais_sur_le_drapeau(self, monkeypatch):
        _servir(monkeypatch, _stage("WTA Finals - Group B", SABALENKA))
        r = ss.result_from_livescore("Aryna Sabalenka vs Karolina Muchova", "tennis", "2026-11-09")
        args = ("Aryna Sabalenka", "Karolina Muchova", r["home_score"], r["away_score"])
        # 25 jeux joués.
        assert determine_outcome("tennis", "totals_over", "Over 22.5", *args,
                                 decompte=r["decompte"]) == "WIN"
        assert determine_outcome("tennis", "totals_under", "Under 22.5", *args,
                                 decompte=r["decompte"]) == "LOSS"
        assert determine_outcome("tennis", "h2h", "Karolina Muchova", *args,
                                 decompte=r["decompte"]) == "LOSS"

    @pytest.mark.parametrize("evenement", [ABANDON, FORFAIT])
    def test_abandon_et_forfait_ne_reglent_rien(self, monkeypatch, evenement):
        _servir(monkeypatch, _stage("ATP Challenger - Orleans, France", evenement))
        noms = f"{evenement['T1'][0]['Nm']} vs {evenement['T2'][0]['Nm']}"
        assert ss.result_from_livescore(noms, "tennis", "2026-11-10") is None

    def test_un_match_en_cours_ne_regle_pas(self, monkeypatch):
        en_cours = dict(SABALENKA, Eps="S2", Tr1="1", Tr2="0")
        _servir(monkeypatch, _stage("WTA Finals - Group B", en_cours))
        assert ss.result_from_livescore("Aryna Sabalenka vs Karolina Muchova", "tennis",
                                        "2026-11-09") is None

    def test_un_double_n_est_jamais_le_match_d_un_simple(self, monkeypatch):
        _servir(monkeypatch, _stage("WTA Finals Doubles - Group B", DOUBLE))
        assert ss.result_from_livescore("Gabriela Dabrowski vs Elise Mertens", "tennis",
                                        "2026-11-10") is None
        assert not ss.livescore_connait("Gabriela Dabrowski vs Elise Mertens", "tennis",
                                        "2026-11-10")

    def test_sans_les_jeux_de_chaque_set_pas_de_decompte(self, monkeypatch):
        """Un set gagné sans sa ligne de jeux : le vainqueur se règle, un total
        sortira UNKNOWN (settlement._paire_comptable) — jamais un compte partiel."""
        troue = {k: v for k, v in SABALENKA.items() if k not in ("Tr1S2", "Tr2S2")}
        _servir(monkeypatch, _stage("WTA Finals - Group B", troue))
        r = ss.result_from_livescore("Aryna Sabalenka vs Karolina Muchova", "tennis", "2026-11-09")
        assert (r["home_score"], r["away_score"]) == (1, 0) and "decompte" not in r
        assert determine_outcome("tennis", "totals_over", "Over 22.5", "Aryna Sabalenka",
                                 "Karolina Muchova", 1, 0, decompte=None) == "UNKNOWN"

    def test_sets_et_vainqueur_declare_en_desaccord_ne_reglent_pas(self, monkeypatch):
        _servir(monkeypatch, _stage("WTA Finals - Group B", dict(SABALENKA, Ewt=2)))
        assert ss.result_from_livescore("Aryna Sabalenka vs Karolina Muchova", "tennis",
                                        "2026-11-09") is None

    def test_le_perimetre_reconnait_un_challenger_dans_les_deux_ordres(self, monkeypatch):
        a_venir = {"Eid": "9", "T1": [{"Nm": "Shintaro Mochizuki"}],
                   "T2": [{"Nm": "Calvin Hemery"}], "Eps": "NS"}
        _servir(monkeypatch, _stage("ATP Challenger - Orleans, France", a_venir))
        assert ss.livescore_connait("Shintaro Mochizuki vs Calvin Hemery", "tennis", "2026-11-10")
        assert ss.livescore_connait("Calvin Hemery vs Shintaro Mochizuki", "tennis", "2026-11-10")
        assert not ss.livescore_connait("Shintaro Mochizuki vs Dusan Lajovic", "tennis",
                                        "2026-11-10")


class TestHockey:
    @pytest.mark.parametrize("evenement, attendu", [(PROLONGATION, (4, 3)), (TIRS_AU_BUT, (3, 2))])
    def test_prolongation_et_tirs_au_but_reglent_sur_le_score_final(self, monkeypatch,
                                                                    evenement, attendu):
        """Une moneyline de hockey à deux issues compte la prolongation — à
        l'inverse du football, réglé à 90 minutes."""
        _servir(monkeypatch, _stage("NHL - Regular season", evenement))
        noms = f"{evenement['T1'][0]['Nm']} vs {evenement['T2'][0]['Nm']}"
        r = ss.result_from_livescore(noms, "hockey", "2026-11-09")
        assert (r["home_score"], r["away_score"]) == attendu and "decompte" not in r
        assert determine_outcome("hockey", "h2h", evenement["T1"][0]["Nm"],
                                 evenement["T1"][0]["Nm"], evenement["T2"][0]["Nm"],
                                 *attendu) == "WIN"

    def test_un_match_a_venir_ou_en_cours_ne_regle_pas(self, monkeypatch):
        for statut in ("NS", "P2"):
            ss._CACHE_LS.clear()
            _servir(monkeypatch, _stage("USA - AHL", dict(PROLONGATION, Eps=statut)))
            assert ss.result_from_livescore("Calgary Flames vs Philadelphia Flyers", "hockey",
                                            "2026-11-09") is None

    def test_une_egalite_n_est_pas_un_resultat(self, monkeypatch):
        _servir(monkeypatch, _stage("USA - AHL", dict(PROLONGATION, Eps="FT", Tr1="3", Tr2="3")))
        assert ss.result_from_livescore("Calgary Flames vs Philadelphia Flyers", "hockey",
                                        "2026-11-09") is None

    def test_une_equipe_rangee_a_l_envers_n_est_pas_reconnue(self, monkeypatch):
        """Seul le tennis accepte l'ordre inverse : en sport d'équipe on ne
        devine pas qui reçoit."""
        _servir(monkeypatch, _stage("NHL - Regular season", PROLONGATION))
        assert ss.result_from_livescore("Philadelphia Flyers vs Calgary Flames", "hockey",
                                        "2026-11-09") is None
        assert not ss.livescore_connait("Philadelphia Flyers vs Calgary Flames", "hockey",
                                        "2026-11-09")

    def test_le_perimetre_reconnait_l_ahl(self, monkeypatch):
        a_venir = {"Eid": "7", "T1": [{"Nm": "Milwaukee Admirals"}], "T2": [{"Nm": "Iowa Wild"}],
                   "Eps": "NS"}
        appels = _servir(monkeypatch, _stage("USA - AHL", a_venir))
        assert ss.livescore_connait("Milwaukee Admirals vs Iowa Wild", "hockey", "2026-11-11")
        assert not ss.livescore_connait("Milwaukee Admirals vs Minnesota Wild", "hockey",
                                        "2026-11-11")
        assert len(appels) == 1 and appels[0].endswith("/hockey/20261111/0")


class TestLeFootballNeBougePas:
    def test_tirs_au_but_toujours_regles_a_90_minutes(self, monkeypatch):
        ev = {"Eid": "1", "T1": [{"Nm": "Grorud"}], "T2": [{"Nm": "Moss"}], "Tr1": "1",
              "Tr2": "1", "Tr1OR": "1", "Tr2OR": "1", "Trp1": "6", "Trp2": "5", "Eps": "AP"}
        _servir(monkeypatch, _stage("Norway - Cup", ev))
        r = ss.result_from_livescore("Grorud vs Moss", "soccer", "2026-09-23")
        assert (r["home_score"], r["away_score"]) == (1, 1)

    def test_le_football_ne_se_lit_pas_a_l_envers(self, monkeypatch):
        ev = {"Eid": "1", "T1": [{"Nm": "Bali United"}], "T2": [{"Nm": "PSS Sleman"}],
              "Tr1": "2", "Tr2": "1", "Eps": "FT"}
        _servir(monkeypatch, _stage("Indonesia - Super League", ev))
        assert ss.result_from_livescore("PSS Sleman vs Bali United", "soccer",
                                        "2026-09-04") is None
