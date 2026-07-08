"""
core/learning_layer.py — PAIM v9.5 — Adaptive Thresholds (Bayesian Learning)
Reads last 50 closed signals per sport. Adjusts MIN_EDGE thresholds based on
the REAL win-rate (outcome column: WIN/LOSS from settle_signal()'s Gemini
score lookup) — never on clv_final. clv_final, for settle_signal()-produced
rows, is a re-derivation of the entry edge from the exact same scan-time
prices used to compute edge_pct — it is ~always >= 0 because MIN_EDGE
already rejected negative-edge signals before they were ever sent, so it is
positive independent of whether the bet won or lost. If fewer than 60% of
decisive (WIN/LOSS) bets win, the sport is too noisy and the threshold
rises; above 82% win-rate it can come back down.
"""
import logging
from datetime import datetime, timezone

from core.stats_utils import p_breakeven, wilson_ci

log = logging.getLogger("LEARN")

# Per-sport baseline — uniquement les sports actifs, seuil relevé à 2.0 % minimum.
# Objectif : moins de signaux, mais plus fiables (gagner ou ne pas jouer).
SPORT_DEFAULTS: dict[str, float] = {
    "soccer":      1.2,   # WC + Copa Lib + MLS + Brasileirão + Amicaux — abaissé pour capturer plus de signaux
    "basketball":  1.5,   # NBA Finals — abaissé ; prob gate 0.65 bloquait tous les signaux
    "hockey":      2.0,   # NHL Cup Finals
    "baseball":    2.0,   # MLB + KBO + NPB — lag timezone documenté
    "rugbyleague": 2.0,   # NRL
    "aussierules": 2.0,   # AFL
    # Sports Gemini-only (core/harvester.py fetch_mma_events/fetch_esports_events/
    # fetch_alternative_sports_batch) — pas de source OddsAPI dédiée, moins de
    # données de calibration. Absents d'ici jusqu'ici, ils étaient scannés et
    # génèraient des signaux sans jamais participer à l'apprentissage adaptatif
    # (dyn_thresholds.get() retombait silencieusement sur MIN_EDGE global).
    # Seuil conservateur, aligné sur les sports les plus "bruyants" ci-dessus.
    "mma":         2.0,
    "esports":     2.0,
    "tabletennis": 2.0,
    "volleyball":  2.0,
    "handball":    2.0,
}
_THRESHOLD_MIN = 1.0   # Floor soccer — permet de capter amicaux et WC avec petit edge
_THRESHOLD_MAX = 6.0   # Hard cap — relevé de 5.0 pour permettre ajustement sur sports bruyants
_STEP_UP       = 0.4   # Pénalisation plus forte si CLV hit-rate < 60% (relevé 0.3→0.4)
_STEP_DOWN     = 0.2   # Récompense inchangée — prudence sur la baisse de seuil
_MIN_SAMPLES   = 30    # Minimum closed signals before any adjustment (relevé 10→30, Task 4:
                       # 10 decisive samples is not enough to distinguish a real edge from noise)
_TARGET_LO     = 0.60  # Below this  → raise threshold (too many weak signals)
_TARGET_HI     = 0.82  # Above this  → lower threshold (relevé 0.80→0.82 : plus exigeant)


def load_thresholds(sb) -> dict[str, float]:
    """
    Load sport-specific MIN_EDGE from Supabase `meta` table.
    Falls back to SPORT_DEFAULTS if the keys don't exist yet.
    """
    result = SPORT_DEFAULTS.copy()
    try:
        res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
        for row in (res.data or []):
            sport = row["key"].replace("threshold_", "")
            if sport in result:
                result[sport] = float(row["value"])
    except Exception as e:
        log.warning("load_thresholds: %s — using defaults", e)
    return result


_DECISIVE_OUTCOMES = ("WIN", "LOSS")   # PUSH/UNKNOWN/closed/expired carry no real result


