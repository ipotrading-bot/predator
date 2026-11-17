"""
tests/test_points_cloture.py — la ligne d'un pari NBA contre la ligne de
CLÔTURE, en points (2026-11-17, `python scripts/ops.py nba`).

Le gagné/perdu ne peut pas juger la NBA à l'échelle d'un mois (35 paris :
espérance +1,06 u, écart-type 5,67 u). Les points pris sur la clôture, si :
89 paris, +0,90 point (t +5,6), mêmes paris à la clôture 45-44. Gardés ici :
  1. la lecture du bloc `pickcenter` d'ESPN, clôture exigée ;
  2. le SIGNE des points pris, par marché et par camp, orientation comprise ;
  3. le match d'un pari = le coup d'envoi le plus proche (deux franchises se
     rencontrent parfois deux fois en trois jours — la première version de
     la commande lisait le mauvais match et rendait 58/18/13 au lieu de
     57/21/11) ;
  4. règle 7 : le bilan porte Wilson et point mort ;
  5. règle 13 : budget chiffré, critère de retrait daté, commande câblée.
"""
import pathlib

import pytest

from core import points_cloture as pc

RACINE = pathlib.Path(__file__).resolve().parents[1]


def _resume(dom_ouv="-2.5", dom_clo="+1.5", tot_ouv="o224.5", tot_clo="o220.5"):
    """Le bloc tel qu'ESPN le rend (Detroit–Boston, 2026-10-20)."""
    return {"pickcenter": [{
        "provider": {"name": "DraftKings"},
        "pointSpread": {"home": {"close": {"line": dom_clo}, "open": {"line": dom_ouv}},
                        "away": {"close": {"line": "-1.5"}, "open": {"line": "+2.5"}}},
        "total": {"over": {"close": {"line": tot_clo}, "open": {"line": tot_ouv}},
                  "under": {"close": {"line": "u220.5"}, "open": {"line": "u224.5"}}}}]}


class TestLectureEspn:
    def test_ouverture_et_cloture(self):
        assert pc.lignes_du_resume(_resume()) == {
            "handicap_dom": 1.5, "total": 220.5,
            "handicap_dom_ouverture": -2.5, "total_ouverture": 224.5}

    @pytest.mark.parametrize("resume", [None, {}, {"pickcenter": []},
                                        {"pickcenter": [{"provider": {"name": "x"}}]}])
    def test_sans_bloc_rien(self, resume):
        assert pc.lignes_du_resume(resume) is None

    def test_cloture_illisible_rien(self):
        """« EVEN », « PK » : on ne mesure pas contre une ligne devinée."""
        assert pc.lignes_du_resume(_resume(dom_clo="EVEN")) is None

    def test_louverture_peut_manquer(self):
        r = _resume()
        del r["pickcenter"][0]["pointSpread"]["home"]["open"]
        assert pc.lignes_du_resume(r)["handicap_dom_ouverture"] is None

    def test_le_second_bloc_sert_si_le_premier_est_vide(self):
        r = _resume()
        r["pickcenter"].insert(0, {"provider": {"name": "vide"}})
        assert pc.lignes_du_resume(r)["total"] == 220.5


L = {"handicap_dom": -8.5, "total": 220.5}


class TestSigneDesPoints:
    def test_totaux(self):
        assert pc.points_pris("totals_over", 218.5, L) == 2.0      # moins de points à faire
        assert pc.points_pris("totals_over", 221.5, L) == -1.0
        assert pc.points_pris("totals_under", 221.5, L) == 1.0
        assert pc.points_pris("totals_under", 218.5, L) == -2.0
        assert pc.points_pris("totals_under", 220.5, L) == 0.0

    def test_handicap_du_domicile(self):
        assert pc.points_pris("spreads_home", -4.5, L) == 4.0      # on donne 4 points de moins
        assert pc.points_pris("spreads_home", -9.5, L) == -1.0

    def test_handicap_de_lexterieur(self):
        assert pc.points_pris("spreads_away", 9.5, L) == 1.0       # on reçoit un point de plus
        assert pc.points_pris("spreads_away", 6.5, L) == -2.0

    def test_orientation_inverse(self):
        """Le « home » du signal est l'extérieur d'ESPN : le camp s'inverse."""
        assert pc.ligne_de_cloture("spreads_home", L, domicile_en_premier=False) == 8.5
        assert pc.points_pris("spreads_home", 9.5, L, domicile_en_premier=False) == 1.0
        assert pc.points_pris("totals_over", 218.5, L, domicile_en_premier=False) == 2.0

    def test_le_cas_dallas_cleveland(self):
        """Signal 12706 : Dallas −4,5 à Cleveland, clôture Dallas −8,5 — quatre
        points d'avance, et perdu quand même (127-117)."""
        lignes = {"handicap_dom": 8.5, "total": 228.5}
        assert pc.points_pris("spreads_away", -4.5, lignes) == 4.0
        assert pc.marge("spreads_away", -4.5, 127, 117) == -14.5
        m = pc.mesurer("spreads_away", -4.5, lignes, 127, 117)
        assert m == {"points": 4.0, "marge_cloture": -18.5}

    def test_h2h_et_ligne_absente_ne_se_mesurent_pas(self):
        assert pc.points_pris("h2h", None, L) is None
        assert pc.points_pris("totals_over", None, L) is None
        assert pc.mesurer("h2h", None, L, 100, 90) is None
        assert pc.points_pris("spreads_home", -4.5, {"total": 220.5}) is None


