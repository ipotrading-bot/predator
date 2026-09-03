"""
tests/test_perf_mois_ligues.py — /performance, 2026-09-03 (demande opérateur) :
le résumé PAR MOIS revient, l'historique se limite au mois choisi dans un menu
déroulant (août « archivé », accessible par ce seul menu), et un tableau PAR
LIGUE dit quelles ligues perdent — avec Wilson et point mort, jamais un taux
nu (règle dure n°7). Tout est pur : core/perf_view.py.
"""
import inspect
import pathlib

from core import perf_view
from core.perf_view import (ALL_MONTHS, SANS_LIGUE, league_breakdown,
                            month_label, monthly_summary, pick_month,
                            rows_of_month)

RACINE = pathlib.Path(__file__).resolve().parent.parent


def _r(mois, outcome, odds=2.0, league="L1", sport="soccer", **extra):
    d = {"created_at": f"{mois}-10T12:00:00+00:00", "outcome": outcome,
         "odds": odds, "league": league, "sport": sport}
    d.update(extra)
    return d


class TestPickMonth:
    MOIS = ["2026-09", "2026-08"]

    def test_par_defaut_le_mois_courant(self):
        assert pick_month(None, self.MOIS) == "2026-09"

    def test_un_mois_de_la_fenetre_est_accepte(self):
        assert pick_month("2026-08", self.MOIS) == "2026-08"

    def test_tout_rouvre_la_fenetre_entiere(self):
        assert pick_month(ALL_MONTHS, self.MOIS) == ALL_MONTHS

    def test_hors_fenetre_retombe_sur_le_mois_courant(self):
        """Le menu est la SEULE porte vers août ; juillet (archivé) ou une
        faute de frappe ne rouvrent rien — ni page vide, ni contournement
        de l'époque zéro."""
        assert pick_month("2026-07", self.MOIS) == "2026-09"
        assert pick_month("n'importe quoi", self.MOIS) == "2026-09"
        assert pick_month("", self.MOIS) == "2026-09"

    def test_fenetre_vide(self):
        assert pick_month("2026-09", []) is None

    def test_rows_of_month(self):
        rows = [_r("2026-09", "WIN"), _r("2026-08", "LOSS")]
        assert [r["outcome"] for r in rows_of_month(rows, "2026-08")] == ["LOSS"]
        assert len(rows_of_month(rows, ALL_MONTHS)) == 2
        assert len(rows_of_month(rows, None)) == 2

    def test_libelle_francais_et_tolerant(self):
        assert month_label("2026-09") == "septembre 2026"
        assert month_label("2026-08") == "août 2026"
        assert month_label("bizarre") == "bizarre"


