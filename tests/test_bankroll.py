"""Bankroll mensuelle et cadencement des mises (core/bankroll.py,
core/bank_view.py) — décision opérateur du 2026-09-09.

Invariants tenus ici :
  · budget du jour = restant ÷ jours restants (aujourd'hui compris) ;
  · réparti au prorata du Kelly contre le Kelly quotidien attendu, jamais
    au-delà du disponible du jour, Kelly forts servis d'abord ;
  · mise arrondie au billet (100 F), plancher 500 F, zéro si rien ;
  · PUSH et expiré rendent leur mise ; WIN rapporte mise × cote ;
  · les lignes sans `stake_xof` sont reconstituées (jour plat ÷ Kelly du
    jour) et MARQUÉES ;
  · un signal déjà actif reprend sa mise figée ;
  · la vue /bank additionne juste (restant + dépensé + engagé = bankroll).
"""
from datetime import datetime, timezone

import pytest

from core import bankroll as bk
from core.bank_view import build
from core.constants import (BANKROLL_DEFAULT_DAY_KELLY, BANKROLL_MONTHLY_XOF,
                            STAKE_MIN_XOF, STAKE_ROUND_XOF)

NOW = datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc)


def _sig(k, mid="m1", mk="h2h", **kw):
    return {"kelly_pct": k, "match_id": mid, "market_key": mk, **kw}


class TestCalendrier:
    def test_jours_restants_aujourdhui_compris(self):
        assert bk.days_left(NOW) == 22               # 9 → 30 septembre
        assert bk.days_left(datetime(2026, 9, 30, tzinfo=timezone.utc)) == 1
        assert bk.days_in_month(datetime(2026, 2, 1, tzinfo=timezone.utc)) == 28

    def test_bornes_du_mois(self):
        assert bk.month_start_iso(NOW) == "2026-09-01T00:00:00+00:00"
        assert bk.next_month_start_iso(NOW) == "2026-10-01T00:00:00+00:00"
        assert bk.next_month_start_iso(datetime(2026, 12, 5, tzinfo=timezone.utc)) == "2027-01-01T00:00:00+00:00"


class TestArrondi:
    def test_billet_et_plancher(self):
        assert bk.round_stake(0) == 0 and bk.round_stake(-5) == 0
        assert bk.round_stake(120) == STAKE_MIN_XOF
        assert bk.round_stake(1_249) == 1_200 and bk.round_stake(1_250) == 1_300
        assert bk.round_stake(5_000) % STAKE_ROUND_XOF == 0

    def test_resultat_et_retour(self):
        win = {"outcome": "WIN", "stake_xof": 1000, "odds": 1.9}
        loss = {"outcome": "LOSS", "stake_xof": 1000, "odds": 1.9}
        push = {"outcome": "PUSH", "stake_xof": 1000, "odds": 1.9}
        exp = {"outcome": "expired", "stake_xof": 1000, "odds": 1.9}
        assert bk.gross_return(win) == pytest.approx(1900) and bk.net_result(win) == pytest.approx(900)
        assert bk.net_result(loss) == -1000 and bk.stake_committed(loss) == 1000
        assert bk.net_result(push) == 0 and bk.stake_committed(push) == 0 and bk.gross_return(push) == 1000
        assert bk.net_result(exp) == 0 and bk.stake_committed(exp) == 0


class TestBudgetDuJour:
    def test_restant_divise_par_jours_restants(self):
        ctx = bk.BankContext(now=NOW, spent=20_000, engaged_before_today=2_000)
        assert ctx.remaining_before_today == 78_000
        assert ctx.budget_today == pytest.approx(78_000 / 22)

    def test_dernier_jour_engage_tout_le_restant(self):
        ctx = bk.BankContext(now=datetime(2026, 9, 30, tzinfo=timezone.utc), spent=90_000)
        assert ctx.budget_today == pytest.approx(10_000)

    def test_deja_engage_aujourdhui_reduit_le_disponible(self):
        ctx = bk.BankContext(now=NOW, engaged_today=3_000)
        assert ctx.available_today == pytest.approx(ctx.budget_today - 3_000)


