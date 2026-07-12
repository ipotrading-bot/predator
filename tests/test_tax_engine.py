"""
tests/test_tax_engine.py — core/tax_engine.py math sanity checks.

core/tax_engine.py was authored from the task description only — the real
tax_engine.py + spec docs it was meant to be copied from are not in this
repo (checked working tree, git log, git stash). These tests exist to
verify the derived formulas are internally consistent (breakeven really is
breakeven, more legs really do require more edge, the numeric Kelly optimum
matches the closed-form Kelly criterion at tax_rate=0), not to assert
against a reference implementation that doesn't exist here.
"""
import pytest

from core.tax_engine import (
    DEFAULT_TAX_RATE,
    is_combo_tax_viable,
    min_edge_required,
    net_b,
    net_return_on_win,
    optimal_stake_fraction,
    single_bet_ev_net_tax,
    suggest_system,
    system_expected_value,
)
from core.paim_engine import correlation_group


class TestNetReturn:
    def test_no_tax_returns_full_payout(self):
        assert net_return_on_win(100, 2.0, tax_rate=0.0) == pytest.approx(200.0)

    def test_tax_only_bites_the_profit_not_the_stake(self):
        # stake=100, odds=2.0 -> profit=100, taxed at 20% -> net profit 80
        # net_return = stake(100) + 80 = 180, NOT (100*2.0)*(1-0.2)=160
        assert net_return_on_win(100, 2.0, tax_rate=0.20) == pytest.approx(180.0)

    def test_losing_bet_is_not_this_functions_concern(self):
        # net_return_on_win only models the win branch; a loss is simply -stake,
        # handled by single_bet_ev_net_tax below.
        assert net_b(1.5, tax_rate=0.20) == pytest.approx(0.5 * 0.8)


class TestSingleBetEV:
    def test_ev_zero_at_the_derived_breakeven_edge(self):
        # min_edge_required(k=1, p) should make single_bet_ev_net_tax ~0
        # at odds = fair_odds * (1 + edge).
        p = 0.55
        edge_pct = min_edge_required(k=1, true_prob=p, tax_rate=0.20)
        fair_odds = 1 / p
        odds = fair_odds * (1 + edge_pct / 100)
        ev = single_bet_ev_net_tax(stake=100, odds=odds, true_prob=p, tax_rate=0.20)
        assert ev == pytest.approx(0.0, abs=0.05)

    def test_ev_positive_above_breakeven_edge(self):
        p = 0.55
        edge_pct = min_edge_required(k=1, true_prob=p, tax_rate=0.20)
        fair_odds = 1 / p
        odds = fair_odds * (1 + (edge_pct + 2) / 100)  # +2pp above breakeven
        ev = single_bet_ev_net_tax(100, odds, p, tax_rate=0.20)
        assert ev > 0

    def test_ev_negative_below_breakeven_edge(self):
        p = 0.55
        edge_pct = min_edge_required(k=1, true_prob=p, tax_rate=0.20)
        fair_odds = 1 / p
        odds = fair_odds * (1 + (edge_pct - 2) / 100)  # -2pp below breakeven
        ev = single_bet_ev_net_tax(100, odds, p, tax_rate=0.20)
        assert ev < 0

    def test_zero_tax_matches_untaxed_ev(self):
        # At tax_rate=0, EV should match the standard (odds*p - 1)*stake formula.
        stake, odds, p = 100, 2.10, 0.55
        ev = single_bet_ev_net_tax(stake, odds, p, tax_rate=0.0)
        expected = stake * (p * odds - 1)
        assert ev == pytest.approx(expected)

    def test_invalid_inputs_return_zero(self):
        assert single_bet_ev_net_tax(0, 2.0, 0.5) == 0.0
        assert single_bet_ev_net_tax(100, 1.0, 0.5) == 0.0
        assert single_bet_ev_net_tax(100, 2.0, 1.5) == 0.0