class TestMonthlySummary:
    def test_une_carte_par_mois_la_plus_recente_dabord(self):
        rows = [_r("2026-08", "WIN", 1.5), _r("2026-08", "LOSS", 1.5),
                _r("2026-08", "PUSH"), _r("2026-08", "expired"),
                _r("2026-09", "WIN", 2.0)]
        cartes = monthly_summary(rows, tax_rate=0.0)
        assert [c["month"] for c in cartes] == ["2026-09", "2026-08"]
        aout = cartes[1]
        assert (aout["total"], aout["wins"], aout["losses"], aout["pushes"], aout["expired"]) == (4, 1, 1, 1, 1)
        assert aout["win_rate"] == 50.0
        assert aout["label"] == "août 2026"

    def test_jamais_un_taux_nu(self):
        """Règle dure n°7 : Wilson ET point mort sur chaque carte."""
        carte = monthly_summary([_r("2026-09", "WIN", 1.5)] * 3, 0.0)[0]
        assert 0 <= carte["win_rate_lo"] <= carte["win_rate"] <= carte["win_rate_hi"] <= 100
        assert carte["p_breakeven"] is not None
        assert carte["avg_odds"] == 1.5

    def test_pnl_a_mise_plate_depuis_la_cote_et_la_taxe(self):
        """`profit_units` est NULL sur une partie des lignes : le P&L se
        recalcule depuis la cote, taxe sur le gain net."""
        rows = [_r("2026-09", "WIN", 2.0), _r("2026-09", "LOSS", 2.0)]
        assert monthly_summary(rows, 0.0)[0]["pnl_units"] == 0.0
        assert monthly_summary(rows, 0.5)[0]["pnl_units"] == -0.5

    def test_mois_sans_pari_decisif(self):
        carte = monthly_summary([_r("2026-09", "expired")], 0.0)[0]
        assert carte["win_rate"] is None
        assert carte["above_breakeven"] is False and carte["below_breakeven"] is False

    def test_clv_reel_prime_et_ne_se_melange_pas(self):
        rows = [_r("2026-09", "WIN", clv_pct_real=3.0, clv_final=-9.0),
                _r("2026-09", "WIN", clv_final=-9.0)]
        carte = monthly_summary(rows, 0.0)[0]
        assert carte["clv_is_real"] is True and carte["avg_clv"] == 3.0 and carte["clv_n"] == 1
        carte2 = monthly_summary([_r("2026-09", "WIN", clv_final=-9.0)], 0.0)[0]
        assert carte2["clv_is_real"] is False and carte2["avg_clv"] == -9.0

    def test_ligne_sans_date_ignoree(self):
        assert monthly_summary([{"outcome": "WIN"}], 0.0) == []


class TestLeagueBreakdown:
    def test_perdantes_dabord_et_seuil_daffichage(self):
        rows = ([_r("2026-09", "LOSS", 1.8, league="Argentine")] * 4
                + [_r("2026-09", "WIN", 1.8, league="Argentine")]
                + [_r("2026-09", "WIN", 1.8, league="MLS")] * 5
                + [_r("2026-09", "LOSS", 1.8, league="Japon")] * 2)
        tab = league_breakdown(rows, 0.0, min_n=5)
        assert [l["league"] for l in tab] == ["Argentine", "MLS"]   # Japon : 2 < 5
        assert tab[0]["pnl_units"] < tab[1]["pnl_units"]
        assert (tab[0]["wins"], tab[0]["losses"], tab[0]["n"]) == (1, 4, 5)
        tout = league_breakdown(rows, 0.0, min_n=1)
        assert len(tout) == 3

    def test_jamais_un_taux_nu(self):
        lg = league_breakdown([_r("2026-09", "WIN", 1.6)] * 5, 0.0, 5)[0]
        assert lg["win_rate_lo"] <= lg["win_rate"] <= lg["win_rate_hi"]
        assert lg["p_breakeven"] is not None

    def test_verdict_par_lintervalle_pas_par_le_point_moyen(self):
        """6 sur 10 à cote 1,70 : le taux (60 %) dépasse le point mort
        (≈59 %) mais l'intervalle de Wilson l'encadre → rien n'est démontré."""
        rows = [_r("2026-09", "WIN", 1.7)] * 6 + [_r("2026-09", "LOSS", 1.7)] * 4
        lg = league_breakdown(rows, 0.0, 5)[0]
        assert lg["win_rate"] == 60.0
        assert lg["above_breakeven"] is False and lg["below_breakeven"] is False
        # 40 défaites sur 40 : intervalle entier sous le point mort → perdante
        lg2 = league_breakdown([_r("2026-09", "LOSS", 1.7)] * 40, 0.0, 5)[0]
        assert lg2["below_breakeven"] is True
        lg3 = league_breakdown([_r("2026-09", "WIN", 1.7)] * 40, 0.0, 5)[0]
        assert lg3["above_breakeven"] is True

    def test_ligue_absente_regroupee_et_expires_comptes(self):
        rows = ([_r("2026-09", "LOSS", league=None)] * 5
                + [_r("2026-09", "expired", league="")] * 3
                + [_r("2026-09", "PUSH", league=None)])
        tab = league_breakdown(rows, 0.0, 5)
        assert len(tab) == 1
        assert tab[0]["league"] == SANS_LIGUE
        assert tab[0]["n"] == 5 and tab[0]["expired"] == 3

    def test_pas_de_normalisation_des_libelles(self):
        """Règle n°6 : « WNBA » et « USA - WNBA » restent deux lignes — la
        divergence des sources se voit au lieu d'être cachée par une liste
        tenue à la main."""
        rows = ([_r("2026-09", "WIN", league="WNBA", sport="basketball")] * 5
                + [_r("2026-09", "WIN", league="USA - WNBA", sport="basketball")] * 5)
        assert len(league_breakdown(rows, 0.0, 5)) == 2


