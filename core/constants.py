"""
core/constants.py — PAIM v9.5 — Single Source of Truth
All thresholds, risk classification, and Kelly calculation used by engine,
rapport, dashboard, and audit must import from here — never redefined inline.
"""
from core.tax_engine import min_edge_required as _min_edge_required

# ELITE_EDGE / SOCCER_ELITE_EDGE / BASKETBALL_ELITE_EDGE are display-tier
# boundaries (risk_flag() below: LOW_VALUE/VALUE/HIGH_VALUE for the
# dashboard), not a send/no-send gate — left as fixed, sport-calibrated
# values. The actual "never send a signal that can't beat tax" gate is
# min_edge_for_k() below, enforced per-signal (using that signal's own
# true probability, not a generic reference) in core/paim_engine.py's
# compute_alpha().
ELITE_EDGE             = 2.5    # % — VALUE / HIGH_VALUE boundary (défaut)
SOCCER_ELITE_EDGE      = 1.5    # % — Soccer AH0 : marché serré, VALUE dès 1.5%
BASKETBALL_ELITE_EDGE  = 2.0    # % — NBA Finales : VALUE dès 2.0% (edges typiques 1.5–2.5%)
AH0_VALUE_THRESHOLD  = 1.5    # Soccer DNB : favori à 1.5+ = valeur intrinsèque
PURGE_EDGE_FLOOR     = 0.5    # % — floor purge : ne jamais supprimer au-dessus de ça
MIN_STAKE    = 2      # € — below this Kelly stake, signal is not actionable
BANKROLL_REF = 150    # € — 100 000 XOF (taux fixe 655.96 XOF/€)
MAX_EDGE     = 15.0   # % — hard cap; above = data mapping error, reject
SUSPECT_EDGE = 10.0   # % — safety trigger: major sport edge above this = SUSPECT_DATA (cap totals=15%)

# ── Tax (PAIM v9.5) ───────────────────────────────────────────────────
TAX_RATE = 0.20   # % withheld on net profit of a winning bet — see core/tax_engine.py


def min_edge_for_k(k: int, true_prob: float = 0.55, tax_rate: float = TAX_RATE) -> float:
    """
    Tax-aware minimum edge (%) required for a k-leg system at a given true
    probability, with a 15% safety margin over the theoretical net-of-tax
    breakeven (core.tax_engine.min_edge_required). Replaces the old fixed
    MIN_EDGE floor, which didn't account for the bookmaker-withholding tax
    at all — a signal could clear the old 1.2% floor and still be a
    guaranteed loser once TAX_RATE is applied to its winnings.
    """
    return round(_min_edge_required(k, true_prob, tax_rate) * 1.15, 4)

# ── Retry & Rate Limiting (Centralized) ──────────────────────────────
DELAY_XBET_MIN       = 2.0      # Seconds — min delay between 1XBet requests
DELAY_XBET_MAX       = 5.0      # Seconds — max delay (random jitter)
DELAY_GEMINI_RATE    = 65.0     # Seconds — Gemini rate limit backoff
DELAY_DB_RETRY       = 1.0      # Seconds — Supabase transient error retry
MAX_DB_RETRIES       = 3        # Attempts — max retries before giving up
GLOBAL_TIMEOUT       = 540      # Seconds — 9 minutes, safety net for GitHub Actions
DEBUG_MODE           = False    # Will be set from env var PREDATOR_DEBUG

# ── Round-line push penalty (totals on integer lines e.g. 9.0, 8.0) ────
# P(push) on MLB integer totals historically ~8–12% (score lands exactly on line).
# Reduces effective sharp_prob: p_adj = p_win / (1 - p_push)
# Half-lines (.5) have P(push)=0 — no adjustment needed.
PUSH_PROB_ROUND_LINE = 0.10    # 10% — conservative MLB/baseball estimate