class TestRepartitionAuProrataDuKelly:
    def test_proportionnelle_au_kelly_contre_le_kelly_attendu(self):
        ctx = bk.BankContext(now=NOW, kelly_day_expected=3.0)       # budget ≈ 4 545 F
        sigs = [_sig(1.5, "a"), _sig(0.5, "b")]
        mises = bk.assign_stakes(sigs, ctx)
        assert mises[0] == bk.round_stake(ctx.budget_today * 1.5 / 3.0)
        assert mises[1] == bk.round_stake(ctx.budget_today * 0.5 / 3.0)
        assert sigs[0]["stake_xof"] == mises[0] and sigs[1]["stake_xof"] == mises[1]
        assert ctx.engaged_today == sum(mises) and ctx.kelly_today == pytest.approx(2.0)

    def test_un_jour_plus_fourni_que_prevu_nexcede_pas_son_budget(self):
        ctx = bk.BankContext(now=NOW, kelly_day_expected=1.0)
        sigs = [_sig(1.0, f"m{i}") for i in range(6)]
        mises = bk.assign_stakes(sigs, ctx)
        assert sum(mises) <= ctx.budget_today + STAKE_ROUND_XOF   # arrondi au billet, jamais plus
        assert all(m > 0 for m in mises)

    def test_les_kelly_forts_sont_servis_dabord_quand_le_budget_manque(self):
        ctx = bk.BankContext(now=NOW, spent=99_000, kelly_day_expected=1.0)   # ≈ 45 F de budget
        sigs = [_sig(0.2, "faible"), _sig(2.0, "fort")]
        mises = bk.assign_stakes(sigs, ctx)
        assert mises == [0, 0] or (mises[1] >= mises[0])

    def test_un_signal_deja_actif_reprend_sa_mise_figee(self):
        ctx = bk.BankContext(now=NOW, active_stakes={("m1", "h2h"): 700}, engaged_today=700, kelly_today=0.7)
        sigs = [_sig(0.7, "m1", "h2h"), _sig(0.7, "m2", "h2h")]
        mises = bk.assign_stakes(sigs, ctx)
        assert mises[0] == 700 and sigs[0]["stake_xof"] == 700
        assert mises[1] > 0
        assert ctx.engaged_today == 700 + mises[1]          # le premier n'est pas recompté

    def test_sans_signal_rien(self):
        assert bk.assign_stakes([], bk.BankContext(now=NOW)) == []


class TestKellyQuotidienAttendu:
    def test_moyenne_sur_la_fenetre_jours_vides_compris(self):
        hist = [{"created_at": "2026-09-01T10:00:00+00:00", "kelly_pct": 1.0},
                {"created_at": "2026-09-01T12:00:00+00:00", "kelly_pct": 1.0},
                {"created_at": "2026-09-05T10:00:00+00:00", "kelly_pct": 2.0},
                {"created_at": "2026-09-09T10:00:00+00:00", "kelly_pct": 9.0},   # aujourd'hui : exclu
                {"created_at": "2026-09-03T10:00:00+00:00", "kelly_pct": 5.0, "is_shadow": True}]
        assert bk.expected_daily_kelly(hist, NOW, lookback_days=14) == pytest.approx(4.0 / 14)

    def test_sans_historique_valeur_par_defaut(self):
        assert bk.expected_daily_kelly([], NOW) == BANKROLL_DEFAULT_DAY_KELLY


class TestReconstitution:
    def test_jour_plat_reparti_au_kelly_et_marque(self):
        rows = [{"created_at": "2026-09-02T10:00:00+00:00", "kelly_pct": 1.0},
                {"created_at": "2026-09-02T11:00:00+00:00", "kelly_pct": 3.0},
                {"created_at": "2026-09-03T11:00:00+00:00", "kelly_pct": 0.0},
                {"created_at": "2026-09-03T12:00:00+00:00", "kelly_pct": None, "stake_xof": 900}]
        out = bk.with_stakes(rows, 30_000, 30)          # 1 000 F par jour
        assert [r["stake_xof"] for r in out] == [bk.round_stake(250), bk.round_stake(750), 1_000, 900]
        assert [r["stake_reconstituee"] for r in out] == [True, True, True, False]


