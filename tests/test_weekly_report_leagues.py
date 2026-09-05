"""
tests/test_weekly_report_leagues.py — la section « par ligue » du rapport
hebdo (2026-09-05) tient le même contrat que le reste : zone jouable, hors
fantômes, Wilson bas contre le point mort après taxe — jamais un taux nu.
"""
from core.constants import TAX_RATE
from core.stats_utils import p_breakeven, wilson_ci
from scripts.weekly_report import format_leagues, format_report, league_breakdown


def _row(league, outcome, odds=1.8, ttm=600, shadow=False):
    return {"league": league, "sport": "soccer", "outcome": outcome, "odds": odds,
            "time_to_match_minutes": ttm, "is_shadow": shadow}


class TestLeagueBreakdown:
    def test_wilson_et_point_mort_par_ligue(self):
        rows = [_row("MLS", "WIN")] * 8 + [_row("MLS", "LOSS")] * 4
        (d,) = league_breakdown(rows, min_decided=10)
        assert (d["league"], d["n"], d["wins"]) == ("MLS", 12, 8)
        assert d["wilson_lower"] == wilson_ci(8, 12)[0]
        assert d["p_breakeven"] == p_breakeven(1.8, TAX_RATE)
        assert abs(d["pnl_flat"] - (8 * 0.8 - 4)) < 1e-9

    def test_fantomes_et_remboursements_exclus(self):
        rows = ([_row("MLS", "WIN")] * 10
                + [_row("MLS", "WIN", ttm=30)] * 5          # < T-2h : fantôme
                + [_row("MLS", "WIN", shadow=True)] * 5     # shadow explicite
                + [_row("MLS", "PUSH")] * 5)
        (d,) = league_breakdown(rows, min_decided=10)
        assert d["n"] == 10 and d["wins"] == 10

    def test_les_petites_ligues_sont_agregees(self):
        rows = [_row("Big", "WIN")] * 10 + [_row("A", "LOSS")] * 3 + [_row("B", "WIN")] * 2
        out = league_breakdown(rows, min_decided=10)
        assert [d["league"][:3] for d in out] == ["Big", "aut"]
        assert out[1]["n"] == 5 and "2 ligues" in out[1]["league"]

    def test_vide_reste_vide(self):
        assert league_breakdown([]) == []
        assert format_leagues([]) == []


class TestFormat:
    def test_la_section_porte_wilson_et_requis_jamais_un_taux_nu(self):
        rows = [_row("La Liga", "WIN", odds=1.6)] * 9 + [_row("La Liga", "LOSS", odds=1.6)] * 3
        lignes = format_leagues(league_breakdown(rows, min_decided=10))
        texte = "\n".join(lignes)
        assert "La Liga — 9-3" in texte and "Wilson-" in texte and "requis" in texte
        assert "P&L +2.4 u" in texte

    def test_le_rapport_embarque_la_section(self):
        texte = format_report({}, {}, (0, 0), __import__("datetime").datetime(2026, 9, 7),
                              leagues=["", "🏟 *Par ligue*", "• X — 1-0"])
        assert "🏟 *Par ligue*" in texte