class TestMinEdgeRequired:
    def test_per_leg_edge_shrinks_but_total_combo_edge_grows_with_k(self):
        # Counter-intuitive at first glance, but verified against direct EV:
        # tax is withheld ONCE on the combo's final net profit, not per leg.
        # A small edge compounds multiplicatively over k legs — (1+e)**k —
        # so a smaller PER-LEG edge is enough to build up the larger TOTAL
        # combo-edge cushion a bigger, fixed-rate tax bite requires as
        # combined_p = true_prob**k shrinks. Confirmed via EV: both a k=1 bet
        # at its min_edge_required and a k=4 combo at its (smaller) per-leg
        # min_edge_required land at combo EV ~= 0.
        edges = [min_edge_required(k=k, true_prob=0.55) for k in range(1, 5)]
        assert edges == sorted(edges, reverse=True)   # per-leg requirement: decreasing
        assert edges[0] > edges[-1]

        total_combo_edges = [(1 + e / 100) ** k - 1 for k, e in enumerate(edges, start=1)]
        assert total_combo_edges == sorted(total_combo_edges)   # total requirement: increasing

    def test_higher_true_prob_needs_less_edge_at_k1(self):
        # Favorites need less edge to clear the tax bar than live underdogs
        # do, at the same tax rate — tax bites the (larger) underdog payout harder.
        e_fav = min_edge_required(k=1, true_prob=0.80)
        e_dog = min_edge_required(k=1, true_prob=0.30)
        assert e_fav < e_dog

    def test_zero_tax_requires_zero_edge(self):
        assert min_edge_required(k=1, true_prob=0.55, tax_rate=0.0) == pytest.approx(0.0, abs=1e-9)

    def test_rejects_invalid_k(self):
        with pytest.raises(ValueError):
            min_edge_required(k=0)

    def test_rejects_invalid_prob(self):
        with pytest.raises(ValueError):
            min_edge_required(true_prob=1.5)


class TestOptimalStakeFraction:
    def test_matches_closed_form_kelly_at_zero_tax(self):
        p, odds = 0.55, 2.10
        b = odds - 1
        closed_form = (p * b - (1 - p)) / b
        numeric = optimal_stake_fraction(p, odds, tax_rate=0.0, kelly_multiplier=1.0)
        assert numeric == pytest.approx(closed_form, abs=1e-3)

    def test_kelly_multiplier_scales_linearly(self):
        p, odds = 0.55, 2.10
        full = optimal_stake_fraction(p, odds, tax_rate=0.20, kelly_multiplier=1.0)
        half = optimal_stake_fraction(p, odds, tax_rate=0.20, kelly_multiplier=0.5)
        assert half == pytest.approx(full * 0.5, abs=1e-3)

    def test_negative_edge_yields_zero_stake(self):
        # true_prob too low for these odds net of tax -> Kelly says don't bet.
        # (The bounded optimizer settles near, not exactly at, f=0 for a
        # negative-edge input — optimal_stake_fraction clamps that solver
        # tolerance to a clean zero rather than leaking a dust-sized stake.)
        assert optimal_stake_fraction(0.3, 1.5, tax_rate=0.20) == 0.0

    def test_tax_shrinks_the_optimal_fraction(self):
        p, odds = 0.60, 2.20
        untaxed = optimal_stake_fraction(p, odds, tax_rate=0.0)
        taxed = optimal_stake_fraction(p, odds, tax_rate=0.20)
        assert taxed < untaxed


class TestComboViability:
    def _leg(self, prob, odds):
        return {"true_prob": prob, "odds": odds}

    def test_two_strong_legs_combine_viable(self):
        legs = [self._leg(0.65, 1.75), self._leg(0.65, 1.75)]
        assert is_combo_tax_viable(legs, tax_rate=0.20) is True

    def test_weak_leg_can_break_an_otherwise_viable_combo(self):
        strong = self._leg(0.70, 1.55)   # fair odds ~1.43, thin edge
        weak = self._leg(0.40, 1.55)     # priced far below fair (~2.5) -> bad leg
        assert is_combo_tax_viable([strong, weak], tax_rate=0.20) is False

    def test_empty_combo_not_viable(self):
        assert is_combo_tax_viable([], tax_rate=0.20) is False

    def test_system_expected_value_matches_is_combo_tax_viable(self):
        legs = [self._leg(0.65, 1.75), self._leg(0.65, 1.75)]
        result = system_expected_value(legs, stake=100, tax_rate=0.20)
        assert result["viable"] == is_combo_tax_viable(legs, tax_rate=0.20)
        assert result["combined_prob"] == pytest.approx(0.65 * 0.65)
        assert result["combined_odds"] == pytest.approx(1.75 * 1.75)