class TestVueBank:
    def _rows(self):
        return [
            {"created_at": "2026-09-02T10:00:00+00:00", "outcome": "WIN", "odds": 2.0, "stake_xof": 1000, "kelly_pct": 1,
             "match": "A vs B", "selection": "A", "sport": "soccer", "time_to_match_minutes": 300},
            {"created_at": "2026-09-02T12:00:00+00:00", "outcome": "LOSS", "odds": 1.8, "stake_xof": 500, "kelly_pct": 1,
             "match": "C vs D", "selection": "C", "sport": "tennis", "time_to_match_minutes": 300},
            {"created_at": "2026-09-05T12:00:00+00:00", "outcome": "PUSH", "odds": 1.9, "stake_xof": 700, "kelly_pct": 1,
             "match": "E vs F", "selection": "E", "sport": "soccer", "time_to_match_minutes": 300},
            {"created_at": "2026-09-06T12:00:00+00:00", "outcome": "LOSS", "odds": 1.9, "stake_xof": 800, "kelly_pct": 1,
             "match": "G vs H", "selection": "G", "sport": "soccer", "is_shadow": True},   # fantôme : hors bankroll
        ]

    def test_resume_et_serie_additionnent_juste(self):
        actifs = [{"created_at": "2026-09-09T08:00:00+00:00", "stake_xof": 600, "kelly_pct": 0.6,
                   "match": "I vs J", "selection_name": "I", "sport": "soccer", "xbet_odd": 1.95,
                   "time_to_match_minutes": 400}]
        v = build(self._rows(), actifs, NOW, "2026-09", BANKROLL_MONTHLY_XOF)
        r = v["resume"]
        assert r["depense"] == 1500 and r["engage"] == 600
        assert r["restant"] + r["depense"] + r["engage"] == BANKROLL_MONTHLY_XOF
        assert r["retours"] == 2000 and r["resultat"] == 500
        assert r["n_regles"] == 3 and r["n_gagnes"] == 1 and r["n_perdus"] == 1 and r["n_rendus"] == 1
        assert r["n_en_cours"] == 1 and r["reconstituees"] == 0
        assert r["budget_jour"] == round((BANKROLL_MONTHLY_XOF - 1500) / 22)
        assert r["budget_jour_dispo"] == r["budget_jour"] - 600
        jours = v["jours"]
        assert [d["jour"] for d in jours] == ["2026-09-02", "2026-09-05"]
        assert jours[0]["mise"] == 1500 and jours[0]["retours"] == 2000 and jours[0]["resultat"] == 500
        assert jours[0]["restant"] == BANKROLL_MONTHLY_XOF - 1500
        assert jours[1]["rendus"] == 1 and jours[1]["mise"] == 0 and jours[1]["cumul"] == 500
        assert v["paris"][0]["outcome"] == "EN COURS" and v["paris"][0]["mise"] == 600

    def test_un_mois_passe_na_ni_engage_ni_budget(self):
        v = build(self._rows(), [], datetime(2026, 10, 3, tzinfo=timezone.utc), "2026-09")
        r = v["resume"]
        assert not r["courant"] and r["engage"] == 0 and r["jours_restants"] == 0 and r["budget_jour"] == 0
        assert r["solde_fin"] == BANKROLL_MONTHLY_XOF + 500

    def test_les_lignes_sans_mise_sont_reconstituees_et_comptees(self):
        rows = [dict(r) for r in self._rows()[:2]]
        for r in rows:
            r.pop("stake_xof")
        v = build(rows, [], NOW, "2026-09")
        assert v["resume"]["reconstituees"] == 2
        assert all(p["reconstituee"] for p in v["paris"])
        assert v["resume"]["depense"] > 0
