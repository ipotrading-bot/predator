"""
tests/test_odds_budget.py — rationnement du quota The Odds API.

Contexte mesuré le 2026-08-01 : plan à 500 requêtes/mois, consommation
~16 crédits/heure, quota du mois épuisé en ~30h — deux mois de suite. Le
garde précédent (`remaining < 50`) ne rationnait rien, il constatait la mort.

Ce qui est épinglé ici :
  - l'allocation est `restant / jours restants dans le mois`, donc elle
    remonte toute seule si l'opérateur passe sur un plan supérieur — aucun
    seuil en dur à revenir changer ;
  - un `remaining` inconnu n'interdit RIEN (une donnée manquante ne doit pas
    devenir une panne de pipeline) ;
  - la réserve dure n'est franchissable que par le tier `golden` ;
  - la dépense du jour est journalière : elle se réinitialise à la date UTC ;
  - `spend()` fait autorité sur l'en-tête `x-requests-remaining`, y compris
    quand un autre run a consommé entre-temps.
"""
from datetime import datetime, timezone

import pytest

import core.odds_budget as ob


JAN15 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)   # 17 jours restants
JAN31 = datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc)   # dernier jour


class TestAllowance:
    def test_days_left_includes_today(self):
        assert ob.days_left_in_cycle(JAN15) == 17
        assert ob.days_left_in_cycle(JAN31) == 1

    def test_allowance_spreads_remaining_over_the_month(self):
        # 300 restants − 12 de réserve = 288, sur 17 jours ≈ 16.9/jour
        assert ob.daily_allowance(300, JAN15) == pytest.approx(288 / 17, abs=0.01)

    def test_upgrade_lifts_the_cap_without_touching_the_code(self):
        # Le seul levier est `remaining` : un plan 20 000 desserre tout seul.
        small = ob.daily_allowance(300, JAN15)
        big   = ob.daily_allowance(19000, JAN15)
        assert big > small * 50

    def test_exhausted_quota_allows_nothing(self):
        assert ob.daily_allowance(0, JAN15) == 0.0
        assert ob.daily_allowance(None, JAN15) == 0.0


def _budget(tier="engine", remaining=None, spent_today=0, now=JAN15):
    state = {"date": now.date().isoformat(), "spent_today": spent_today,
             "remaining": remaining, "used": None}
    return ob.Budget(sb=None, tier=tier, state=state, now=now)


class TestPacing:
    def test_an_early_run_cannot_eat_the_whole_day(self):
        # golden_hour tourne 24x/jour : sans rythme, les trois premiers runs
        # videraient l'allocation et il ne resterait rien pour le prime-time.
        midnight = datetime(2026, 1, 15, 0, 30, tzinfo=timezone.utc)
        evening  = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
        assert _budget("golden", remaining=300, now=midnight).tier_cap() \
            < _budget("golden", remaining=300, now=evening).tier_cap()

    def test_a_late_run_inherits_what_was_not_spent(self):
        evening = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
        b = _budget("golden", remaining=300, spent_today=6, now=evening)
        assert b.can_spend(3) is True


class TestCanSpend:
    def test_unknown_remaining_never_blocks(self):
        # Premier run, colonne meta vide : on ne bride pas, le premier appel
        # payant renseignera le compteur.
        assert _budget(remaining=None).can_spend(3) is True

    def test_within_daily_allowance(self):
        # 300 restants / 17 jours ≈ 16.9 ; tier engine = 60% ≈ 10.2
        b = _budget("engine", remaining=300)
        assert b.can_spend(3) is True

    def test_beyond_tier_share_is_refused(self):
        b = _budget("engine", remaining=300, spent_today=9)
        assert b.can_spend(3) is False          # 9 + 3 > 10.2

    def test_golden_gets_the_full_allowance(self):
        # Même dépense, même quota, même heure : le tier prioritaire passe là
        # où le scan de fond et le deep scan sont bloqués.
        assert _budget("golden", remaining=300, spent_today=6).can_spend(3) is True
        assert _budget("engine", remaining=300, spent_today=6).can_spend(3) is False
        assert _budget("deep",   remaining=300, spent_today=6).can_spend(3) is False

    def test_run_local_spend_counts_against_the_cap(self):
        b = _budget("golden", remaining=300)
        assert b.can_spend(3) is True
        b.spend(3, remaining_header=297)
        b.spend(3, remaining_header=294)
        b.spend(3, remaining_header=291)
        b.spend(3, remaining_header=288)
        b.spend(3, remaining_header=285)        # 15 dépensés, plafond ≈ 16.9
        assert b.can_spend(3) is False

    def test_hard_reserve_is_golden_only(self):
        b_engine = _budget("engine", remaining=14)
        b_golden = _budget("golden", remaining=14)
        assert b_engine.can_spend(3) is False
        assert b_golden.can_spend(3) is True

    def test_nothing_spends_the_last_credit(self):
        assert _budget("golden", remaining=3).can_spend(3) is False

    def test_last_day_of_month_can_spend_everything(self):
        # 17 jours de moins à couvrir : le même quota devient dépensable.
        assert _budget("engine", remaining=300, spent_today=9, now=JAN31).can_spend(3) is True


class TestState:
    def test_headers_override_local_arithmetic(self):
        # Un autre run a consommé pendant ce scan : l'en-tête fait foi.
        b = _budget("engine", remaining=300)
        b.spend(3, remaining_header=120)
        assert b.remaining == 120

    def test_spend_without_header_decrements_locally(self):
        b = _budget("engine", remaining=300)
        b.spend(3)
        assert b.remaining == 297

    def test_note_headers_records_without_spending(self):
        b = _budget("engine", remaining=300)
        b.note_headers(250, 250)
        assert (b.remaining, b.spent_this_run) == (250, 0)

    def test_new_day_resets_spend_but_keeps_remaining(self, monkeypatch):
        stored = {"date": "2026-01-14", "spent_today": 40, "remaining": 260, "used": 240}

        class _SB:
            def table(self, _n): return self
            def select(self, *_a, **_k): return self
            def eq(self, *_a, **_k): return self
            def limit(self, *_a, **_k): return self
            def execute(self):
                import json
                return type("R", (), {"data": [{"value": json.dumps(stored)}]})()

        state = ob.load_state(_SB(), JAN15)
        assert state["spent_today"] == 0        # nouveau jour
        assert state["remaining"] == 260        # mémoire du quota conservée

    def test_missing_db_is_not_an_error(self):
        state = ob.load_state(None, JAN15)
        assert state["spent_today"] == 0 and state["remaining"] is None
        ob.save_state(None, state)              # ne doit pas lever

    def test_unknown_tier_falls_back_to_default(self):
        assert ob.Budget(None, "n_importe_quoi", {}, JAN15).tier == ob.DEFAULT_TIER
