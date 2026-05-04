"""
core/paim_engine.py — Moteur PAIM (5 piliers stochastiques)
Lead Layer · Lag Layer · Bayesian Filter · Binary Synthesis · Dynamic Allocation
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ═══════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class MarketOdds:
    """Cotes brutes d'un bookmaker pour un événement binaire."""
    bookmaker: str
    event_id: str
    market_key: str           # h2h, spreads, totals
    outcome_home: float       # Cote brute outcome A
    outcome_away: float       # Cote brute outcome B
    timestamp: float          # Unix timestamp de capture


@dataclass
class PAIMSignal:
    """Résultat complet d'un cycle PAIM."""
    event_id: str
    market_key: str
    selection: str            # 'home' | 'away'
    bookmaker_target: str     # où parier (Soft)
    sharp_prob: float         # π issu de Pinnacle (Shin)
    implied_prob_soft: float  # probabilité implicite du Soft
    ev_plus: float            # Expected Value > 0
    snr_ratio: float          # Signal-to-Noise Ratio
    recommended_stake: float  # mise post-Kelly + Smart Staking
    clv_estimate: float       # Closing Line Value estimée
    is_valid: bool = False    # validé par Bayesian Filter
    ai_context: str = ""      # résumé contexte Gemini

    @property
    def label(self) -> str:
        return f"EV+{self.ev_plus:.1%} | SNR {self.snr_ratio:.2f} | {self.selection.upper()}"


# ═══════════════════════════════════════════════════════════════════
# PILIER 1 — LEAD LAYER : Shin's Method
# ═══════════════════════════════════════════════════════════════════

class ShinDemarger:
    """
    Extrait la probabilité 'vraie' (π) depuis les cotes bookmaker
    en décomposant la marge en composante insider (z) et markup.
    Méthode: Shin (1993) — 'Measuring the Incidence of Insider Trading'.
    """

    @staticmethod
    def deMargin_additive(odds: list[float]) -> list[float]:
        """Démargeage additif simple (baseline rapide)."""
        total_implied = sum(1 / o for o in odds)
        return [1 / (o * total_implied) for o in odds]

    @staticmethod
    def _bisection_root(func, a, b, tol=1e-8, max_iter=1000):
        """
        Méthode de la bisection pour trouver la racine d'une fonction.
        Remplace scipy.optimize.brentq pour éviter la dépendance scipy.
        """
        fa = func(a)
        fb = func(b)

        if fa * fb > 0:
            raise ValueError(
                f"La fonction ne change pas de signe sur [{a}, {b}]. "
                f"f({a})={fa}, f({b})={fb}"
            )

        if fa == 0:
            return a
        if fb == 0:
            return b

        for _ in range(max_iter):
            c = (a + b) / 2.0
            fc = func(c)

            if abs(fc) < tol or (b - a) / 2.0 < tol:
                return c

            if fa * fc < 0:
                b = c
                fb = fc
            else:
                a = c
                fa = fc

        return (a + b) / 2.0

    @staticmethod
    def shin_probabilities(odds: list[float]) -> tuple[list[float], float]:
        """
        Résolution numérique du paramètre z (proportion insider).
        Retourne (probabilités_vraies, z_insider_proportion).
        Utilise la méthode de la bisection (pure Python) au lieu de scipy.
        """
        n = len(odds)
        implied = [1 / o for o in odds]
        total_margin = sum(implied)

        def shin_equation(z: float) -> float:
            """f(z) = 0 selon la condition Shin."""
            if z <= 0 or z >= 1:
                return float("inf")
            pis = []
            for qi in implied:
                discriminant = z**2 + 4 * (1 - z) * qi**2 / total_margin
                if discriminant < 0:
                    return float("inf")
                pi = (math.sqrt(discriminant) - z) / (2 * (1 - z))
                pis.append(pi)
            return sum(pis) - 1.0

        try:
            z = ShinDemarger._bisection_root(shin_equation, 1e-6, 0.2, tol=1e-8)
        except ValueError:
            return ShinDemarger.deMargin_additive(odds), 0.0

        probs = []
        for qi in implied:
            discriminant = z**2 + 4 * (1 - z) * qi**2 / total_margin
            pi = (math.sqrt(discriminant) - z) / (2 * (1 - z))
            probs.append(pi)

        total = sum(probs)
        probs = [p / total for p in probs]
        return probs, z


# ═══════════════════════════════════════════════════════════════════
# PILIER 2 — LAG LAYER : Ornstein-Uhlenbeck Latency Model
# ═══════════════════════════════════════════════════════════════════

