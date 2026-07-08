"""
tests/test_learning_layer.py — core/learning_layer.py threshold adjustment.

Regression guard for the tautology bug: compute_and_save() used to derive
hit_rate from `clv_final >= 0`. For rows produced by core/settlement.py's
settle_signal(), clv_final is a re-derivation of the entry edge from the
exact same scan-time prices already used for edge_pct — since MIN_EDGE only
ever lets positive-edge signals through in the first place, clv_final is
~always >= 0 no matter what actually happened in the match. A batch of
100% real LOSS outcomes could still show ~100% "hit rate" under the old
code. These tests fail on the pre-fix code (both batches raise the
threshold, or WIN/LOSS produce identical adjustments) and pass once
hit_rate is computed from the real `outcome` column instead.
"""
from core.learning_layer import (
    SPORT_DEFAULTS,
    _MIN_SAMPLES,
    _sport_stats,
    compute_and_save,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _LedgerQuery:
    """Chainable stub for sb.table("ai_learning_ledger").select(...).eq(...)
    .order(...).limit(...).execute() — returns the rows pre-loaded for
    whichever sport .eq("sport", ...) selects."""

    def __init__(self, rows_by_sport: dict):
        self._rows_by_sport = rows_by_sport
        self._sport = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, value):
        if col == "sport":
            self._sport = value
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _Result(self._rows_by_sport.get(self._sport, []))


class _MetaTable:
    def __init__(self, writes: list):
        self._writes = writes

    def upsert(self, payload, **_k):
        self._writes.append(payload)
        return self

    def execute(self):
        return _Result(None)

    def select(self, *_a, **_k):
        # load_thresholds() also queries `meta` — return no rows so
        # SPORT_DEFAULTS is used as the starting point for every test.
        return _EmptyMetaSelect()


class _EmptyMetaSelect:
    def like(self, *_a, **_k):
        return self

    def execute(self):
        return _Result([])


class _FakeSupabase:
    def __init__(self, rows_by_sport: dict):
        self._rows_by_sport = rows_by_sport
        self.meta_writes: list = []

    def table(self, name):
        if name == "ai_learning_ledger":
            return _LedgerQuery(self._rows_by_sport)
        if name == "meta":
            return _MetaTable(self.meta_writes)
        raise AssertionError(f"unexpected table: {name}")


def _row(outcome, kelly_pct=10.0, odds=2.0, clv_final=5.0):
    # clv_final is deliberately positive on every row regardless of outcome
    # — reproducing the exact incident this guards against: entry-edge-as-
    # CLV is ~always >= 0 (MIN_EDGE already rejected negative edges before
    # the signal was sent), so a 100%-LOSS batch would still look like a
    # 100% "hit rate" if anything ever read clv_final again instead of
    # outcome.
    return {"outcome": outcome, "kelly_pct": kelly_pct, "odds": odds, "clv_final": clv_final}


class TestSportStats:
    """Pure-function unit tests for the outcome-driven stats helper."""

    def test_all_wins_full_hit_rate(self):
        stats = _sport_stats([_row("WIN") for _ in range(5)])
        assert stats["hit_rate"] == 1.0
        assert stats["n"] == 5

    def test_all_losses_zero_hit_rate(self):
        stats = _sport_stats([_row("LOSS") for _ in range(5)])
        assert stats["hit_rate"] == 0.0

    def test_push_and_unknown_excluded_from_denominator(self):
        rows = [_row("WIN"), _row("WIN"), _row("PUSH"), _row("UNKNOWN")]
        stats = _sport_stats(rows)
        assert stats["n"] == 2          # only the two WINs are decisive
        assert stats["hit_rate"] == 1.0

    def test_closed_and_expired_excluded(self):
        # These outcomes come from the CLV-only audit fallback, never a
        # real settled result — they must not masquerade as wins.
        rows = [_row("closed"), _row("expired"), _row("WIN")]
        stats = _sport_stats(rows)
        assert stats["n"] == 1

    def test_roi_positive_when_wins_outweigh_losses(self):
        rows = [_row("WIN", kelly_pct=10, odds=3.0)] * 3 + [_row("LOSS", kelly_pct=10, odds=3.0)]
        stats = _sport_stats(rows)
        assert stats["roi"] > 0

    def test_roi_negative_when_losses_outweigh_wins(self):
        rows = [_row("LOSS", kelly_pct=10, odds=1.5)] * 3 + [_row("WIN", kelly_pct=10, odds=1.5)]
        stats = _sport_stats(rows)
        assert stats["roi"] < 0

    def test_roi_none_without_stake_data(self):
        rows = [{"outcome": "WIN", "kelly_pct": None, "odds": 2.0}]
        stats = _sport_stats(rows)
        assert stats["roi"] is None

    def test_empty_batch(self):
        stats = _sport_stats([])
        assert stats["n"] == 0
        assert stats["hit_rate"] is None
        assert stats["roi"] is None
        assert stats["wilson_lower"] is None
        assert stats["p_breakeven"] is None