class TestBranchement:
    def test_la_route_utilise_les_fonctions_pures(self):
        import api.index as dash
        src = inspect.getsource(dash.performance)
        for attendu in ("_monthly_summary(", "_league_breakdown(", "_pick_month(",
                        "_rows_of_month(", 'request.args.get("mois")',
                        "_perf_filter_rows(", ".gte(\"created_at\""):
            assert attendu in src, attendu

    def test_le_gabarit_porte_menu_cartes_et_ligues(self):
        g = (RACINE / "templates" / "performance.html").read_text(encoding="utf-8")
        assert 'id="mois-select"' in g and "?mois=" in g
        for v in ("monthly", "leagues", "mois_label", "all_months"):
            assert v in g, v
        # Règle n°7 rendue : Wilson et point mort sur les cartes ET les ligues
        assert g.count("win_rate_lo") >= 2 and g.count("p_breakeven") >= 3
        # Fantômes T-2h à part sur les cartes et dans les tuiles, jamais dans un taux
        assert "m.phantoms" in g and "global_s.phantoms" in g

    def test_le_menu_ne_propose_que_la_fenetre(self):
        """Les options du menu sont `months` (= shown_months) + « tout » :
        aucune liste de mois écrite à la main dans le gabarit."""
        g = (RACINE / "templates" / "performance.html").read_text(encoding="utf-8")
        assert "{% for v, lbl in months %}" in g
        assert "2026-07" not in g

    def test_la_formule_du_bloc_decisif_vit_une_seule_fois(self):
        assert perf_view._bloc_decisif.__module__ == "core.perf_view"
        src = inspect.getsource(perf_view.league_breakdown) + inspect.getsource(perf_view.monthly_summary)
        assert "wilson_ci(" not in src   # les deux passent par _bloc_decisif


class TestZoneJouable:
    def test_bornes_importees_de_la_couche_dapprentissage(self):
        """Règle n°6 : le dashboard découpe le ledger là où `playable_rows`
        le découpe — pas de bornes recopiées."""
        from core.learning_layer import _PLAYABLE_MAX_MINUTES, _PLAYABLE_MIN_MINUTES
        assert perf_view.playable_zone({"time_to_match_minutes": _PLAYABLE_MIN_MINUTES}) == "zone"
        assert perf_view.playable_zone({"time_to_match_minutes": _PLAYABLE_MAX_MINUTES}) == "zone"
        assert perf_view.playable_zone({"time_to_match_minutes": _PLAYABLE_MIN_MINUTES - 1}) == "golden"
        assert perf_view.playable_zone({"time_to_match_minutes": _PLAYABLE_MAX_MINUTES + 1}) == "hors"
        assert perf_view.playable_zone({}) == "nc"
        assert perf_view.playable_zone({"time_to_match_minutes": "x"}) == "nc"
        assert "_PLAYABLE_MIN_MINUTES =" not in inspect.getsource(perf_view)

    def test_les_pertes_hors_zone_sont_comptees_a_part(self):
        rows = ([_r("2026-09", "LOSS", time_to_match_minutes=30)] * 3      # golden
                + [_r("2026-09", "LOSS", time_to_match_minutes=600)] * 2  # zone
                + [_r("2026-09", "WIN", time_to_match_minutes=3000)])     # hors
        lg = league_breakdown(rows, 0.0, 5)[0]
        assert lg["losses_out_of_zone"] == 3
        assert lg["zones"]["zone"] == {"wins": 0, "losses": 2}
        assert lg["zones"]["hors"] == {"wins": 1, "losses": 0}
        carte = monthly_summary(rows, 0.0)[0]
        assert carte["zones"]["golden"]["losses"] == 3


