"""
tests/test_edge_frequency_audit.py — scripts/edge_frequency_audit.py: the
empirical edge amplitude x frequency audit, tested against synthetic
multi-day data (the real Supabase dataset as of 2026-07-08 has only 11
signals in a single 2.5h window and 1 permanent ledger row — nowhere near
enough to exercise this logic, which is exactly this audit's headline
finding; see reports/edge_frequency_audit.md).
"""
import pytest

from scripts.edge_frequency_audit import (
    edge_distribution,
    frequency_by_k,
    group_by_day,
    magnitude_by_k,
    normalize_ledger_row,
    normalize_signal_row,
    posthoc_validation,
    run_audit,
)


def _signal_row(date, sport, league, edge_pct, xbet_odd, pinnacle_price,
                match="A vs B", market_key="h2h", outcome=None, sharp_prob=None):
    return {
        "scanned_at": f"{date}T12:00:00+00:00", "sport": sport, "league": league,
        "edge_pct": edge_pct, "xbet_odd": xbet_odd, "pinnacle_price": pinnacle_price,
        "match": match, "market_key": market_key, "outcome": outcome, "sharp_prob": sharp_prob,
    }


def _rec(date, sport, league, true_prob, odds, edge_pct=None, outcome=None, match="A vs B", market="h2h"):
    """Build an already-normalized record directly (bypassing normalize_*)
    for tests that only care about the analysis functions, not parsing."""
    if edge_pct is None:
        fair_odds = 1 / true_prob
        edge_pct = (odds / fair_odds - 1) * 100
    return {
        "date": date, "sport": sport, "market": market, "league": league, "match": match,
        "edge_pct": edge_pct, "true_prob": true_prob, "odds": odds, "outcome": outcome,
        "correlation_group": f"{sport}:{date}:{league}",
        "source": "test",
    }


class TestNormalizeSignalRow:
    def test_basic_mapping(self):
        row = _signal_row("2026-07-01", "soccer", "MLS", 3.5, 2.10, 2.03)
        norm = normalize_signal_row(row)
        assert norm["date"] == "2026-07-01"
        assert norm["sport"] == "soccer"
        assert norm["correlation_group"] == "soccer:2026-07-01:MLS"
        assert norm["true_prob"] == pytest.approx(1 / 2.03)

    def test_uses_sharp_prob_when_present(self):
        row = _signal_row("2026-07-01", "soccer", "MLS", 3.5, 2.10, 2.03, sharp_prob=0.60)
        norm = normalize_signal_row(row)
        assert norm["true_prob"] == 0.60

    def test_missing_pinnacle_price_excluded(self):
        row = _signal_row("2026-07-01", "soccer", "MLS", 3.5, 2.10, 0.0)
        assert normalize_signal_row(row) is None

    def test_missing_edge_excluded(self):
        row = _signal_row("2026-07-01", "soccer", "MLS", None, 2.10, 2.03)
        assert normalize_signal_row(row) is None


class TestNormalizeLedgerRow:
    def test_derives_true_prob_from_edge_and_odds_without_sharp_prob(self):
        # edge=5%, odds=2.10 -> fair_odds = 2.10/1.05 = 2.0 -> true_prob=0.5
        row = {"initial_edge": 5.0, "odds": 2.10, "created_at": "2026-07-01T12:00:00", "sport": "soccer", "league": "MLS"}
        norm = normalize_ledger_row(row)
        assert norm["true_prob"] == pytest.approx(0.5, abs=1e-6)

    def test_uses_sharp_prob_when_present(self):
        row = {"initial_edge": 5.0, "odds": 2.10, "created_at": "2026-07-01T12:00:00",
               "sport": "soccer", "league": "MLS", "sharp_prob": 0.55}
        norm = normalize_ledger_row(row)
        assert norm["true_prob"] == 0.55

    def test_missing_odds_or_edge_excluded(self):
        assert normalize_ledger_row({"initial_edge": None, "odds": 2.0, "created_at": "2026-07-01"}) is None
        assert normalize_ledger_row({"initial_edge": 5.0, "odds": None, "created_at": "2026-07-01"}) is None


class TestEdgeDistribution:
    def test_grouups_by_sport_and_market_separately(self):
        records = [
            _rec("2026-07-01", "soccer", "MLS", 0.55, 2.0, edge_pct=3.0),
            _rec("2026-07-02", "soccer", "MLS", 0.55, 2.0, edge_pct=5.0),
            _rec("2026-07-01", "basketball", "NBA", 0.60, 1.8, edge_pct=2.0, market="totals"),
        ]
        dist = edge_distribution(records)
        assert dist["soccer:h2h"]["n"] == 2
        assert dist["soccer:h2h"]["mean"] == pytest.approx(4.0)
        assert dist["basketball:totals"]["n"] == 1

    def test_percentiles_present(self):
        records = [_rec("2026-07-01", "soccer", "MLS", 0.55, 2.0, edge_pct=e) for e in [1, 2, 3, 4, 5]]
        dist = edge_distribution(records)
        d = dist["soccer:h2h"]
        assert d["p10"] <= d["p50"] <= d["p90"]


class TestGroupByDay:
    def test_groups_correctly(self):
        records = [
            _rec("2026-07-01", "soccer", "MLS", 0.55, 2.0),
            _rec("2026-07-01", "basketball", "NBA", 0.55, 2.0),
            _rec("2026-07-02", "soccer", "MLS", 0.55, 2.0),
        ]
        by_day = group_by_day(records)
        assert set(by_day.keys()) == {"2026-07-01", "2026-07-02"}
        assert len(by_day["2026-07-01"]) == 2   # two distinct correlation groups that day


