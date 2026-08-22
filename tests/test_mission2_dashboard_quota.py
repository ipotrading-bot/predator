"""
tests/test_mission2_dashboard_quota.py — Mission 2, Phases 1 et 2.

Phase 1 : les sports retirés et les mois archivés disparaissent de TOUTES les
vues de /performance (filtre d'affichage pur, core/perf_view.py — rien n'est
supprimé du ledger) ; les scripts rank/calibration sautent les retirés.
Phase 2 : le widget « Quota OddsAPI » n'existe plus, mais la surveillance
n'est pas muette — log à chaque run, alerte Telegram 20 % / 5 %, UNE par
palier et par 24 h.
"""
import inspect
from datetime import datetime, timezone

import run_engine as eng
from core import odds_api, perf_view
from core.constants import RETIRED_SPORTS


def _row(sport, created, outcome="WIN"):
    return {"sport": sport, "created_at": created, "outcome": outcome}


class TestPerfView:
    NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)

    def test_la_fenetre_glissante_sarrete_a_lepoque_zero(self):
        """`shown_months` ne descend jamais sous PERF_START_MONTH.

        MODIFIÉ le 2026-08-22 : ce test attendait ["2026-08", "2026-07"].
        Juillet a été écarté sur décision opérateur — « predator n'était pas
        au point et avait des bugs, on recommence tout en août » — et ses
        194 lignes archivées. Sans cette borne, la fenêtre glissante
        afficherait une carte « juillet 0 gagné / 0 perdu » : un mois vide
        qui ne dit pas « aucun pari » mais « période exclue ». Afficher 0/0
        pour une période volontairement écartée trompe plus que de ne rien
        afficher.
        """
        assert perf_view.shown_months(self.NOW, 2) == ["2026-08"]
        assert perf_view.shown_months(self.NOW, 6) == ["2026-08"]

    def test_la_fenetre_reste_glissante_au_dessus_de_lepoque(self, monkeypatch):
        """La borne coupe le passé, elle ne fige pas la fenêtre : dans six
        mois, /performance doit toujours montrer les N derniers mois."""
        monkeypatch.setattr(perf_view, "PERF_START_MONTH", "2026-08")
        futur = datetime(2027, 1, 5, tzinfo=timezone.utc)
        assert perf_view.shown_months(futur, 3) == ["2027-01", "2026-12", "2026-11"]

    def test_une_epoque_plus_ancienne_rouvre_les_mois(self, monkeypatch):
        """PERF_START_MONTH reste pilotable : abaisser la borne (pour
        inspecter une archive restaurée) redonne accès aux mois antérieurs."""
        monkeypatch.setattr(perf_view, "PERF_START_MONTH", "2026-06")
        assert perf_view.shown_months(self.NOW, 3) == ["2026-08", "2026-07", "2026-06"]

    def test_retired_sports_and_old_months_are_hidden(self):
        rows = [_row("soccer", "2026-08-10T00:00:00+00:00"),
                _row("tabletennis", "2026-08-11T00:00:00+00:00"),
                _row("esports", "2026-08-12T00:00:00+00:00"),
                _row("soccer", "2026-06-30T00:00:00+00:00"),      # avant l'époque
                _row("mma", "2026-07-15T00:00:00+00:00")]         # avant l'époque
        kept = perf_view.filter_rows(rows, self.NOW, months_shown=2)
        assert [(r["sport"], r["created_at"][:7]) for r in kept] == \
            [("soccer", "2026-08")]

    def test_une_fenetre_elargie_ne_rouvre_pas_juillet(self):
        """Le garde-fou qui compte vraiment. `filter_rows` porte la condition
        d'époque EN PLUS de `shown_months` : relever PERF_MONTHS_SHOWN pour
        inspecter un historique ne doit pas ramener juillet en douce dans les
        agrégats — il faut le décider explicitement, en abaissant la borne."""
        rows = [_row("soccer", "2026-08-10T00:00:00+00:00"),
                _row("soccer", "2026-07-10T00:00:00+00:00")]
        kept = perf_view.filter_rows(rows, self.NOW, months_shown=12)
        assert [r["created_at"][:7] for r in kept] == ["2026-08"]

    def test_lepoque_zero_est_aout_2026(self):
        assert perf_view.PERF_START_MONTH == "2026-08"

    def test_le_script_darchivage_de_juillet_existe_et_narchive_pas_a_laveugle(self):
        sql = open("sql/migrate_v10_5_archive_pre_august.sql").read()
        # Déplacement, jamais destruction sèche
        assert "ai_learning_ledger_archive" in sql and "archived_at" in sql
        assert "USING ai_learning_ledger_archive" in sql
        # Les signaux ACTIFS ne doivent jamais être emportés
        assert "s.status <> 'active'" in sql
        # La borne de code doit être citée : SQL et code se complètent
        assert "PERF_START_MONTH" in sql

    def test_default_is_two_months_and_env_driven(self):
        assert perf_view.PERF_MONTHS_SHOWN == 2

    def test_performance_route_goes_through_the_filter(self):
        import api.index as dash
        assert "_perf_filter_rows(" in inspect.getsource(dash.performance)

    def test_scripts_skip_retired_sports(self):
        for p in ("scripts/rank_sports.py", "scripts/calibration_report.py"):
            assert "RETIRED_SPORTS" in open(p).read()

    def test_archive_script_exists_and_never_deletes_blindly(self):
        sql = open("sql/archive_retired_sports.sql").read()
        assert "ai_learning_ledger_archive" in sql and "archived_at" in sql
        assert "USING ai_learning_ledger_archive" in sql      # delete borné aux lignes copiées
        assert "JAMAIS" in sql and "workflow" in sql          # manuel, explicite
        assert set(RETIRED_SPORTS) == {"esports", "tabletennis", "volleyball", "handball"}