def _sport_stats(rows: list[dict]) -> dict:
    """
    Real performance stats from a batch of ai_learning_ledger rows.
    Keyed exclusively off `outcome` — never `clv_final` (see module
    docstring). PUSH/UNKNOWN/'closed'/'expired' rows are excluded from both
    hit_rate and ROI: they carry no decisive WIN/LOSS result.

    roi = Σ(kelly_pct·(odds-1)) if WIN else -kelly_pct, / Σ(kelly_pct) —
    kelly_pct stands in for stake size (mise); since it's a % of the same
    fixed reference bankroll for every row, it cancels correctly in the
    ratio without needing the absolute € amount. Rows missing kelly_pct
    (pre-migration, or odds<=0) are skipped for ROI but still count for
    hit_rate.
    """
    decisive = [r for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES]
    n = len(decisive)
    if n == 0:
        return {"n": 0, "hit_rate": None, "roi": None, "wilson_lower": None, "p_breakeven": None}

    wins = sum(1 for r in decisive if r["outcome"] == "WIN")
    hit_rate = wins / n
    wilson_lower, _ = wilson_ci(wins, n)

    odds_vals = [r["odds"] for r in decisive if r.get("odds")]
    avg_odds = sum(odds_vals) / len(odds_vals) if odds_vals else None
    breakeven = p_breakeven(avg_odds) if avg_odds else None

    staked = [r for r in decisive if r.get("kelly_pct") and r.get("odds")]
    if staked:
        numer = sum(r["kelly_pct"] * (r["odds"] - 1) if r["outcome"] == "WIN" else -r["kelly_pct"]
                    for r in staked)
        denom = sum(r["kelly_pct"] for r in staked)
        roi = numer / denom if denom else None
    else:
        roi = None

    return {
        "n": n,
        "hit_rate": hit_rate,
        "roi": roi,
        "wilson_lower": wilson_lower,
        "p_breakeven": breakeven,
    }


def compute_and_save(sb) -> dict[str, float]:
    """
    Re-compute thresholds from real WIN/LOSS history and persist them to
    `meta`. Returns the updated threshold dict (unchanged sports included).
    """
    current = load_thresholds(sb)
    updated = current.copy()
    now     = datetime.now(timezone.utc).isoformat()

    for sport in SPORT_DEFAULTS:
        try:
            res = (sb.table("ai_learning_ledger")
                   .select("outcome, kelly_pct, odds")
                   .eq("sport", sport)
                   .order("created_at", desc=True)
                   .limit(50)
                   .execute())

            stats = _sport_stats(res.data or [])
            if stats["n"] < _MIN_SAMPLES:
                log.info("[%s] %d decisive samples < %d — threshold unchanged (%.1f%%)",
                         sport, stats["n"], _MIN_SAMPLES, current[sport])
                continue

            hit_rate = stats["hit_rate"]
            old_t    = current[sport]

            if hit_rate < _TARGET_LO:
                # Raising the threshold is always the safe direction —
                # never gated on statistical significance: being extra
                # conservative on a noisy bad sample costs nothing.
                new_t = min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2))
                reason = f"win rate {hit_rate*100:.0f}% < {_TARGET_LO*100:.0f}% → ↑"
            elif hit_rate > _TARGET_HI:
                # Task 4: only relax the threshold (make it easier to pass)
                # if we're statistically confident — Wilson 95% CI lower
                # bound above the segment's tax-adjusted breakeven
                # probability — that it's genuinely profitable. An observed
                # 82%+ hit rate on a small/noisy sample can still have a CI
                # lower bound under breakeven; don't loosen on that alone.
                if (stats["wilson_lower"] is not None and stats["p_breakeven"] is not None
                        and stats["wilson_lower"] < stats["p_breakeven"]):
                    log.info("[%s] win rate %.0f%% > target but Wilson lower bound %.1f%% < "
                             "breakeven %.1f%% (n=%d) — not statistically significant, "
                             "threshold unchanged (%.1f%%)",
                             sport, hit_rate * 100, stats["wilson_lower"] * 100,
                             stats["p_breakeven"] * 100, stats["n"], old_t)
                    continue
                new_t = max(_THRESHOLD_MIN, round(old_t - _STEP_DOWN, 2))
                reason = f"win rate {hit_rate*100:.0f}% > {_TARGET_HI*100:.0f}% → ↓"
            else:
                log.info("[%s] win rate %.0f%% in target — no change (%.1f%%)",
                         sport, hit_rate * 100, old_t)
                continue

            updated[sport] = new_t
            roi_str = f"{stats['roi']*100:+.1f}%" if stats["roi"] is not None else "n/a"
            log.info("[%s] Threshold %.2f%% → %.2f%% | %s | n=%d | ROI %s",
                     sport, old_t, new_t, reason, stats["n"], roi_str)

            sb.table("meta").upsert({
                "key":        f"threshold_{sport}",
                "value":      str(new_t),
                "updated_at": now,
            }).execute()

        except Exception as e:
            log.error("learning_layer [%s]: %s", sport, e)

    return updated
