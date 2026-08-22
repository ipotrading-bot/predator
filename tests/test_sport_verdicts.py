"""
tests/test_sport_verdicts.py — Phase 4 : boucle d'apprentissage et de
calibration. Les verdicts promotion/rétrogradation sont CHIFFRÉS (≥30
réglés, IC de Wilson contre la rentabilité post-taxe), LOGGÉS dans meta et
jamais appliqués automatiquement. Le rapport hebdo suit les métriques de
vérité : CLV réel, Brier, ROI net taxe, taux SUSPECT_DATA.
"""
import json
from datetime import datetime, timezone

import yaml

from core.learning_layer import (_PROMOTION_MIN_SAMPLES, load_sport_verdicts,
                                 sport_verdict)
from scripts.weekly_report import format_report, sport_truth_metrics, suspect_rate


def _stats(n, lo, hi, be=0.55, hit=0.6):
    return {"n": n, "hit_rate": hit, "wilson_lower": lo, "wilson_upper": hi,
            "p_breakeven": be, "roi": 0.05}


class TestVerdict:
    def test_below_30_is_insufficient_whatever_the_numbers(self):
        v = sport_verdict(_stats(29, 0.70, 0.90))
        assert v["status"] == "insuffisant" and v["retrait_propose"] is False

    def test_wilson_lower_above_breakeven_is_promotion_eligible(self):
        v = sport_verdict(_stats(40, 0.58, 0.75), {"n": 20, "avg_clv": 1.2, "positive_rate": 0.6})
        assert v["status"] == "promotion_eligible"
        assert v["avg_clv"] == 1.2

    def test_wilson_upper_below_breakeven_is_proven_loss(self):
        v = sport_verdict(_stats(40, 0.30, 0.50, be=0.55, hit=0.4))
        assert v["status"] == "perte_prouvee" and v["retrait_propose"] is True

    def test_straddling_ci_after_30_is_not_demonstrated(self):
        v = sport_verdict(_stats(35, 0.45, 0.70))
        assert v["status"] == "non_demontre" and v["retrait_propose"] is True

    def test_threshold_constant_is_30_not_the_learning_min_samples(self):
        from core.learning_layer import _MIN_SAMPLES
        assert _PROMOTION_MIN_SAMPLES == 30 and _MIN_SAMPLES == 20


def test_load_sport_verdicts_roundtrip():
    class _Q:
        def __init__(self): self.rows = [
            {"key": "sport_verdict_soccer", "value": json.dumps({"status": "non_demontre"})},
            {"key": "sport_verdict_mma", "value": "pas du json"},
        ]
        def select(self, *_a): return self
        def like(self, *_a): return self
        def execute(self): return type("R", (), {"data": self.rows})()

    class _SB:
        def table(self, _n): return _Q()

    got = load_sport_verdicts(_SB())
    assert got == {"soccer": {"status": "non_demontre"}}


def _row(outcome, odds=1.9, prob=0.55, clv=None, ttm=300, kelly=0.5):
    return {"outcome": outcome, "kelly_pct": kelly, "odds": odds, "market_type": "h2h",
            "initial_edge": 3.0, "sharp_prob": prob, "clv_pct_real": clv,
            "time_to_match_minutes": ttm}


class TestWeeklyReport:
    def test_truth_metrics(self):
        rows = [_row("WIN", clv=1.5) for _ in range(12)] + [_row("LOSS", clv=-0.5) for _ in range(8)]
        m = sport_truth_metrics(rows)
        assert m["n"] == 20 and abs(m["hit_rate"] - 0.6) < 1e-9
        assert m["clv_n"] == 20 and abs(m["avg_clv"] - (12*1.5 - 8*0.5)/20) < 1e-9
        assert m["brier"] is not None and m["brier_ref"] is not None
        assert m["roi_net"] is not None

    def test_out_of_playable_zone_rows_are_ignored(self):
        rows = [_row("WIN", ttm=30) for _ in range(20)]      # < 2h : golden hour fantôme
        assert sport_truth_metrics(rows)["n"] == 0

    def test_suspect_rate(self):
        assert suspect_rate([{"risk_flag": "SUSPECT_DATA"}, {"risk_flag": "VALUE"}]) == (1, 2)
        assert suspect_rate([]) == (0, 0)

    def test_format_mentions_alerts_and_skips_empty_sports(self):
        metrics = {"soccer": sport_truth_metrics([_row("WIN", clv=2.0)] * 30),
                   "boxing": sport_truth_metrics([])}
        verdicts = {"soccer": {"status": "non_demontre", "retrait_propose": True,
                               "reason": "IC chevauche"}}
        text = format_report(metrics, verdicts, (1, 40), datetime(2026, 8, 24, 7, tzinfo=timezone.utc))
        assert "soccer" in text and "RETRAIT PROPOSÉ" in text and "Alertes" in text
        assert "boxing" not in text
        assert "SUSPECT_DATA : 1/40" in text


def test_weekly_workflow_is_scheduled_and_runs_the_report():
    wf = yaml.safe_load(open(".github/workflows/rank_sports.yml"))
    on = wf.get("on") or wf.get(True)
    assert any("cron" in s for s in on["schedule"])
    steps = wf["jobs"]["rank"]["steps"]
    assert any("weekly_report.py" in (s.get("run") or "") for s in steps)
    assert any("calibration_report.py" in (s.get("run") or "") for s in steps)