class TestComputeAndSaveRealOutcome:
    """Integration tests through compute_and_save()'s Supabase-facing path."""

    def test_all_losses_raises_threshold(self):
        rows = [_row("LOSS") for _ in range(_MIN_SAMPLES)]
        sb = _FakeSupabase({"soccer": rows})

        updated = compute_and_save(sb)

        assert updated["soccer"] > SPORT_DEFAULTS["soccer"]

    def test_all_wins_lowers_or_holds_threshold(self):
        rows = [_row("WIN") for _ in range(_MIN_SAMPLES)]
        sb = _FakeSupabase({"soccer": rows})

        updated = compute_and_save(sb)

        assert updated["soccer"] <= SPORT_DEFAULTS["soccer"]

    def test_wins_and_losses_produce_opposite_adjustments(self):
        losses = [_row("LOSS") for _ in range(_MIN_SAMPLES)]
        wins   = [_row("WIN") for _ in range(_MIN_SAMPLES)]

        loss_result = compute_and_save(_FakeSupabase({"soccer": losses}))["soccer"]
        win_result  = compute_and_save(_FakeSupabase({"soccer": wins}))["soccer"]

        assert loss_result > win_result

    def test_below_min_samples_leaves_threshold_unchanged(self):
        rows = [_row("LOSS") for _ in range(_MIN_SAMPLES - 1)]
        sb = _FakeSupabase({"soccer": rows})

        updated = compute_and_save(sb)

        assert updated["soccer"] == SPORT_DEFAULTS["soccer"]

    def test_expired_rows_dont_pad_the_sample_count(self):
        # Only 3 real decisive rows — well under _MIN_SAMPLES — padded out
        # with CLV-only 'expired' rows that must not count toward the
        # threshold for enough samples to trigger an adjustment.
        rows = [_row("expired") for _ in range(_MIN_SAMPLES)] + [_row("WIN") for _ in range(3)]
        sb = _FakeSupabase({"soccer": rows})

        updated = compute_and_save(sb)

        assert updated["soccer"] == SPORT_DEFAULTS["soccer"]

    def test_high_hit_rate_not_significant_at_low_odds_holds_threshold(self):
        # Task 4: 25/30 = 83% observed hit rate clears _TARGET_HI, but at
        # odds=1.3 the tax-adjusted breakeven is ~96% (p_breakeven=1.25/1.3)
        # — the Wilson 95% CI lower bound (~66%) doesn't clear that, so the
        # threshold must NOT be relaxed on this sample alone.
        rows = [_row("WIN", odds=1.3) for _ in range(25)] + [_row("LOSS", odds=1.3) for _ in range(5)]
        sb = _FakeSupabase({"soccer": rows})

        updated = compute_and_save(sb)

        assert updated["soccer"] == SPORT_DEFAULTS["soccer"]

    def test_high_hit_rate_significant_at_higher_odds_lowers_threshold(self):
        # Same 83% observed hit rate, but at odds=2.5 the breakeven is only
        # 50% — the same Wilson lower bound (~66%) clears that comfortably,
        # so the threshold should relax here.
        rows = [_row("WIN", odds=2.5) for _ in range(25)] + [_row("LOSS", odds=2.5) for _ in range(5)]
        sb = _FakeSupabase({"soccer": rows})

        updated = compute_and_save(sb)

        assert updated["soccer"] < SPORT_DEFAULTS["soccer"]