class TestMarge:
    def test_totaux(self):
        assert pc.marge("totals_over", 220.5, 120, 110) == 9.5
        assert pc.marge("totals_under", 220.5, 120, 110) == -9.5

    def test_handicaps(self):
        assert pc.marge("spreads_home", -4.5, 120, 110) == 5.5
        assert pc.marge("spreads_away", 4.5, 120, 110) == -5.5
        assert pc.marge("spreads_home", -4.5, 120, 110, domicile_en_premier=False) == -14.5


class TestLeBonMatch:
    def test_le_coup_denvoi_le_plus_proche(self):
        candidats = [("2026-11-10T00:10Z", "aller"), ("2026-11-13T00:10Z", "retour")]
        assert pc.le_plus_proche(candidats, "2026-11-13T00:10:00+00:00")[1] == "retour"
        assert pc.le_plus_proche(candidats, "2026-11-10T00:40:00+00:00")[1] == "aller"

    def test_hors_fenetre_rien(self):
        assert pc.le_plus_proche([("2026-11-10T00:10Z", "x")], "2026-11-11T00:10:00+00:00") is None
        assert pc.le_plus_proche([], "2026-11-11T00:10:00+00:00") is None
        assert pc.le_plus_proche([(None, "x")], "2026-11-11T00:10:00+00:00") is None

    def test_la_commande_choisit_par_lheure(self):
        src = (RACINE / "scripts" / "ops.py").read_text(encoding="utf-8")
        assert "pc.le_plus_proche(candidats, r.get(\"match_time\"))" in src


def _m(outcome, points, marge_cloture, odds=1.93):
    return {"outcome": outcome, "points": points, "marge_cloture": marge_cloture, "odds": odds}


class TestBilan:
    MESURES = [_m("WIN", 1.0, 3.0), _m("WIN", 2.0, -1.0), _m("LOSS", 0.0, -5.0),
               _m("LOSS", -1.0, 2.0), _m("WIN", 1.0, 0.0)]

    def test_les_chiffres(self):
        b = pc.bilan(self.MESURES, tax_rate=0.0)
        assert (b["n"], b["gagnes"], b["perdus"]) == (5, 3, 2)
        assert b["points_moyens"] == pytest.approx(0.6)
        assert (b["meilleures"], b["egales"], b["pires"]) == (3, 1, 1)
        # Rejoués à la clôture : le remboursé (0) ne compte ni d'un côté ni de l'autre.
        assert (b["cloture_gagnes"], b["cloture_perdus"]) == (2, 2)
        assert b["t"] == pytest.approx(0.6 / (1.14017542509 / 5 ** 0.5), rel=1e-6)

    def test_jamais_un_taux_nu(self):
        """Règle 7 : Wilson ET point mort à côté de toute réussite."""
        b = pc.bilan(self.MESURES, tax_rate=0.0)
        assert 0 < b["wilson_bas"] < b["reussite"] < b["wilson_haut"] < 1
        assert b["point_mort"] == pytest.approx(1 / 1.93, abs=1e-4)
        ligne = pc.ligne_de_bilan("tout", b)
        assert "[" in ligne and "pm 51.8%" in ligne and "3-2" in ligne

    def test_la_taxe_est_celle_quon_lui_donne(self):
        assert pc.bilan(self.MESURES, tax_rate=0.20)["point_mort"] > 1 / 1.93

    def test_vide_et_non_decides(self):
        assert pc.bilan([], 0.0) == {"n": 0}
        assert pc.bilan([_m("PUSH", 1.0, 0.0)], 0.0) == {"n": 0}
        assert pc.ligne_de_bilan("x", {"n": 0}).endswith("n=0")

    def test_un_seul_pari_na_pas_de_t(self):
        assert pc.bilan([_m("WIN", 1.0, 2.0)], 0.0)["t"] is None


class TestRegleDEntree:
    """Règle 13 (AUDIT.md §3bis) : budget chiffré, critère de retrait daté."""

    def test_budget_et_critere_sont_ecrits(self):
        doc = pc.__doc__
        assert "BUDGET" in doc and "CRITÈRE DE RETRAIT" in doc
        assert 0 < pc.REQUETES_MAX <= 300 and 0 < pc.COUVERTURE_MIN < 1
        assert pc.REVUE_LE == "2026-12-17"

    def test_la_commande_est_cablee_plafonnee_et_ne_touche_pas_les_compteurs(self):
        src = (RACINE / "scripts" / "ops.py").read_text(encoding="utf-8")
        corps = src.split("def nba(args):", 1)[1].split("\ndef main(argv):", 1)[0]
        assert 'elif cmd == "nba":' in src and "ops.py nba [jours]" in src
        assert "pc.REQUETES_MAX" in corps and "pc.COUVERTURE_MIN" in corps
        assert "daily_quota.add" not in corps and "_rest(\"POST\"" not in corps
        assert "ss._espn_jour(" in corps          # un jour par requête (règle 15)
        assert "dates=" not in corps

    def test_aucun_cron_ne_lappelle(self):
        for wf in (RACINE / ".github" / "workflows").glob("*.yml"):
            assert "ops.py nba" not in wf.read_text(encoding="utf-8"), wf.name
