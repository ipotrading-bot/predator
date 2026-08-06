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
import json

from core.learning_layer import (
    SPORT_DEFAULTS,
    _MIN_SAMPLES,
    _SEGMENT_MIN_SAMPLES,
    _calibration_flag,
    _clv_stats,
    _decide_threshold,
    _edge_band_diagnostic,
    _market_family,
    _sport_stats,
    playable_rows,
    compute_and_save,
    load_learning_summary,
    load_segment_thresholds,
    load_sport_ranking,
    load_edge_ceilings,
    _top_band_verdict,
    _odds_band_verdict,
    load_odds_ceilings,
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


def _row(outcome, kelly_pct=10.0, odds=2.0, clv_final=5.0, market_type=None,
         initial_edge=None, sharp_prob=None, clv_pct_real=None):
    # clv_final is deliberately positive on every row regardless of outcome
    # — reproducing the exact incident this guards against: entry-edge-as-
    # CLV is ~always >= 0 (MIN_EDGE already rejected negative edges before
    # the signal was sent), so a 100%-LOSS batch would still look like a
    # 100% "hit rate" if anything ever read clv_final again instead of
    # outcome.
    return {
        "outcome": outcome, "kelly_pct": kelly_pct, "odds": odds, "clv_final": clv_final,
        "market_type": market_type, "initial_edge": initial_edge,
        "sharp_prob": sharp_prob, "clv_pct_real": clv_pct_real,
    }


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


class TestBreakevenUsesOperatorTaxRate:
    def test_lowering_allowed_at_realistic_win_rate_in_tax_zero_regime(self):
        # 43/50 = 86% real win rate at avg odds 1.45. In the operator's
        # configured tax regime (constants.TAX_RATE = 0.0) the breakeven is
        # 1/1.45 ≈ 69.0%, and the Wilson lower bound (~73.8%) clears it —
        # the gate must allow lowering. Before the fix, _sport_stats called
        # p_breakeven(avg_odds) bare, silently inheriting the function's
        # own tax_rate=0.20 default: breakeven 1/(0.8*1.45) ≈ 86.2%, which
        # no realistic sample can clear at these odds — every lowering was
        # frozen in a regime the operator explicitly zeroed.
        rows = ([_row("WIN", odds=1.45) for _ in range(43)]
                + [_row("LOSS", odds=1.45) for _ in range(7)])
        stats = _sport_stats(rows)
        assert stats["hit_rate"] > 0.82   # sanity: above _TARGET_HI

        new_t, reason = _decide_threshold(2.0, stats, _clv_stats(rows), overconfident=False)
        assert new_t == 1.8, f"lowering blocked: {reason}"


class TestMarketFamily:
    def test_h2h_passthrough(self):
        assert _market_family("h2h") == "h2h"

    def test_totals_side_folded_to_family(self):
        assert _market_family("totals_over") == "totals"
        assert _market_family("totals_under") == "totals"

    def test_spreads_side_folded_to_family(self):
        assert _market_family("spreads_home") == "spreads"
        assert _market_family("spreads_away") == "spreads"

    def test_missing_or_empty(self):
        assert _market_family(None) == ""
        assert _market_family("") == ""


class TestClvStats:
    """Real CLV (clv_pct_real) must drive this, never clv_final/was_clv_positive
    — clv_final is deliberately positive on every fixture row (see _row's
    docstring on the tautology it guards against); a correct _clv_stats
    ignores it entirely."""

    def test_reads_real_clv_not_entry_edge_clv_final(self):
        # clv_final is positive on every row (the tautology-prone field);
        # clv_pct_real is negative on every row (the real signal). A correct
        # implementation must report a negative average / 0% positive rate.
        rows = [_row("WIN", clv_pct_real=-1.5) for _ in range(10)]
        stats = _clv_stats(rows)
        assert stats["n"] == 10
        assert stats["positive_rate"] == 0.0
        assert stats["avg_clv"] < 0

    def test_rows_without_real_clv_are_excluded_not_zeroed(self):
        rows = [_row("WIN") for _ in range(5)]   # clv_pct_real defaults to None
        stats = _clv_stats(rows)
        assert stats == {"n": 0, "avg_clv": None, "positive_rate": None}

    def test_mixed_rows_only_count_the_ones_with_real_clv(self):
        rows = ([_row("WIN", clv_pct_real=2.0) for _ in range(3)]
                + [_row("LOSS") for _ in range(7)])   # no clv_pct_real
        stats = _clv_stats(rows)
        assert stats["n"] == 3
        assert stats["positive_rate"] == 1.0


class TestCalibrationFlag:
    def test_well_calibrated_high_confidence_not_flagged(self):
        # 80%+ stated confidence, ~80% real win rate — no overconfidence.
        rows = ([_row("WIN", sharp_prob=0.85) for _ in range(16)]
                + [_row("LOSS", sharp_prob=0.85) for _ in range(4)])
        assert _calibration_flag(rows) is False

    def test_overconfident_high_bucket_is_flagged(self):
        # Stated 85% confidence but only winning half the time — the sport
        # can still sit inside the healthy 60-82% overall win-rate band
        # while this bucket alone is badly miscalibrated.
        rows = ([_row("WIN", sharp_prob=0.85) for _ in range(10)]
                + [_row("LOSS", sharp_prob=0.85) for _ in range(10)])
        assert _calibration_flag(rows) is True

    def test_too_few_samples_not_flagged(self):
        rows = [_row("LOSS", sharp_prob=0.85) for _ in range(5)]
        assert _calibration_flag(rows) is False


class TestEdgeBandDiagnostic:
    def test_too_few_samples_returns_none(self):
        rows = [_row("WIN", initial_edge=5.0) for _ in range(5)]
        assert _edge_band_diagnostic("soccer", rows) is None

    def test_monotonic_edge_no_warning(self):
        # Higher edge bucket wins MORE than the lower one — matches the
        # system's founding assumption, nothing to warn about.
        rows = ([_row("LOSS", initial_edge=1.0) for _ in range(10)]
                + [_row("WIN", initial_edge=1.0) for _ in range(5)]
                + [_row("WIN", initial_edge=9.0) for _ in range(14)]
                + [_row("LOSS", initial_edge=9.0) for _ in range(1)])
        assert _edge_band_diagnostic("soccer", rows) is None

    def test_top_bucket_underperforming_triggers_warning(self):
        # Top edge bucket (8%+, near SUSPECT_EDGE) loses MORE than a lower
        # bucket — the exact "erreur de données" pattern this diagnostic
        # exists to surface.
        rows = ([_row("WIN", initial_edge=1.0) for _ in range(13)]
                + [_row("LOSS", initial_edge=1.0) for _ in range(2)]
                + [_row("LOSS", initial_edge=9.0) for _ in range(12)]
                + [_row("WIN", initial_edge=9.0) for _ in range(3)])
        warning = _edge_band_diagnostic("soccer", rows)
        assert warning is not None
        assert "soccer" in warning


class TestDecideThreshold:
    def _stats(self, hit_rate, n=40, wilson_lower=0.9, p_breakeven=0.5):
        return {"hit_rate": hit_rate, "n": n, "wilson_lower": wilson_lower,
                "p_breakeven": p_breakeven, "roi": None}

    _no_clv = {"n": 0, "avg_clv": None, "positive_rate": None}

    def test_overconfident_forces_raise_inside_healthy_band(self):
        stats = self._stats(hit_rate=0.70)   # inside the healthy 60-82% band
        new_t, reason = _decide_threshold(2.0, stats, self._no_clv, overconfident=True)
        assert new_t is not None and new_t > 2.0
        assert "overconfident" in reason

    def test_negative_real_clv_blocks_a_would_be_lowering(self):
        stats = self._stats(hit_rate=0.90)   # would otherwise lower
        clv = {"n": 30, "avg_clv": -1.0, "positive_rate": 0.2}
        new_t, reason = _decide_threshold(2.0, stats, clv, overconfident=False)
        assert new_t is None
        assert "CLV" in reason

    def test_positive_real_clv_does_not_block_lowering(self):
        stats = self._stats(hit_rate=0.90)
        clv = {"n": 30, "avg_clv": 1.0, "positive_rate": 0.8}
        new_t, reason = _decide_threshold(2.0, stats, clv, overconfident=False)
        assert new_t is not None and new_t < 2.0

    def test_low_hit_rate_still_raises_even_with_positive_clv(self):
        # Raising stays always-safe regardless of CLV — CLV only tags the
        # reason (probable variance) so the operator can tell it apart from
        # genuine edge decay, it never blocks a raise.
        stats = self._stats(hit_rate=0.40)
        clv = {"n": 30, "avg_clv": 1.0, "positive_rate": 0.8}
        new_t, reason = _decide_threshold(2.0, stats, clv, overconfident=False)
        assert new_t is not None and new_t > 2.0
        assert "variance" in reason


class TestSegmentThresholds:
    def test_load_segment_thresholds_parses_sport_and_family(self):
        class _Sel:
            def like(self, *_a, **_k):
                return self
            def execute(self):
                return _Result([{"key": "threshold_seg_soccer_totals", "value": "3.5"}])

        class _MetaSel:
            def select(self, *_a, **_k):
                return _Sel()

        class _SB:
            def table(self, name):
                assert name == "meta"
                return _MetaSel()

        result = load_segment_thresholds(_SB())
        assert result == {"soccer:totals": 3.5}

    def test_compute_and_save_persists_a_segment_threshold(self):
        # 30 h2h rows losing badly for soccer — enough to clear
        # _SEGMENT_MIN_SAMPLES and move the h2h-specific threshold, on top
        # of (not instead of) the sport-wide one.
        rows = [_row("LOSS", market_type="h2h") for _ in range(_SEGMENT_MIN_SAMPLES + 5)]
        sb = _FakeSupabase({"soccer": rows})

        compute_and_save(sb)

        assert any(w["key"] == "threshold_seg_soccer_h2h" for w in sb.meta_writes)


class TestLearningSummary:
    def test_load_learning_summary_parses_persisted_json(self):
        class _Sel:
            def eq(self, *_a, **_k):
                return self
            def limit(self, *_a, **_k):
                return self
            def execute(self):
                return _Result([{"value": '["soccer: seuil 1.20% -> 1.60%"]'}])

        class _MetaSel:
            def select(self, *_a, **_k):
                return _Sel()

        class _SB:
            def table(self, name):
                assert name == "meta"
                return _MetaSel()

        assert load_learning_summary(_SB()) == ["soccer: seuil 1.20% -> 1.60%"]

    def test_compute_and_save_persists_a_summary_for_a_threshold_change(self):
        rows = [_row("LOSS") for _ in range(_MIN_SAMPLES)]
        sb = _FakeSupabase({"soccer": rows})

        compute_and_save(sb)

        summary_writes = [w for w in sb.meta_writes if w["key"] == "learning_summary"]
        assert len(summary_writes) == 1
        assert any("soccer" in line for line in json.loads(summary_writes[0]["value"]))


class TestSportRanking:
    """meta.sport_ranking — l'ordre qui arbitre le budget oracle du scan.

    Le tri se fait sur la borne basse de Wilson, jamais sur le taux brut.
    Mesuré le 2026-08-02 sur le vrai ledger : mma affichait 100% sur 5
    résultats et tabletennis 62,5% sur 8, quand soccer faisait 48,2% sur 83.
    Un tri sur le brut aurait placé deux échantillons de bruit devant la seule
    série mesurable, et le budget oracle serait parti sur des sports dont on
    ne sait rien. Ces tests échouent si quelqu'un rebascule sur `hit_rate`.
    """

    def _ranking(self, sb) -> list:
        writes = [w for w in sb.meta_writes if w["key"] == "sport_ranking"]
        assert len(writes) == 1, "sport_ranking doit être écrit exactement une fois"
        return json.loads(writes[0]["value"])

    def test_small_perfect_sample_is_excluded(self):
        # 5 WIN sur 5 = 100% brut, mais très en dessous de _MIN_SAMPLES.
        sb = _FakeSupabase({"mma": [_row("WIN") for _ in range(5)]})
        compute_and_save(sb)
        assert "mma" not in self._ranking(sb)

    def test_ranks_by_wilson_lower_bound_not_raw_hit_rate(self):
        # Le point de bascule est étroit et vaut d'être fixé : Wilson ne
        # renverse PAS n'importe quel écart. À 80% sur 30 contre 60% sur 150,
        # il garde 80% devant, et il a raison — l'écart est trop large pour
        # être du bruit. Il renverse quand l'écart brut est mince et l'écart
        # de taille grand : 60% sur 30 (borne 0.423) passe DERRIÈRE 55% sur
        # 300 (borne 0.493), parce que 30 tirages ne distinguent pas encore
        # 60% de 45%.
        sb = _FakeSupabase({
            "basketball": [_row("WIN")] * 18 + [_row("LOSS")] * 12,     # 60% / n=30
            "soccer": [_row("WIN")] * 165 + [_row("LOSS")] * 135,       # 55% / n=300
        })
        compute_and_save(sb)
        ranking = self._ranking(sb)

        basket_stats = _sport_stats(sb._rows_by_sport["basketball"])
        soccer_stats = _sport_stats(sb._rows_by_sport["soccer"])
        assert basket_stats["hit_rate"] > soccer_stats["hit_rate"], "prémisse du test"
        assert basket_stats["wilson_lower"] < soccer_stats["wilson_lower"], "prémisse"

        # Un tri sur hit_rate donnerait basketball en tête.
        assert ranking.index("soccer") < ranking.index("basketball")

    def test_no_decisive_results_writes_an_empty_order(self):
        # Aucun WIN/LOSS nulle part : le classement doit être vide, pas deviné.
        # run_engine garde alors son ordre par défaut.
        sb = _FakeSupabase({"soccer": [_row("PUSH") for _ in range(_MIN_SAMPLES)]})
        compute_and_save(sb)
        assert self._ranking(sb) == []

    def test_load_sport_ranking_round_trip(self):
        sb = _FakeSupabase({"soccer": [_row("WIN") for _ in range(_MIN_SAMPLES)]})
        compute_and_save(sb)
        stored = self._ranking(sb)

        class _Sel:
            def eq(self, *_a, **_k):
                return self

            def maybe_single(self):
                return self

            def execute(self):
                return _Result({"value": json.dumps(stored)})

        class _SB:
            def table(self, name):
                assert name == "meta"

                class _T:
                    def select(self, *_a, **_k):
                        return _Sel()
                return _T()

        assert load_sport_ranking(_SB()) == stored

    def test_load_sport_ranking_returns_empty_when_key_absent(self):
        class _Sel:
            def eq(self, *_a, **_k):
                return self

            def maybe_single(self):
                return self

            def execute(self):
                return _Result(None)

        class _SB:
            def table(self, name):
                class _T:
                    def select(self, *_a, **_k):
                        return _Sel()
                return _T()

        assert load_sport_ranking(_SB()) == []


class TestEdgeCeiling:
    """La boucle de rétroaction que _decide_threshold créait tout seul.

    Il n'a qu'un levier — le PLANCHER d'edge — et le monte quand un sport perd.
    Si ce sont les GROS edges qui perdent, le relèvement pousse toute l'émission
    dans la bande perdante, le résultat empire, le plancher remonte : la boucle
    finit au plafond dur et le sport devient muet. Constaté le 2026-08-02 sur le
    vrai ledger — soccer était à 6,00% (le plafond) alors que sa bande 6%+
    affichait 36,7% de réussite sur 49 paris pour 47,8% requis, pendant que la
    bande 1,5-4% gagnait.
    """

    def _rows(self, low_band_wr: float, top_band_wr: float, n_each: int = 12):
        """n_each résultats dans une bande basse et dans la bande haute."""
        rows = []
        for edge, wr in ((1.0, low_band_wr), (9.0, top_band_wr)):
            wins = int(round(n_each * wr))
            rows += [_row("WIN", initial_edge=edge) for _ in range(wins)]
            rows += [_row("LOSS", initial_edge=edge) for _ in range(n_each - wins)]
        return rows

    def test_detects_a_losing_top_band(self):
        fake, ceiling, n, best_lo = _top_band_verdict(
            self._rows(low_band_wr=0.75, top_band_wr=0.25))
        assert fake is True
        assert ceiling == 6.0        # plancher du bucket (6, 100)
        assert n > 0
        assert best_lo == 1.0 or best_lo == 0.0   # la bande basse gagnante

    def test_healthy_top_band_sets_no_ceiling(self):
        # La bande haute gagne PLUS que la basse : c'est l'hypothèse fondatrice
        # du système qui tient. Aucun plafond ne doit être posé.
        fake, ceiling, _n, _best = _top_band_verdict(
            self._rows(low_band_wr=0.30, top_band_wr=0.80))
        assert fake is False
        assert ceiling is None

    def test_threshold_is_not_raised_when_the_top_band_is_the_problem(self):
        # Mauvais taux de réussite : la règle normale relèverait le plancher.
        stats = _sport_stats([_row("LOSS") for _ in range(_MIN_SAMPLES)])
        clv = _clv_stats([])

        raised, _ = _decide_threshold(2.0, stats, clv, False, top_band_fake=False)
        assert raised is not None and raised > 2.0, "prémisse : sans le garde, ça monte"

        held, reason = _decide_threshold(2.0, stats, clv, False, top_band_fake=True)
        assert held is None
        assert "plancher tenu" in reason

    def test_floor_drops_to_the_best_measured_band_in_one_move(self):
        # Le cas soccer : plancher coincé à 6,0% (le plafond dur) alors que la
        # bande gagnante est à 1,5%. Descendre par pas de _STEP_DOWN aurait pris
        # 22 audits, soit plus de 5 jours de silence.
        stats = _sport_stats([_row("LOSS") for _ in range(_MIN_SAMPLES)])
        clv = _clv_stats([])
        new_t, reason = _decide_threshold(6.0, stats, clv, False,
                                          top_band_fake=True, best_band_lo=1.5)
        assert new_t == 1.5
        assert "meilleure bande" in reason

    def test_overconfidence_alone_still_raises(self):
        # Le garde ne doit neutraliser le relèvement QUE si la bande haute est
        # en cause — sinon il désarmerait la correction de surconfiance.
        stats = _sport_stats([_row("WIN") for _ in range(_MIN_SAMPLES)])
        clv = _clv_stats([])
        new_t, _ = _decide_threshold(2.0, stats, clv, True, top_band_fake=False)
        assert new_t is not None and new_t > 2.0

    def test_ceiling_is_persisted_to_meta(self):
        rows = self._rows(low_band_wr=0.75, top_band_wr=0.20, n_each=15)
        sb = _FakeSupabase({"soccer": rows})
        compute_and_save(sb)
        writes = [w for w in sb.meta_writes if w["key"] == "edge_ceiling_soccer"]
        assert len(writes) == 1
        assert float(writes[0]["value"]) == 6.0

    def test_load_edge_ceilings_parses_and_skips_garbage(self):
        class _Sel:
            def like(self, *_a, **_k):
                return self

            def execute(self):
                return _Result([
                    {"key": "edge_ceiling_soccer", "value": "8.0"},
                    {"key": "edge_ceiling_broken", "value": "not-a-number"},
                ])

        class _SB:
            def table(self, name):
                assert name == "meta"

                class _T:
                    def select(self, *_a, **_k):
                        return _Sel()
                return _T()

        assert load_edge_ceilings(_SB()) == {"soccer": 8.0}


class TestOddsCeiling:
    """Le plafond de COTE ne s'active que sur une bande PROUVÉE perdante.

    Asymétrie voulue avec le plafond d'edge : « un edge trop gros est un prix
    mal apparié » préexistait dans le code (SUSPECT_EDGE, MAX_EDGE) et le test
    relatif haut-contre-bas la valide (p=0,005 sur soccer au 2026-08-02). « On
    gagne sur les favoris courts » est une affirmation NOUVELLE, sans mécanisme
    préalable — exactement le motif qui ressort d'une fouille de données puis
    disparaît hors échantillon. Au 2026-08-02 aucune bande n'était concluante,
    pas même celle à n=109 dont la borne haute (52,5%) dépassait son seuil de
    50,0%. Poser la règle dessus aurait coupé 58% du volume sur du bruit.
    """

    def _rows(self, odds: float, n: int, wr: float):
        wins = int(round(n * wr))
        return ([_row("WIN", odds=odds) for _ in range(wins)]
                + [_row("LOSS", odds=odds) for _ in range(n - wins)])

    def test_real_2026_08_02_data_activates_nothing(self):
        # Les bandes telles que mesurées : aucune n'est concluante.
        rows = (self._rows(1.35, 17, 0.824)
                + self._rows(1.66, 60, 0.517)
                + self._rows(2.00, 109, 0.431))
        cap, _n, _diag = _odds_band_verdict(rows)
        assert cap is None, "aucune bande n'était prouvée perdante ce jour-là"

    def test_activates_on_a_clearly_losing_band(self):
        # Même bande de cote, mais un taux assez bas pour que la borne HAUTE
        # de Wilson passe sous le seuil de rentabilité.
        rows = self._rows(1.60, 40, 0.80) + self._rows(2.00, 80, 0.25)
        cap, n, diag = _odds_band_verdict(rows)
        assert cap == 1.80
        assert n >= 80
        assert "prouvée" in diag

    def test_never_caps_below_short_favourites(self):
        # Une bande 1,0-1,5 catastrophique ne doit pas produire un plafond qui
        # supprimerait toute émission : _ODDS_CEILING_MIN la met hors jeu.
        rows = self._rows(1.20, 60, 0.10) + self._rows(1.60, 40, 0.80)
        cap, _n, _diag = _odds_band_verdict(rows)
        assert cap is None or cap >= 1.50

    def test_too_few_samples_activates_nothing(self):
        cap, _n, _diag = _odds_band_verdict(self._rows(2.00, 5, 0.0))
        assert cap is None

    def test_load_odds_ceilings_parses_and_skips_garbage(self):
        class _Sel:
            def like(self, *_a, **_k):
                return self

            def execute(self):
                return _Result([
                    {"key": "odds_ceiling_soccer", "value": "1.8"},
                    {"key": "odds_ceiling_broken", "value": "nope"},
                ])

        class _SB:
            def table(self, name):
                class _T:
                    def select(self, *_a, **_k):
                        return _Sel()
                return _T()

        assert load_odds_ceilings(_SB()) == {"soccer": 1.8}


class TestPlayableZone:
    """Le moteur n'apprend que sur les paris qu'il recommande encore.

    Mesuré le 2026-08-06 sur 204 paris réglés : la zone 2-24h fait +9,4% de
    ROI, le reste -28,5% (p=0,002) — et ce reste n'est plus jouable (fenêtre
    de scan à 24h, golden hour en fantôme). L'y laisser faisait monter les
    seuils à cause de pertes que le système ne subit plus.
    """

    @staticmethod
    def _row(minutes):
        return {"outcome": "WIN", "odds": 1.9, "time_to_match_minutes": minutes}

    def test_golden_hour_is_excluded(self):
        assert playable_rows([self._row(30), self._row(119)]) == []

    def test_beyond_scan_window_is_excluded(self):
        assert playable_rows([self._row(1441), self._row(4000)]) == []

    def test_playable_zone_is_kept(self):
        rows = [self._row(120), self._row(600), self._row(1440)]
        assert playable_rows(rows) == rows

    def test_row_without_lead_time_is_kept(self):
        # On ne peut pas prouver qu'elle est hors zone : la jeter viderait
        # l'apprentissage de tout l'historique antérieur à la colonne.
        rows = [{"outcome": "WIN", "odds": 1.9, "time_to_match_minutes": None},
                {"outcome": "LOSS", "odds": 2.0}]
        assert playable_rows(rows) == rows

    def test_unparseable_lead_time_is_kept(self):
        rows = [{"outcome": "WIN", "odds": 1.9, "time_to_match_minutes": "n/a"}]
        assert playable_rows(rows) == rows


class TestBreakevenAnchoredThreshold:
    """Le critère est la rentabilité mesurée, plus un taux absolu.

    Régression du cliquet constaté en base le 2026-08-06 : monter à <60% et
    ne descendre qu'à >82% envoyait tout sport au plafond de 6,0% puis au
    silence, alors qu'un pari à cote 1,85 est rentable dès 54,1%.
    """

    _no_clv = {"n": 0, "avg_clv": None, "positive_rate": None}

    @staticmethod
    def _stats(hit_rate, n=40, wilson_lower=0.0, p_breakeven=0.54):
        return {"hit_rate": hit_rate, "n": n, "wilson_lower": wilson_lower,
                "wilson_upper": 1.0, "p_breakeven": p_breakeven, "roi": None}

    def test_profitable_sport_below_60pct_no_longer_raises(self):
        # 57% de réussite à 1,85 = rentable. L'ancienne règle montait quand
        # même (57 < 60) — c'est ce qui a collé basketball au plafond.
        stats = self._stats(hit_rate=0.57, p_breakeven=0.54)
        new_t, reason = _decide_threshold(3.0, stats, self._no_clv, overconfident=False)
        assert new_t is None
        assert "ne tranche pas" in reason

    def test_below_breakeven_still_raises(self):
        stats = self._stats(hit_rate=0.50, p_breakeven=0.58)
        new_t, _ = _decide_threshold(3.0, stats, self._no_clv, overconfident=False)
        assert new_t is not None and new_t > 3.0

    def test_lowering_no_longer_needs_82pct(self):
        # 65% prouvés au-dessus d'une rentabilité de 54% suffisent désormais :
        # aucun segment du ledger n'a jamais atteint les 82% exigés avant.
        stats = self._stats(hit_rate=0.65, wilson_lower=0.58, p_breakeven=0.54)
        new_t, _ = _decide_threshold(3.0, stats, self._no_clv, overconfident=False)
        assert new_t is not None and new_t < 3.0

    def test_high_odds_sport_is_judged_on_its_own_breakeven(self):
        # Cote ~3,0 → rentable dès 34%. 45% de réussite ne doit pas être
        # traité comme un échec sous prétexte que c'est loin de 60%.
        stats = self._stats(hit_rate=0.45, p_breakeven=0.34)
        new_t, _ = _decide_threshold(3.0, stats, self._no_clv, overconfident=False)
        assert new_t is None

    def test_falls_back_to_absolute_rule_without_odds(self):
        stats = self._stats(hit_rate=0.40, p_breakeven=None)
        new_t, reason = _decide_threshold(3.0, stats, self._no_clv, overconfident=False)
        assert new_t is not None and new_t > 3.0
        assert "repli" in reason