class TestFrequencyByK:
    def _make_day_with_n_groups(self, date, n_groups, true_prob=0.80, odds=None):
        """n independent correlation groups (different leagues), each with
        one leg at a comfortably-qualifying edge for small k (true_prob=0.80
        keeps min_edge_required(k) modest even at k up to ~6)."""
        fair_odds = 1 / true_prob
        odds = odds if odds is not None else fair_odds * 1.20   # ~20% raw edge
        return [_rec(date, "soccer", f"league{i}", true_prob, odds) for i in range(n_groups)]

    def test_more_days_qualify_at_lower_k(self):
        records = (
            self._make_day_with_n_groups("2026-07-01", 5)
            + self._make_day_with_n_groups("2026-07-02", 2)
        )
        by_day = group_by_day(records)
        freq = frequency_by_k(by_day, k_range=range(2, 6))
        by_k = {r["k"]: r for r in freq}
        # k=2: both days qualify (5>=2 and 2>=2) -> 2/2
        assert by_k[2]["valid_days"] == 2
        # k=5: only the 5-group day qualifies -> 1/2
        assert by_k[5]["valid_days"] == 1

    def test_zero_days_when_no_day_has_enough_groups(self):
        records = self._make_day_with_n_groups("2026-07-01", 1)
        by_day = group_by_day(records)
        freq = frequency_by_k(by_day, k_range=range(2, 4))
        assert all(r["valid_days"] == 0 for r in freq)

    def test_empty_input_returns_zero_ratio_not_crash(self):
        freq = frequency_by_k({}, k_range=range(2, 4))
        assert all(r["ratio"] == 0.0 and r["total_days"] == 0 for r in freq)

    def test_same_group_legs_dont_double_count_toward_k(self):
        # Two legs, SAME correlation group -> counts as only 1 toward k.
        records = [
            _rec("2026-07-01", "soccer", "MLS", 0.80, 1.20 / 0.80, match="A vs B"),
            _rec("2026-07-01", "soccer", "MLS", 0.80, 1.20 / 0.80, match="A vs B", market="totals"),
        ]
        by_day = group_by_day(records)
        freq = frequency_by_k(by_day, k_range=range(2, 3))
        assert freq[0]["valid_days"] == 0   # only 1 distinct group, need k=2


class TestMagnitudeByK:
    def test_no_valid_days_gives_zero_growth(self):
        records = [_rec("2026-07-01", "soccer", "MLS", 0.55, 1.60)]   # weak, single group
        by_day = group_by_day(records)
        mags = magnitude_by_k(by_day, k_range=range(2, 4))
        assert all(m["n_opportunities"] == 0 for m in mags)
        assert all(m["expected_monthly_log_growth"] == 0.0 for m in mags)

    def test_strong_multi_group_day_produces_positive_growth(self):
        records = [_rec("2026-07-01", "soccer", f"league{i}", 0.80, (1 / 0.80) * 1.30) for i in range(4)]
        by_day = group_by_day(records)
        mags = magnitude_by_k(by_day, k_range=range(2, 4))
        by_k = {m["k"]: m for m in mags}
        assert by_k[2]["n_opportunities"] == 1
        assert by_k[2]["avg_log_growth_per_opportunity"] > 0


class TestPosthocValidation:
    def test_insufficient_sample_reported_explicitly(self):
        records = [_rec("2026-07-01", "soccer", f"league{i}", 0.80, (1 / 0.80) * 1.30, outcome="WIN")
                  for i in range(3)]
        by_day = group_by_day(records)
        result = posthoc_validation(by_day, k_range=range(2, 3), min_samples=30)
        assert result[0]["sufficient"] is False
        assert "need >=" in result[0]["message"]

    def test_sufficient_sample_computes_wilson_ci(self):
        # 40 days, each with 2 qualifying groups that both WIN -> 40 decisive
        # 2-leg combos, all wins.
        records = []
        for d in range(40):
            date = f"2026-08-{d+1:02d}" if d < 31 else f"2026-09-{d-30:02d}"
            for i in range(2):
                records.append(_rec(date, "soccer", f"league{i}", 0.80, (1 / 0.80) * 1.30, outcome="WIN"))
        by_day = group_by_day(records)
        result = posthoc_validation(by_day, k_range=range(2, 3), min_samples=30)
        assert result[0]["sufficient"] is True
        assert result[0]["hit_rate"] == 1.0
        assert result[0]["wilson_lower"] > 0.5

    def test_unresolved_legs_excluded_from_decisive_count(self):
        records = [_rec("2026-07-01", "soccer", f"league{i}", 0.80, (1 / 0.80) * 1.30, outcome=None)
                  for i in range(2)]
        by_day = group_by_day(records)
        result = posthoc_validation(by_day, k_range=range(2, 3), min_samples=1)
        assert result[0]["decisive_combos"] == 0
        assert result[0]["sufficient"] is False


class TestRunAudit:
    def test_integration_smoke(self):
        records = [_rec("2026-07-01", "soccer", f"league{i}", 0.80, (1 / 0.80) * 1.30, outcome="WIN")
                  for i in range(3)]
        result = run_audit(records)
        assert result["n_records"] == 3
        assert result["n_days"] == 1
        assert "edge_distribution" in result
        assert "frequency_by_k" in result
        assert "magnitude_by_k" in result
        assert "posthoc_validation" in result

    def test_empty_records_does_not_crash(self):
        result = run_audit([])
        assert result["n_records"] == 0
        assert result["n_days"] == 0
