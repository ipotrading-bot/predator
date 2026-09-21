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


class TestPushMuet:
    """Le 2026-09-21 l'opérateur a reçu le bundle hebdo QUATRE fois : le cron
    du lundi (en retard de 6h42) et trois `push` touchant un script de rapport,
    sur une branche de travail comme sur `main`. Le job `hebdo` se rejoue
    exprès sur push (boucle courte pour ajuster le rapport) — mais il parlait.
    `--no-telegram` garde le rejeu et supprime l'envoi."""

    class _Res:
        data: list = []

    class _Q:
        def __getattr__(self, _):
            return lambda *a, **k: self

        def execute(self):
            return TestPushMuet._Res()

    class _SB:
        def table(self, *a, **k):
            return TestPushMuet._Q()

    def _sans_base(self, monkeypatch):
        import core.ai_router
        from scripts import weekly_report as wr
        monkeypatch.setattr(wr, "get_db", lambda **k: self._SB())
        monkeypatch.setattr(core.ai_router, "health_summary", lambda: [])
        envois = []
        monkeypatch.setattr(wr, "_send", lambda text: envois.append(text) or True)
        return wr, envois

    def test_le_drapeau_calcule_le_rapport_sans_l_envoyer(self, monkeypatch):
        wr, envois = self._sans_base(monkeypatch)
        assert wr.main(["--no-telegram"]) == 0
        assert envois == [], "un push ne doit RIEN envoyer sur Telegram"

    def test_sans_drapeau_le_rapport_part(self, monkeypatch):
        wr, envois = self._sans_base(monkeypatch)
        assert wr.main([]) == 0
        assert len(envois) == 1

    def test_le_workflow_passe_le_drapeau_sur_un_push(self):
        yml = open(".github/workflows/reports.yml", encoding="utf-8").read()
        ligne = next(l for l in yml.splitlines()
                     if "run: python scripts/weekly_report.py" in l)
        assert "github.event_name == 'push'" in ligne and "--no-telegram" in ligne, ligne

    def test_les_deux_autres_rapports_n_envoient_rien(self):
        """Ils n'ont pas besoin du drapeau : ils impriment, ils ne postent pas.
        Si l'un se met à parler un jour, ce test le dit avant l'opérateur."""
        for script in ("scripts/rank_sports.py", "scripts/calibration_report.py"):
            src = open(script, encoding="utf-8").read()
            assert "api.telegram.org" not in src, f"{script} envoie sur Telegram"