# ── MLB Totals lineup confirmation window ─────────────────────────────
# Starting pitchers are officially confirmed ~1h before first pitch.
# Signals generated more than MLB_LINEUP_WINDOW_H hours before game time
# are based on a total line that doesn't yet reflect the actual starter.
# ERA, recent form, and matchup are not priced in until lineup is locked.
MLB_LINEUP_WINDOW_H  = 6       # Hours — discard MLB totals signals beyond this

# Fractional Kelly par sport — uniquement les 6 sport-types actifs sélectionnés
# (à ne pas confondre avec les 19 SPORT_KEYS d'odds_api.py, plus fins : plusieurs
# ligues/compétitions collapsent vers le même sport-type ici)
# Principe : sharper market = fraction plus haute = mise plus agressive
# Sports exclus (cricket, darts, boxing, tennis, etc.) → retirés pour réduire bruit
#
# TEMPORARILY REDUCED to the 0.10-0.15 band (Task 10, PAIM v9.5) — was
# 0.20-0.30. Tasks 1 (real outcome-based learning), 3 (real closing-line
# CLV) and 4 (Wilson-CI-gated thresholds, _MIN_SAMPLES=30) only just
# started producing trustworthy signal on this run; until each segment
# has >=30 real settled signals validating a genuine post-tax edge
# (core/learning_layer.py's Wilson-CI gate is the actual check — see
# _sport_stats), staking at the old, more aggressive fractions would size
# real money on a still-unvalidated edge. Relative ordering (sharper
# market = higher fraction) is preserved, just compressed into the
# smaller band. Restore the original values once validated.
KELLY_FRACTION = {
    "basketball":  0.15,   # NBA — marché le plus sharp au monde, confiance max
    "hockey":      0.13,   # NHL — sharp, liquid, peu de bruit
    "soccer":      0.12,   # FIFA WC + Copa Lib + MLS + Brasileirão — relevé légèrement
    "baseball":    0.12,   # MLB + KBO + NPB — volume élevé + lag timezone documenté
    "rugbyleague": 0.10,   # NRL — Pinnacle très sharp, marché australien fiable
    "aussierules": 0.10,   # AFL — Pinnacle + Betfair très actifs
}


def risk_flag(edge_pct: float, elite: float = ELITE_EDGE) -> str:
    """Consistent risk label stored in DB and used by all consumers.
    `elite` lets callers pass a sport-specific boundary (SOCCER_ELITE_EDGE,
    BASKETBALL_ELITE_EDGE) — defaults to the generic ELITE_EDGE."""
    if edge_pct >= elite * 2:
        return "HIGH_VALUE"
    if edge_pct >= elite:
        return "VALUE"
    return "LOW_VALUE"


def kelly_stake(xbet_odd: float, sharp_prob: float,
                bankroll: int = BANKROLL_REF,
                sport: str = "soccer",
                current_exposure: float = 0.0) -> int:
    """
    Fractional Kelly adaptatif par sport — fraction dans KELLY_FRACTION.
    Returns 0 (non-actionable) if computed stake < MIN_STAKE.

    `current_exposure` (Task 7, core/risk_manager.py) is capital already
    committed to other active signals — the effective bankroll available
    for THIS stake is reduced by it, so a portfolio already at or past its
    exposure cap (core.risk_manager.MAX_EXPOSURE_PCT) naturally sizes new
    stakes down to 0 instead of stacking risk on top of risk. Defaults to
    0 (no reduction) for callers that haven't computed exposure.
    """
    b = xbet_odd - 1
    if b <= 0 or sharp_prob <= 0:
        return 0
    effective_bankroll = max(0.0, bankroll - current_exposure)
    if effective_bankroll <= 0:
        return 0
    kf = (sharp_prob * b - (1 - sharp_prob)) / b
    fraction = KELLY_FRACTION.get(sport, 0.12)   # fallback also inside Task 10's temporary 0.10-0.15 band
    stake = round(max(0.0, kf * fraction) * effective_bankroll)
    return stake if stake >= MIN_STAKE else 0
