"""
scripts/edge_frequency_audit.py — PAIM v9.5 — Edge amplitude x frequency audit.

Answers: does the 1XBet-vs-Pinnacle lag edge have enough AMPLITUDE and
FREQUENCY simultaneously, on real data, to survive the tax threshold at a
given system size k? A system can be +EV on paper (tax_engine.py) and
still be commercially dead if a k-leg opportunity only exists twice a
month in reality.

KNOWN DATA LIMITATION (checked before writing any analysis logic — see
Step 0 in the accompanying prompt): only signals that ALREADY passed
compute_alpha's min_edge gate are ever persisted anywhere (run_engine.py's
_emit() returns without appending to `signals` on any DISCARD/VOLATILE
path — see that function). There is no rejected-candidate log. The edge
distribution measured here is therefore truncated at whatever the
adaptive threshold happened to be at scan time — it can describe "how
good are the edges that already qualified" but NOT "what's the full
distribution including near-misses," which would need a logging change
in run_engine.py to ever become answerable. Documented here, not
silently assumed away.

Sources: `signals` (any status — 48h retention) + `ai_learning_ledger`
(permanent, but only ever fed by settled/closed/expired terminal signals
— same truncation applies). Rows from either are normalized to one
common shape before analysis.
"""
import logging
import math
import statistics
from collections import defaultdict

from core.stats_utils import wilson_ci
from core.tax_engine import (
    DEFAULT_TAX_RATE,
    min_edge_required,
    optimal_stake_fraction,
    system_expected_value,
)

log = logging.getLogger("PREDATOR.edge_frequency_audit")

DEFAULT_K_RANGE = range(2, 13)   # k = 2..12, per the task spec
DEFAULT_SAFETY_MARGIN = 1.15     # matches the safety margin over min_edge_required() specified in the task
DEFAULT_MIN_POSTHOC_SAMPLES = 30 # same bar as core/learning_layer.py's _MIN_SAMPLES


# ── Normalization ────────────────────────────────────────────────────────

def normalize_signal_row(row: dict) -> dict | None:
    """From the `signals` table (any status)."""
    edge = row.get("edge_pct")
    odds = row.get("xbet_odd")
    pin = row.get("pinnacle_price")
    if edge is None or not odds or not pin or float(pin) <= 1.01:
        return None
    true_prob = row.get("sharp_prob") or (1 / float(pin))
    date = (row.get("scanned_at") or row.get("created_at") or "")[:10]
    if not date:
        return None
    sport = row.get("sport") or ""
    league = row.get("league") or ""
    return {
        "date": date,
        "sport": sport,
        "market": row.get("market_key") or row.get("market") or "",
        "league": league,
        "match": row.get("match", ""),
        "edge_pct": float(edge),
        "true_prob": float(true_prob),
        "odds": float(odds),
        "outcome": row.get("outcome"),
        "correlation_group": f"{sport}:{date}:{league}",
        "source": "signals",
    }


def normalize_ledger_row(row: dict) -> dict | None:
    """
    From `ai_learning_ledger`. This table doesn't store pinnacle_price,
    so true_prob falls back to an implied fair probability derived from
    initial_edge + odds (fair_odds = odds/(1+edge/100), true_prob =
    1/fair_odds) whenever sharp_prob isn't populated (pre-migrate_v9_7
    rows, or before it's applied at all) — mathematically identical to
    what compute_alpha() already assumed when the signal was scanned.
    """
    edge = row.get("initial_edge")
    odds = row.get("odds")
    if edge is None or not odds:
        return None
    edge = float(edge)
    odds = float(odds)
    sharp_prob = row.get("sharp_prob")
    if sharp_prob:
        true_prob = float(sharp_prob)
    else:
        fair_odds = odds / (1 + edge / 100)
        if fair_odds <= 1.01:
            return None
        true_prob = 1 / fair_odds
    date = (row.get("created_at") or "")[:10]
    if not date:
        return None
    sport = row.get("sport") or ""
    league = row.get("league") or ""
    return {
        "date": date,
        "sport": sport,
        "market": row.get("market_type") or row.get("market") or "",
        "league": league,
        "match": row.get("match", ""),
        "edge_pct": edge,
        "true_prob": true_prob,
        "odds": odds,
        "outcome": row.get("outcome"),
        "correlation_group": f"{sport}:{date}:{league}",
        "source": "ledger",
    }


# ── Step 2: empirical edge distribution ─────────────────────────────────