class OULatencyModel:
    """
    Modélise la vitesse de convergence d'un bookmaker Soft vers le Sharp.
    Utilise le processus d'Ornstein-Uhlenbeck : dX = θ(μ - X)dt + σ dW
    """

    def __init__(self, theta: float = 0.3, sigma: float = 0.02):
        self.theta = theta
        self.sigma = sigma

    def expected_convergence_time(self, gap: float) -> float:
        """
        Temps espéré (minutes) pour que le Soft comble `gap` d'inefficacité.
        gap = |prob_sharp - prob_soft|
        """
        if gap <= 0 or self.theta <= 0:
            return 0.0
        return -math.log(1 - min(gap / (self.sigma + 1e-9), 0.99)) / self.theta * 60

    def inefficiency_score(
        self, sharp_prob: float, soft_prob: float, dt_minutes: float
    ) -> float:
        """
        Score d'inefficacité résiduelle après `dt_minutes` minutes.
        Score ∈ [0, 1] — plus élevé = meilleure opportunité.
        """
        gap = abs(sharp_prob - soft_prob)
        decay = math.exp(-self.theta * dt_minutes / 60)
        residual = gap * decay
        return min(residual / (self.sigma + 1e-9), 1.0)


# ═══════════════════════════════════════════════════════════════════
# PILIER 3 — BAYESIAN FILTER : Signal-to-Noise Ratio
# ═══════════════════════════════════════════════════════════════════

class BayesianSNRFilter:
    """
    Filtre bayésien pour rejeter les Trap Lines.
    SNR = signal_strength / noise_floor
    """

    def __init__(self, min_snr: float = 1.5):
        self.min_snr = min_snr
        self._history: list[tuple[float, float]] = []

    def compute_snr(
        self,
        ev: float,
        volatility: float,
        n_confirming_books: int,
        volume_ratio: float = 1.0,
    ) -> float:
        """
        SNR composite basé sur:
        - EV (force du signal)
        - Volatilité des cotes (bruit)
        - Nombre de bookmakers confirmant le mouvement
        - Ratio volume sharp/soft
        """
        if volatility <= 0:
            volatility = 0.001
        signal = ev * math.log1p(n_confirming_books) * volume_ratio
        noise = volatility
        return signal / noise

    def update_brier(self, predicted_prob: float, outcome: int) -> float:
        """Met à jour et retourne le Brier Score glissant (derniers 100 paris)."""
        self._history.append((predicted_prob, outcome))
        if len(self._history) > 100:
            self._history.pop(0)
        brier = np.mean([(p - o) ** 2 for p, o in self._history])
        return float(brier)

    def is_valid_signal(self, snr: float) -> bool:
        return snr >= self.min_snr


# ═══════════════════════════════════════════════════════════════════
# PILIER 4 — BINARY SYNTHESIS
# ═══════════════════════════════════════════════════════════════════

class BinarySynthesizer:
    """
    Transforme tout marché complexe en issue binaire.
    Priorité: Asian Handicap 0.0, Moneyline, Over/Under.
    """

    BINARY_MARKETS = {"h2h", "spreads", "totals", "asian_handicap"}

    @staticmethod
    def is_binary_market(market_key: str) -> bool:
        return any(bm in market_key for bm in BinarySynthesizer.BINARY_MARKETS)

    # Commission moyenne 1XBet sur les marchés binaires
    COMMISSION_1XBET = 0.035  # 3.5%

    @staticmethod
    def compute_ev(true_prob: float, offered_odds: float, commission: float = 0.0) -> float:
        """
        EV_net = (prob_vraie × (cote_net - 1)) - (1 - prob_vraie)
        
        Où cote_net = offered_odds × (1 - commission)
        
        commission: Frais implicite du bookmaker soft.
        Par défaut: 3.5% pour 1XBet sur les marchés binaires.
        
        Formule complète:
        EV_net = [Cote_Soft × (1 - Comm_Soft)] / Cote_Fair_Sharp - 1
        """
        # Déduction commission bookmaker (défaut: 3.5% pour 1XBet)
        net_odds = offered_odds * (1.0 - commission)
        return true_prob * (net_odds - 1.0) - (1.0 - true_prob)

    @staticmethod
    def best_binary_selection(
        sharp_probs: list[float],
        soft_odds_home: float,
        soft_odds_away: float,
        commission: float = 0.0,
    ) -> tuple[str, float, float]:
        """
        Retourne (sélection, EV, cote_soft) pour la meilleure issue binaire.
        
        Args:
            commission: Frais du bookmaker (3.5% pour 1XBet par défaut)
        """
        ev_home = BinarySynthesizer.compute_ev(sharp_probs[0], soft_odds_home, commission)
        ev_away = BinarySynthesizer.compute_ev(sharp_probs[1], soft_odds_away, commission)

        if ev_home >= ev_away:
            return "home", ev_home, soft_odds_home
        return "away", ev_away, soft_odds_away