class TestSuggestSystem:
    def _sig(self, match, prob, odds):
        return {"match": match, "sharp_prob": prob, "xbet_odd": odds}

    def test_empty_signals_returns_none(self):
        assert suggest_system([], bankroll=150) is None

    def test_no_viable_combo_returns_none(self):
        # Both legs priced at/under fair value -> no edge anywhere.
        signals = [self._sig("A vs B", 0.60, 1.60), self._sig("C vs D", 0.60, 1.60)]
        assert suggest_system(signals, bankroll=150, tax_rate=0.20) is None

    def test_strong_signals_produce_a_positive_ev_system(self):
        signals = [
            self._sig("A vs B", 0.65, 1.80),
            self._sig("C vs D", 0.65, 1.80),
            self._sig("E vs F", 0.65, 1.80),
        ]
        result = suggest_system(signals, bankroll=150, tax_rate=0.20)
        assert result is not None
        assert result["ev"] > 0
        assert result["stake"] > 0
        assert 1 <= result["k"] <= 3

    def test_stake_never_exceeds_the_dashboard_kelly_pct_ceiling(self):
        # Sizing-base unification (2026-07-11, operator decision: dashboard
        # is canonical — see core/risk_manager.py's module docstring). Two
        # legs whose combined bet is comfortably tax-viable and whose
        # UNCAPPED tax-engine-optimal stake would be well above the sum of
        # their own persisted kelly_pct (each leg's dashboard-displayed
        # solo stake, set here artificially low to force the cap to bind) —
        # the system must never recommend more than that dashboard sum.
        sig_a = {**self._sig("A vs B", 0.65, 1.80), "kelly_pct": 1.0}
        sig_b = {**self._sig("C vs D", 0.65, 1.80), "kelly_pct": 1.0}
        bankroll = 150

        # Sanity: confirm the pre-fix (uncapped) tax-engine-optimal stake on
        # this exact combo really would exceed the dashboard ceiling, so a
        # passing test below proves the cap engaged rather than coincided.
        uncapped_frac = optimal_stake_fraction(0.65 * 0.65, 1.80 * 1.80, tax_rate=0.20, kelly_multiplier=1.0)
        dashboard_cap = (sig_a["kelly_pct"] + sig_b["kelly_pct"]) / 100 * bankroll
        assert uncapped_frac * bankroll > dashboard_cap

        result = suggest_system([sig_a, sig_b], bankroll=bankroll, tax_rate=0.20)

        assert result is not None
        assert result["k"] == 2
        assert result["stake"] <= dashboard_cap + 1e-6

    def test_missing_kelly_pct_falls_back_to_tax_engine_optimal(self):
        # Legacy signals / direct suggest_system() callers (e.g. every other
        # test in this class) never set kelly_pct — there's no dashboard
        # figure to reconcile against, so the pre-unification behavior
        # (tax-engine-optimal stake alone) must still apply unchanged.
        signals = [self._sig("A vs B", 0.65, 1.80), self._sig("C vs D", 0.65, 1.80)]
        result = suggest_system(signals, bankroll=150, tax_rate=0.20)
        assert result is not None
        uncapped_frac = optimal_stake_fraction(0.65 * 0.65, 1.80 * 1.80, tax_rate=0.20, kelly_multiplier=1.0)
        assert result["stake"] == pytest.approx(round(uncapped_frac * 150, 2))

    def test_signals_missing_odds_or_prob_are_skipped_not_fatal(self):
        signals = [
            {"match": "A vs B", "sharp_prob": None, "xbet_odd": 1.8},
            self._sig("C vs D", 0.65, 1.80),
        ]
        # Should not raise, and should still find the one usable leg viable.
        result = suggest_system(signals, bankroll=150, tax_rate=0.20)
        assert result is None or result["k"] == 1

    def test_never_combines_two_legs_from_the_same_correlation_group(self):
        # Two markets on the same match share a correlation_group by
        # construction (core.paim_engine.correlation_group) — even though
        # each leg alone is strong, suggest_system() must never bundle them
        # together in the default "forbid" mode.
        grp = correlation_group("soccer", "MLS", "2026-07-10T20:00:00+00:00")
        same_match_legs = [
            {**self._sig("A vs B", 0.65, 1.80), "correlation_group": grp},
            {**self._sig("A vs B", 0.65, 1.80), "correlation_group": grp},   # different market, same match
        ]
        independent_leg = {**self._sig("C vs D", 0.65, 1.80),
                           "correlation_group": correlation_group("soccer", "NBA", "2026-07-11T01:00:00+00:00")}

        result = suggest_system(same_match_legs + [independent_leg], bankroll=150, tax_rate=0.20)

        assert result is not None
        matches_used = {leg["match"] for leg in result["legs"]}
        # Never both same-match legs together — at most one of them, possibly
        # combined with the independent leg from a different match.
        assert not ({"A vs B"} <= matches_used and len([leg for leg in result["legs"] if leg["match"] == "A vs B"]) > 1)