class TestRecommandesEtMarches:
    """2026-09-03, second lot : le bandeau ne compte que les paris RECOMMANDÉS,
    les fantômes golden hour sont à part, et un tableau PAR MARCHÉ existe."""

    def test_recommandes_gardent_lincertain_et_ecartent_les_fantomes(self):
        rows = [_r("2026-09", "WIN", time_to_match_minutes=30),     # golden
                _r("2026-09", "WIN", time_to_match_minutes=600),    # zone
                _r("2026-09", "WIN", time_to_match_minutes=3000),   # hors
                _r("2026-09", "WIN")]                               # inconnu
        reco = perf_view.recommended_rows(rows)
        assert [perf_view.playable_zone(r) for r in reco] == ["zone", "nc"]
        assert [perf_view.playable_zone(r) for r in perf_view.phantom_rows(rows)] == ["golden", "hors"]
        assert len(reco) + len(perf_view.phantom_rows(rows)) == len(rows)

    def test_par_marche_perdants_dabord_et_repli_market(self):
        rows = ([_r("2026-09", "LOSS", 1.9, market_type="spreads_away")] * 5
                + [_r("2026-09", "WIN", 1.9, market="h2h")] * 5)
        tab = perf_view.market_breakdown(rows, 0.0, 5)
        assert [m["market"] for m in tab] == ["spreads_away", "h2h"]
        assert tab[0]["p_breakeven"] is not None and "win_rate_lo" in tab[0]
        assert perf_view.market_breakdown([_r("2026-09", "WIN")] * 2, 0.0, 5) == []

    def test_la_route_separe_recommandes_et_fantomes(self):
        import api.index as dash
        src = inspect.getsource(dash.performance)
        assert "_recommended_rows(rows)" in src and "_phantom_rows(rows)" in src
        assert '"phantoms"' in src and "_market_breakdown(" in src
        # les agrégats, les cartes, le détail : tout part de `reco`, plus de `rows`
        assert "for r in reco if r.get(\"outcome\") in (\"WIN\", \"LOSS\")" in src
        assert "_monthly_summary(reco" in src and "_rows_of_month(reco" in src
        assert "_sport_breakdown(reco" in src

    def test_le_gabarit_montre_fantomes_et_marches(self):
        g = (RACINE / "templates" / "performance.html").read_text(encoding="utf-8")
        assert "global_s.phantoms" in g and "markets" in g
        # Un seul gabarit de tableau (macro) pour ligues et marchés — pas deux copies
        assert g.count("{% macro tableau(") == 1 and "tableau(markets" in g and "tableau(leagues" in g
        # Le menu est dans l'en-tête, sans phrase d'explication
        assert "Les chiffres du haut de page" not in g


class TestParSport:
    def test_meme_bloc_que_les_mois_et_les_ligues(self):
        rows = ([_r("2026-09", "WIN", 1.6, sport="soccer")] * 6
                + [_r("2026-09", "LOSS", 1.9, sport="basketball")] * 3)
        tab = perf_view.sport_breakdown(rows, 0.0)
        assert [d["sport"] for d in tab] == ["basketball", "soccer"]   # perdant d'abord
        assert tab[0]["pnl_units"] == -3.0 and tab[1]["p_breakeven"] is not None
        assert "win_rate_lo" in tab[1] and "wilson_ci(" not in inspect.getsource(perf_view.sport_breakdown)

    def test_la_route_ne_recalcule_plus_par_sport_en_ligne(self):
        import api.index as dash
        src = inspect.getsource(dash.performance)
        assert "sport_perf" not in src