# ═══════════════════════════════════════════════════════════════════
# PILIER 5 — DYNAMIC ALLOCATION : Fractional Kelly
# ═══════════════════════════════════════════════════════════════════

class FractionalKelly:
    """
    Critère de Kelly fractionné (0.25) pour l'optimisation du capital.
    f* = (bp - q) / b  →  mise_kelly = f* × fraction × bankroll
    """

    def __init__(self, fraction: float = 0.25):
        self.fraction = fraction

    def kelly_stake(
        self, true_prob: float, offered_odds: float, bankroll: float
    ) -> float:
        """Calcule la mise Kelly fractionnée en euros."""
        b = offered_odds - 1
        p = true_prob
        q = 1 - p
        f_star = (b * p - q) / b
        f_star = max(f_star, 0.0)
        return f_star * self.fraction * bankroll

    @staticmethod
    def smart_stake_round(stake: float, base: int = 10) -> float:
        """Arrondi au multiple de `base` le plus proche (obfuscation)."""
        return round(stake / base) * base


# ═══════════════════════════════════════════════════════════════════
# ORCHESTRATEUR PAIM
# ═══════════════════════════════════════════════════════════════════

class PAIMEngine:
    """
    Orchestre les 5 piliers pour produire un PAIMSignal complet.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        min_ev: float = 0.08,
        min_snr: float = 1.5,
        stake_base: int = 10,
    ):
        self.shin = ShinDemarger()
        self.ou = OULatencyModel()
        self.snr_filter = BayesianSNRFilter(min_snr=min_snr)
        self.synthesizer = BinarySynthesizer()
        self.kelly = FractionalKelly(fraction=kelly_fraction)
        self.min_ev = min_ev
        self.stake_base = stake_base

    def process(
        self,
        sharp: MarketOdds,
        soft: MarketOdds,
        bankroll: float,
        dt_minutes: float = 0.0,
        n_confirming_books: int = 1,
    ) -> Optional[PAIMSignal]:
        """
        Pipeline complet : Sharp odds → PAIMSignal ou None si filtré.
        """
        # 1. Lead Layer — Shin
        sharp_odds = [sharp.outcome_home, sharp.outcome_away]
        sharp_probs, z_insider = self.shin.shin_probabilities(sharp_odds)

        # 2. Binary Synthesis
        if not BinarySynthesizer.is_binary_market(soft.market_key):
            return None

        selection, ev, soft_cote = self.synthesizer.best_binary_selection(
            sharp_probs, soft.outcome_home, soft.outcome_away,
            commission=self.synthesizer.COMMISSION_1XBET  # 3.5% pour 1XBet
        )

        if ev < self.min_ev:
            return None

        # 3. Lag Layer — OU inefficiency
        soft_implied = [1 / soft.outcome_home, 1 / soft.outcome_away]
        soft_prob = soft_implied[0] if selection == "home" else soft_implied[1]
        sharp_prob = sharp_probs[0] if selection == "home" else sharp_probs[1]

        volatility = abs(sharp_prob - soft_prob) * 0.5 + 0.01
        ineff_score = self.ou.inefficiency_score(sharp_prob, soft_prob, dt_minutes)

        # 4. Bayesian SNR Filter
        snr = self.snr_filter.compute_snr(
            ev=ev,
            volatility=volatility,
            n_confirming_books=n_confirming_books,
            volume_ratio=1 + ineff_score,
        )

        if not self.snr_filter.is_valid_signal(snr):
            return None

        # 5. Dynamic Allocation — Kelly
        raw_stake = self.kelly.kelly_stake(sharp_prob, soft_cote, bankroll)
        stake = FractionalKelly.smart_stake_round(raw_stake, self.stake_base)

        clv_estimate = ev * 0.7

        return PAIMSignal(
            event_id=sharp.event_id,
            market_key=sharp.market_key,
            selection=selection,
            bookmaker_target=soft.bookmaker,
            sharp_prob=sharp_prob,
            implied_prob_soft=soft_prob,
            ev_plus=ev,
            snr_ratio=snr,
            recommended_stake=stake,
            clv_estimate=clv_estimate,
            is_valid=True,
        )