def _percentile(sorted_vals: list[float], p: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def edge_distribution(records: list[dict]) -> dict:
    """
    Empirical edge_pct distribution grouped by (sport, market) — n, mean,
    std, p10/p50/p90. Deliberately NOT pooled across sports/markets: a
    single pooled distribution is exactly the homogeneous-true_prob
    simplification core.tax_engine.min_edge_required() makes for its
    single-scalar threshold, and this step exists to check whether that
    simplification hides real heterogeneity.
    """
    groups = defaultdict(list)
    for r in records:
        groups[(r["sport"], r["market"])].append(r["edge_pct"])

    result = {}
    for (sport, market), edges in groups.items():
        edges_sorted = sorted(edges)
        n = len(edges_sorted)
        result[f"{sport}:{market}"] = {
            "n": n,
            "mean": statistics.mean(edges_sorted),
            "std": statistics.stdev(edges_sorted) if n > 1 else 0.0,
            "p10": _percentile(edges_sorted, 0.10),
            "p50": _percentile(edges_sorted, 0.50),
            "p90": _percentile(edges_sorted, 0.90),
        }
    return result


# ── Step 3: correlation groups per day ───────────────────────────────────

def group_by_day(records: list[dict]) -> dict:
    """{date: {correlation_group: [records]}}"""
    by_day = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_day[r["date"]][r["correlation_group"]].append(r)
    return {d: dict(g) for d, g in by_day.items()}


def _best_qualifying_legs_per_group(groups: dict, k: int, safety_margin: float) -> list[dict]:
    """The single best (highest-edge) qualifying leg from each
    correlation_group present on a day — never lets two legs from the
    same group both count toward k (Task 5's correlation guard, applied
    here at analysis time rather than at signal-generation time)."""
    best = []
    for _group, legs in groups.items():
        qualifying = [leg for leg in legs
                      if leg["edge_pct"] > min_edge_required(k, leg["true_prob"]) * safety_margin]
        if qualifying:
            best.append(max(qualifying, key=lambda x: x["edge_pct"]))
    return best


# ── Step 4: frequency test per k ─────────────────────────────────────────

def frequency_by_k(by_day: dict, k_range=DEFAULT_K_RANGE,
                   safety_margin: float = DEFAULT_SAFETY_MARGIN) -> list[dict]:
    """
    For each k, count how many historical days had >= k qualifying legs
    from DISTINCT correlation groups (edge_pct > min_edge_required(k,
    true_prob)*safety_margin, using each leg's OWN true_prob — not a
    single reference probability).
    """
    total_days = len(by_day)
    results = []
    for k in k_range:
        valid_days = sum(1 for groups in by_day.values()
                         if len(_best_qualifying_legs_per_group(groups, k, safety_margin)) >= k)
        ratio = valid_days / total_days if total_days else 0.0
        results.append({
            "k": k,
            "valid_days": valid_days,
            "total_days": total_days,
            "ratio": ratio,
            "estimated_systems_per_month": round(ratio * 30, 2),
        })
    return results


# ── Step 5: frequency x magnitude -> expected monthly log-growth ────────

def magnitude_by_k(by_day: dict, k_range=DEFAULT_K_RANGE,
                   safety_margin: float = DEFAULT_SAFETY_MARGIN,
                   tax_rate: float = DEFAULT_TAX_RATE) -> list[dict]:
    """
    For each k and each valid day, compute the REAL combo EV via
    system_expected_value() and the numerically optimal stake fraction —
    using the actual best-per-correlation-group legs available that day,
    not tax_engine.min_edge_required()'s homogeneous-true_prob
    approximation. Expected monthly log-growth = opportunities/month x
    average log-growth per opportunity — the metric meant to decide
    viability, not EV-per-system taken in isolation (a system can be
    individually +EV and still contribute negligible growth if it's rare).
    """
    total_days = len(by_day)
    results = []
    for k in k_range:
        log_growths = []
        for groups in by_day.values():
            best = _best_qualifying_legs_per_group(groups, k, safety_margin)
            if len(best) < k:
                continue
            chosen = sorted(best, key=lambda x: x["edge_pct"], reverse=True)[:k]
            legs_for_ev = [{"true_prob": leg["true_prob"], "odds": leg["odds"]} for leg in chosen]
            ev_result = system_expected_value(legs_for_ev, stake=1.0, tax_rate=tax_rate)
            if not ev_result["viable"]:
                continue
            p, combined_odds = ev_result["combined_prob"], ev_result["combined_odds"]
            frac = optimal_stake_fraction(p, combined_odds, tax_rate)
            if frac <= 0:
                continue
            net_b = (combined_odds - 1) * (1 - tax_rate)
            growth = p * math.log(1 + frac * net_b) + (1 - p) * math.log(max(1e-9, 1 - frac))
            log_growths.append(growth)

        n_opp = len(log_growths)
        avg_growth = sum(log_growths) / n_opp if n_opp else 0.0
        opp_per_month = (n_opp / total_days * 30) if total_days else 0.0
        results.append({
            "k": k,
            "n_opportunities": n_opp,
            "avg_log_growth_per_opportunity": avg_growth,
            "opportunities_per_month": round(opp_per_month, 2),
            "expected_monthly_log_growth": avg_growth * opp_per_month,
        })
    return results


# ── Step 6: post-hoc validation ──────────────────────────────────────────

def posthoc_validation(by_day: dict, k_range=DEFAULT_K_RANGE,
                       safety_margin: float = DEFAULT_SAFETY_MARGIN,
                       min_samples: int = DEFAULT_MIN_POSTHOC_SAMPLES) -> list[dict]:
    """
    For each k, among the qualifying k-leg combos identified per day,
    keep only combos where EVERY leg has a real settled outcome
    (WIN/LOSS) and compute the realized hit rate + Wilson 95% CI. Below
    `min_samples` resolved combos for a given k, says so explicitly
    rather than computing a statistically meaningless CI (never display
    a rate without its interval — same rule as core/learning_layer.py).
    """
    results = []
    for k in k_range:
        wins = 0
        decisive = 0
        for groups in by_day.values():
            best = _best_qualifying_legs_per_group(groups, k, safety_margin)
            if len(best) < k:
                continue
            chosen = sorted(best, key=lambda x: x["edge_pct"], reverse=True)[:k]
            outcomes = [leg["outcome"] for leg in chosen]
            if any(o not in ("WIN", "LOSS") for o in outcomes):
                continue
            decisive += 1
            if all(o == "WIN" for o in outcomes):
                wins += 1

        if decisive < min_samples:
            results.append({
                "k": k, "decisive_combos": decisive, "sufficient": False,
                "message": (f"only {decisive} fully-resolved {k}-leg combo(s) found — "
                           f"need >= {min_samples} for a statistically meaningful hit rate/CI"),
            })
            continue

        lo, hi = wilson_ci(wins, decisive)
        results.append({
            "k": k, "decisive_combos": decisive, "sufficient": True,
            "hit_rate": wins / decisive, "wilson_lower": lo, "wilson_upper": hi,
        })
    return results


# ── I/O layer ─────────────────────────────────────────────────────────────

def fetch_records(sb) -> list[dict]:
    """
    Pull every row from `signals` (any status — not just active) and
    `ai_learning_ledger` (the permanent record), normalized to one common
    shape. This is ALL the qualifying-signal data that exists anywhere —
    per this module's docstring, there is no separate rejected-candidate
    log to also include.
    """
    records = []
    try:
        res = sb.table("signals").select("*").execute()
        records.extend(r for r in (normalize_signal_row(row) for row in (res.data or [])) if r)
    except Exception as e:
        log.error("fetch signals: %s", e)
    try:
        res = sb.table("ai_learning_ledger").select("*").execute()
        records.extend(r for r in (normalize_ledger_row(row) for row in (res.data or [])) if r)
    except Exception as e:
        log.error("fetch ai_learning_ledger: %s", e)
    return records


def run_audit(records: list[dict]) -> dict:
    """Run all analysis steps (2-6) and return the combined result dict
    — pure function of `records`, no I/O, easy to test and to re-run
    against a report template."""
    by_day = group_by_day(records)
    return {
        "n_records": len(records),
        "n_days": len(by_day),
        "edge_distribution": edge_distribution(records),
        "frequency_by_k": frequency_by_k(by_day),
        "magnitude_by_k": magnitude_by_k(by_day),
        "posthoc_validation": posthoc_validation(by_day),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    from core.db import get_db
    sb = get_db(write=False)
    if sb is None:
        log.critical("Supabase not configured (SUPABASE_URL/SUPABASE_KEY) — cannot run")
        raise SystemExit(1)
    records = fetch_records(sb)
    result = run_audit(records)
    import json
    print(json.dumps(result, indent=2, default=str))
