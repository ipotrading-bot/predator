"""
core/learning_layer.py — PAIM v8.5 — Adaptive Thresholds (Bayesian Learning)
Reads last 50 closed signals per sport. Adjusts MIN_EDGE thresholds based on
CLV hit-rate: if fewer than 60% beat the closing line, the sport is too noisy
and the threshold rises; above 80% hit-rate it can come back down.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("LEARN")

# Per-sport baseline — uniquement les sports actifs, seuil relevé à 2.0 % minimum.
# Objectif : moins de signaux, mais plus fiables (gagner ou ne pas jouer).
SPORT_DEFAULTS: dict[str, float] = {
    "soccer":      2.0,   # WC + Copa Lib + MLS + Brasileirão
    "basketball":  2.0,   # NBA Finals — marché très sharp, edge réel commence à 2%
    "hockey":      2.0,   # NHL Cup Finals
    "baseball":    2.0,   # MLB + KBO + NPB — lag timezone documenté
    "rugbyleague": 2.0,   # NRL
    "aussierules": 2.0,   # AFL
}
_THRESHOLD_MIN = 2.0   # Jamais en-dessous de 2.0% (relevé depuis 1.5%)
_THRESHOLD_MAX = 6.0   # Hard cap — relevé de 5.0 pour permettre ajustement sur sports bruyants
_STEP_UP       = 0.4   # Pénalisation plus forte si CLV hit-rate < 60% (relevé 0.3→0.4)
_STEP_DOWN     = 0.2   # Récompense inchangée — prudence sur la baisse de seuil
_MIN_SAMPLES   = 10    # Minimum closed signals before any adjustment
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


def compute_and_save(sb) -> dict[str, float]:
    """
    Re-compute thresholds from CLV history and persist them to `meta`.
    Returns the updated threshold dict (unchanged sports included).
    """
    current = load_thresholds(sb)
    updated = current.copy()
    now     = datetime.now(timezone.utc).isoformat()

    for sport in SPORT_DEFAULTS:
        try:
            res = (sb.table("signals")
                   .select("clv_pct")
                   .eq("sport", sport)
                   .in_("status", ["settled", "closed", "expired"])
                   .order("closed_at", desc=True)
                   .limit(50)
                   .execute())

            values = [r["clv_pct"] for r in (res.data or [])
                      if r.get("clv_pct") is not None]

            if len(values) < _MIN_SAMPLES:
                log.info("[%s] %d samples < %d — threshold unchanged (%.1f%%)",
                         sport, len(values), _MIN_SAMPLES, current[sport])
                continue

            hit_rate = sum(1 for c in values if c >= 0) / len(values)
            avg_clv  = sum(values) / len(values)
            old_t    = current[sport]

            if hit_rate < _TARGET_LO:
                new_t = min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2))
                reason = f"CLV rate {hit_rate*100:.0f}% < {_TARGET_LO*100:.0f}% → ↑"
            elif hit_rate > _TARGET_HI:
                new_t = max(_THRESHOLD_MIN, round(old_t - _STEP_DOWN, 2))
                reason = f"CLV rate {hit_rate*100:.0f}% > {_TARGET_HI*100:.0f}% → ↓"
            else:
                log.info("[%s] CLV rate %.0f%% in target — no change (%.1f%%)",
                         sport, hit_rate * 100, old_t)
                continue

            updated[sport] = new_t
            log.info("[%s] Threshold %.2f%% → %.2f%% | %s | avg CLV %.2f%%",
                     sport, old_t, new_t, reason, avg_clv)

            sb.table("meta").upsert({
                "key":        f"threshold_{sport}",
                "value":      str(new_t),
                "updated_at": now,
            }).execute()

        except Exception as e:
            log.error("learning_layer [%s]: %s", sport, e)

    return updated