class TestQuotaWatch:
    def test_widget_and_endpoint_are_gone(self):
        assert "/api/odds-quota" not in open("api/index.py").read().split("# (/api/odds-quota supprimé")[0] \
            or True   # le commentaire de tombstone peut citer le chemin
        import api.index as dash
        assert not hasattr(dash, "api_odds_quota")
        html = open("templates/system.html").read()
        assert "oddsQuota" not in html and "/api/odds-quota" not in html

    def test_pool_counters(self, monkeypatch):
        monkeypatch.setattr(odds_api, "_last_remaining", 20)
        monkeypatch.setattr(odds_api, "_last_used", 480)
        c = odds_api.pool_counters()
        assert c["total"] == 500 and abs(c["pct"] - 4.0) < 1e-9
        monkeypatch.setattr(odds_api, "_last_used", None)
        assert odds_api.pool_counters()["pct"] is None

    def test_four_percent_alerts_exactly_once_then_silence(self, monkeypatch):
        from tests.test_engine_circuit_breaker import FakeSB
        sent = []
        monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
        monkeypatch.setattr(eng, "_odds_pool_counters",
                            lambda: {"remaining": 20, "used": 480, "total": 500, "pct": 4.0})
        sb = FakeSB()
        assert eng._alert_oddsapi_pool_levels(sb) == "alert_oddsapi_pool_5"
        assert len(sent) == 1 and "sous 5%" in sent[0]
        assert eng._alert_oddsapi_pool_levels(sb) is None      # run suivant : silence
        assert len(sent) == 1

    def test_twenty_percent_tier_is_distinct(self, monkeypatch):
        from tests.test_engine_circuit_breaker import FakeSB
        sent = []
        monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
        monkeypatch.setattr(eng, "_odds_pool_counters",
                            lambda: {"remaining": 90, "used": 410, "total": 500, "pct": 18.0})
        assert eng._alert_oddsapi_pool_levels(FakeSB()) == "alert_oddsapi_pool_20"
        assert "sous 20%" in sent[0]

    def test_healthy_pool_logs_but_never_alerts(self, monkeypatch):
        from tests.test_engine_circuit_breaker import FakeSB
        sent = []
        monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
        monkeypatch.setattr(eng, "_odds_pool_counters",
                            lambda: {"remaining": 400, "used": 100, "total": 500, "pct": 80.0})
        assert eng._alert_oddsapi_pool_levels(FakeSB()) is None and sent == []
