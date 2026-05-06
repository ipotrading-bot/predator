"""
core/paim_engine.py — Dataclasses & moteur PAIM central
Importé par : data/supabase_client.py, data/odds_fetcher.py, core/notifications.py

PAIM = Probabilistic Arbitrage Intelligence Machine
Doctrine : EV+ Bernoulli, Shin Method, Fractional Kelly
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import math
from integrations.api_sports_client import ApiSportsClient
from integrations.odds_api_client import OddsApiClient


# ═══════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class MarketOdds:
    """Cotes brutes d'un marché pour un bookmaker donné."""
    bookmaker: str
    event_id: str
    market_key: str          # h2h | spreads | totals
    outcome_home: float
    outcome_away: float
    outcome_draw: Optional[float] = None
    timestamp: float = 0.0


@dataclass
class PAIMSignal:
    """Signal validé par le moteur PAIM (EV+ confirmé)."""
    event_id: str
    market_key: str
    selection: str           # nom de l'outcome sélectionné
    bookmaker_target: str    # bookie où parier (ex: "1xbet")

    # Probabilités
    sharp_prob: float        # prob déduite des cotes sharp (Shin)
    implied_prob_soft: float # prob implicite du soft book

    # Métriques quant
    ev_plus: float           # Expected Value > 0 (ex: 0.08 = 8%)
    snr_ratio: float         # Signal-to-Noise Ratio ≥ 3.0
    clv_estimate: float      # Closing Line Value estimé

    # Staking
    recommended_stake: float # Mise Kelly fractionnaire (€)

    # Optionnel
    ai_context: str = ""
    is_elite: bool = False   # EV+ > 15%


@dataclass
class ValidationResult:
    """Résultat de validation contextuelle (IA + red flags)."""
    approved: bool
    context_summary: str
    red_flags: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class ScanResult:
    """Résumé d'un cycle de scan complet."""
    events_analyzed: int = 0
    signals_found: int = 0
    signals_validated: int = 0
    signals_rejected: int = 0
    duration_seconds: float = 0.0
    session_name: str = "auto"


# ═══════════════════════════════════════════════════════════════════
# MOTEUR MATHÉMATIQUE PAIM
# ═══════════════════════════════════════════════════════════════════

class PAIMEngine:
    """
    Moteur de calcul EV+ et Kelly fractionnaire.
    Doctrine PhD MIT : Bernoulli Trials uniquement (marchés binaires).
    """

    def __init__(self, kelly_fraction: float = 0.25, max_stake_pct: float = 0.03):
        self.kelly_fraction = kelly_fraction
        self.max_stake_pct = max_stake_pct
        self.sports_client = ApiSportsClient()
        self.odds_client = OddsApiClient()

    async def fetch_odds_data(self, sport: str) -> dict:
        """Fetches odds data for a given sport."""
        async with self.odds_client:
            return await self.odds_client.fetch_odds(sport)

    def compute_ev(self, sharp_prob: float, soft_odds: float) -> float:
        """
        EV+ = p_sharp * soft_odds - 1
        Valeur positive = edge sur le marché.
        """
        return sharp_prob * soft_odds - 1.0

    def compute_snr(self, sharp_prob: float, implied_prob_soft: float) -> float:
        """
        SNR = signal / noise = EV+ / (implied_prob - sharp_prob)
        Seuil minimum : 3.0
        """
        noise = abs(implied_prob_soft - sharp_prob)
        if noise < 1e-6:
            return 0.0
        ev = self.compute_ev(sharp_prob, 1.0 / implied_prob_soft)
        return ev / noise

    def compute_kelly_stake(
        self, sharp_prob: float, soft_odds: float, bankroll: float
    ) -> float:
        """
        Kelly fractionnaire : f* = (p*b - q) / b * kelly_fraction
        Plafonné à max_stake_pct du bankroll.
        """
        b = soft_odds - 1.0
        q = 1.0 - sharp_prob
        if b <= 0:
            return 0.0
        kelly_full = (sharp_prob * b - q) / b
        kelly_frac = max(0.0, kelly_full * self.kelly_fraction)
        max_stake = bankroll * self.max_stake_pct
        raw_stake = kelly_frac * bankroll
        # Arrondi à la dizaine inférieure
        stake = min(raw_stake, max_stake)
        return math.floor(stake / 10) * 10 if stake >= 10 else round(stake, 2)

    def compute_clv_estimate(
        self, sharp_prob: float, implied_prob_soft: float
    ) -> float:
        """
        CLV (Closing Line Value) estimé = edge capturé sur le closing.
        Proxy : différence de probabilité normalisée.
        """
        return (sharp_prob - implied_prob_soft) / implied_prob_soft

    def to_binary_probs(self, odds_1n2: dict) -> dict:
        """
        Convertit les cotes 1N2 en probabilités de Double Chance.
        Odds_1n2 : {'home': float, 'draw': float, 'away': float}
        """
        # Convert to implied probabilities
        probs = {k: 1.0 / v for k, v in odds_1n2.items() if v > 0}
        total = sum(probs.values())
        if total == 0:
            return {}

        # Normalize probabilities
        norm_probs = {k: v / total for k, v in probs.items()}

        h = norm_probs.get('home', 0.0)
        d = norm_probs.get('draw', 0.0)
        a = norm_probs.get('away', 0.0)

        return {
            'home_or_draw': h + d,
            'home_or_away': h + a,
            'draw_or_away': d + a
        }

    def compute_confidence(self, alpha: float, liquidity_score: float, ia_score: float) -> float:
        """
        Calcule le score de confiance du signal (alpha 40%, liquidité 30%, IA 30%).
        """
        return (alpha * 0.4) + (liquidity_score * 0.3) + (ia_score * 0.3)

    def evaluate_signal(
        self,
        event_id: str,
        market_key: str,
        selection: str,
        bookmaker_target: str,
        sharp_prob: float,
        soft_odds: float,
        bankroll: float,
        min_ev: float = 0.08,
        min_snr: float = 3.0,
    ) -> Optional[PAIMSignal]:
        """
        Évalue et retourne un PAIMSignal si les seuils sont respectés.
        Retourne None si le signal ne passe pas les filtres doctrinaires.
        """
        implied_prob_soft = 1.0 / soft_odds if soft_odds > 0 else 0.0
        ev = self.compute_ev(sharp_prob, soft_odds)
        snr = self.compute_snr(sharp_prob, implied_prob_soft)

        if ev < min_ev or snr < min_snr:
            return None

        stake = self.compute_kelly_stake(sharp_prob, soft_odds, bankroll)
        clv = self.compute_clv_estimate(sharp_prob, implied_prob_soft)

        return PAIMSignal(
            event_id=event_id,
            market_key=market_key,
            selection=selection,
            bookmaker_target=bookmaker_target,
            sharp_prob=sharp_prob,
            implied_prob_soft=implied_prob_soft,
            ev_plus=ev,
            snr_ratio=snr,
            clv_estimate=clv,
            recommended_stake=stake,
            is_elite=(ev >= 0.15),
        )