class TestCorrelation:
    def _leg(self, prob, odds, group=None):
        return {"true_prob": prob, "odds": odds, "correlation_group": group}

    def test_forbid_mode_rejects_combo_sharing_a_group(self):
        legs = [self._leg(0.65, 1.75, group="g1"), self._leg(0.65, 1.75, group="g1")]
        assert is_combo_tax_viable(legs, tax_rate=0.20, correlation_mode="forbid") is False

    def test_forbid_mode_allows_combo_with_distinct_groups(self):
        legs = [self._leg(0.65, 1.75, group="g1"), self._leg(0.65, 1.75, group="g2")]
        assert is_combo_tax_viable(legs, tax_rate=0.20, correlation_mode="forbid") is True

    def test_discount_mode_reduces_combined_probability_vs_naive_independence(self):
        legs = [self._leg(0.65, 1.75, group="g1"), self._leg(0.65, 1.75, group="g1")]
        naive_p = 0.65 * 0.65
        result = system_expected_value(legs, stake=100, tax_rate=0.20,
                                       correlation_mode="discount", rho=0.20)
        assert result["combined_prob"] < naive_p

    def test_discount_mode_recovers_independence_at_rho_zero(self):
        legs = [self._leg(0.65, 1.75, group="g1"), self._leg(0.65, 1.75, group="g1")]
        naive_p = 0.65 * 0.65
        result = system_expected_value(legs, stake=100, tax_rate=0.20,
                                       correlation_mode="discount", rho=0.0)
        assert result["combined_prob"] == pytest.approx(naive_p, abs=1e-6)

    def test_higher_rho_discounts_more(self):
        legs_lo = [self._leg(0.65, 1.75, group="g1"), self._leg(0.65, 1.75, group="g1")]
        legs_hi = [self._leg(0.65, 1.75, group="g1"), self._leg(0.65, 1.75, group="g1")]
        r_lo = system_expected_value(legs_lo, stake=100, tax_rate=0.20, correlation_mode="discount", rho=0.15)
        r_hi = system_expected_value(legs_hi, stake=100, tax_rate=0.20, correlation_mode="discount", rho=0.30)
        assert r_hi["combined_prob"] < r_lo["combined_prob"]

    def test_no_shared_group_unaffected_by_mode(self):
        legs = [self._leg(0.65, 1.75, group="g1"), self._leg(0.65, 1.75, group="g2")]
        forbid = system_expected_value(legs, stake=100, tax_rate=0.20, correlation_mode="forbid")
        discount = system_expected_value(legs, stake=100, tax_rate=0.20, correlation_mode="discount")
        assert forbid["viable"] is True
        assert forbid["combined_prob"] == pytest.approx(discount["combined_prob"])


class TestCorrelationGroupTag:
    def test_same_match_different_market_shares_a_group(self):
        # Two markets (h2h, totals) on the same match, sport, league, date.
        g1 = correlation_group("soccer", "MLS", "2026-07-10T20:00:00+00:00")
        g2 = correlation_group("soccer", "MLS", "2026-07-10T20:00:00+00:00")
        assert g1 == g2

    def test_different_league_or_date_yields_different_group(self):
        base = correlation_group("soccer", "MLS", "2026-07-10T20:00:00+00:00")
        other_league = correlation_group("soccer", "Brasileirao", "2026-07-10T20:00:00+00:00")
        other_date = correlation_group("soccer", "MLS", "2026-07-11T20:00:00+00:00")
        assert base != other_league
        assert base != other_date

    def test_missing_match_time_does_not_raise(self):
        assert correlation_group("soccer", "MLS", "") == "soccer::MLS"
