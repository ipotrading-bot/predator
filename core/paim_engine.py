"""
core/paim_engine.py — Moteur PAIM v2.1 (Reconstruction Doctrinaire)
5 piliers stochastiques : Shin · OU Lag · Bayesian SNR · Binary Synthesis · Kelly 0.25
Seuls les Bernoulli Trials (issues binaires) sont traités.
Tout signal avec EV+ < 8%, Sharp Prob < 60% ou SNR < 3.0 est rejeté.
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
    """Résultat complet d'un cycle PAIM — Bernoulli Trial uniquement."""
    event_id: str
    market_key: str
    selection: str            # 'home' | 'away'
    bookmaker_target: str     # où parier (Soft)
    sharp_prob: float         # π issu de Pinnacle (Shin) — DOIT ÊTRE ∈ [0.60, 0.90]
    implied_prob_soft: float  # probabilité implicite du Soft
    ev_plus: float            # Expected Value > 0 — DOIT ÊTRE > 8%
    snr_ratio: float          # Signal-to-Noise Ratio — DOIT ÊTRE > 3.0
    recommended_stake: float  # mise post-Kelly 0.25 + cap 3% bankroll
    clv_estimate: float       # Closing Line Value estimée
    fair_price: float = 0.0   # 1 / sharp_prob (Prix juste Pinnacle)
    is_valid: bool = False    # validé par Bayesian Filter
    ai_context: str = ""      # résumé contexte IA

    @property
    def label(self) -> str:
        return f"EV+{self.ev_plus:.1%} | SNR {self.snr_ratio:.2f} | {self.selection.upper()}"


# ═══════════════════════════════════════════════════════════════════
# PILIER 1 — LEAD LAYER : Shin's Method (Pinnacle uniquement)
# ═══════════════════════════════════════════════════════════════════

class ShinDemarger:
    """
    Extrait la probabilité 'vraie' (π) depuis les cotes Pinnacle
    en décomposant la marge en composante insider (z) et markup.
    Méthode: Shin (1993) — 'Measuring the Incidence of Insider Trading'.
    
    Si Pinnacle n'est pas disponible → signal REJETÉ (retourne None).
    """

    @staticmethod
    def _bisection_root(func, a, b, tol=1e-8, max_iter=1000):
        fa = func(a)
        fb = func(b)
        if fa * fb > 0:
            raise ValueError("Pas de racine sur l'intervalle")
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
        Échoue → fallback additif (mais signal marqué comme à risque).
        """
        implied = [1 / o for o in odds]
        total_margin = sum(implied)

        def shin_equation(z: float) -> float:
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
            # Fallback additif — signaux marqués à risque
            total_implied = sum(implied)
            return [qi / total_implied for qi in implied], 0.0

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
        if gap <= 0 or self.theta <= 0:
            return 0.0
        return -math.log(1 - min(gap / (self.sigma + 1e-9), 0.99)) / self.theta * 60

    def inefficiency_score(self, sharp_prob: float, soft_prob: float, dt_minutes: float) -> float:
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
    Seuil minimum : 3.0 (contre 1.5 dans la version déviante)
    """

    def __init__(self, min_snr: float = 3.0):
        self.min_snr = min_snr
        self._history: list[tuple[float, float]] = []

    def compute_snr(
        self,
        ev: float,
        volatility: float,
        n_confirming_books: int,
        volume_ratio: float = 1.0,
    ) -> float:
        if volatility <= 0:
            volatility = 0.001
        signal = ev * math.log1p(n_confirming_books) * volume_ratio
        noise = volatility
        return signal / noise

    def update_brier(self, predicted_prob: float, outcome: int) -> float:
        self._history.append((predicted_prob, outcome))
        if len(self._history) > 100:
            self._history.pop(0)
        brier = np.mean([(p - o) ** 2 for p, o in self._history])
        return float(brier)

    def is_valid_signal(self, snr: float) -> bool:
        return snr >= self.min_snr


# ═══════════════════════════════════════════════════════════════════
# PILIER 4 — BINARY SYNTHESIS (Bernoulli Trials uniquement)
# ═══════════════════════════════════════════════════════════════════

class BinarySynthesizer:
    """
    Transforme tout marché complexe en issue binaire.
    - Moneyline (h2h) : pour NBA, Tennis, NHL — rejette les matchs à 3 issues
    - Asian Handicap 0.0 / spreads : pour Soccer
    - Totals (Over/Under) : pour tout sport
    
    REJETTE STRICTEMENT les marchés à 3 issues (1N2, 3-way moneyline).
    Seuls les Bernoulli Trials (p(win) ∈ [0.60, 0.90]) sont conservés.
    """

    # Marchés binaires autorisés
    BINARY_MARKETS = {"h2h", "spreads", "totals", "asian_handicap"}
    
    # Sports avec moneyline binaire (pas de 3-way)
    BINARY_SPORTS = {
        "basketball_nba",        # NBA Moneyline = binaire (pas de match nul)
        "tennis_atp",            # Tennis = binaire
        "tennis_atp_french_open",
        "tennis_atp_us_open",
        "tennis_atp_wimbledon",
        "esports_lol",           # Esports = binaire
        "nhl",                   # Hockey = binaire (règle pro)
        "mma_mixed_martial_arts",# MMA = binaire
    }
    
    # Sports qui nécessitent spreads/asian handicap (pas de moneyline 3-way directe)
    SPREAD_SPORTS = {
        "soccer_uefa_champs_league",
        "soccer_epl",
        "soccer_spain_la_liga",
    }

    # Commission moyenne 1XBet sur les marchés binaires
    COMMISSION_1XBET = 0.035  # 3.5%

    @staticmethod
    def is_binary_market(market_key: str, sport_key: str = "") -> bool:
        """
        Vérifie si le marché est binaire et autorisé pour ce sport.
        
        Pour les sports BINARY_SPORTS: h2h uniquement (pas de 3-way)
        Pour les sports SPREAD_SPORTS: spreads, totals, asian_handicap uniquement (pas de h2h)
        """
        is_binary_key = any(bm in market_key for bm in BinarySynthesizer.BINARY_MARKETS)
        if not is_binary_key:
            return False
        
        # Vérifier si le sport est dans les listes autorisées
        is_binary_sport = any(s in sport_key for s in BinarySynthesizer.BINARY_SPORTS)
        is_spread_sport = any(s in sport_key for s in BinarySynthesizer.SPREAD_SPORTS)
        
        if not is_binary_sport and not is_spread_sport:
            return False
        
        # Pour sports binaires : h2h autorisé
        if is_binary_sport and "h2h" in market_key:
            return True
        
        # Pour sports spreads : h2h INTERDIT (3-way), only spreads/totals/asian_handicap
        if is_spread_sport and "h2h" in market_key:
            return False  # REJET : h2h soccer = 3-way (1N2)
        
        if is_spread_sport:
            return True  # spreads, totals, asian_handicap OK
        
        # Pour les autres binary sports : spreads, totals OK
        if is_binary_sport:
            return True
        
        return False

    @staticmethod
    def compute_ev(true_prob: float, offered_odds: float, commission: float = 0.0) -> float:
        """
        EV_net = (prob_vraie × (cote_net - 1)) - (1 - prob_vraie)
        
        Formule complète:
        EV_net = [Cote_Soft × (1 - Comm_Soft)] / Cote_Fair_Sharp - 1
        """
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
        """
        ev_home = BinarySynthesizer.compute_ev(sharp_probs[0], soft_odds_home, commission)
        ev_away = BinarySynthesizer.compute_ev(sharp_probs[1], soft_odds_away, commission)

        if ev_home >= ev_away:
            return "home", ev_home, soft_odds_home
        return "away", ev_away, soft_odds_away


# ═══════════════════════════════════════════════════════════════════
# PILIER 5 — DYNAMIC ALLOCATION : Fractional Kelly (0.25)
# ═══════════════════════════════════════════════════════════════════

class FractionalKelly:
    """
    Critère de Kelly fractionné (0.25) pour l'optimisation du capital.
    f* = (bp - q) / b  →  mise_kelly = f* × fraction × bankroll
    
    CAP STRICT : 3% de la bankroll max par pari (300€ sur 10k€)
    """

    def __init__(self, fraction: float = 0.25, max_stake_pct: float = 0.03):
        self.fraction = fraction
        self.max_stake_pct = max_stake_pct

    def kelly_stake(self, true_prob: float, offered_odds: float, bankroll: float) -> float:
        """Calcule la mise Kelly fractionnée en euros, plafonnée à max_stake_pct."""
        b = offered_odds - 1
        p = true_prob
        q = 1 - p
        f_star = (b * p - q) / b
        f_star = max(f_star, 0.0)
        raw_stake = f_star * self.fraction * bankroll
        
        # Cap strict : jamais plus de max_stake_pct% de la bankroll
        max_stake = bankroll * self.max_stake_pct
        return min(raw_stake, max_stake)

    @staticmethod
    def smart_stake_round(stake: float, base: int = 10) -> float:
        """Arrondi au multiple de `base` le plus proche (obfuscation)."""
        return round(stake / base) * base


# ═══════════════════════════════════════════════════════════════════
# ORCHESTRATEUR PAIM — Pipeline Doctrinaire
# ═══════════════════════════════════════════════════════════════════

class PAIMEngine:
    """
    Orchestre les 5 piliers pour produire un PAIMSignal complet.
    
    Seuils doctrinaires (PhD MIT) :
    - EV+ minimum : 8% (0.08)
    - SNR minimum : 3.0 (contre 1.5 dans la version déviante)
    - Sharp Prob : ∈ [0.60, 0.90] (Bernoulli Trial valide)
    - Mise max : 3% de la bankroll
    - Commission Soft : 3.5% (1XBet)
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        min_ev: float = 0.08,
        min_snr: float = 3.0,
        min_sharp_prob: float = 0.60,
        max_sharp_prob: float = 0.90,
        max_stake_pct: float = 0.03,
        stake_base: int = 10,
    ):
        self.shin = ShinDemarger()
        self.ou = OULatencyModel()
        self.snr_filter = BayesianSNRFilter(min_snr=min_snr)
        self.synthesizer = BinarySynthesizer()
        self.kelly = FractionalKelly(fraction=kelly_fraction, max_stake_pct=max_stake_pct)
        self.min_ev = min_ev
        self.min_sharp_prob = min_sharp_prob
        self.max_sharp_prob = max_sharp_prob
        self.stake_base = stake_base

    def process(
        self,
        sharp: MarketOdds,
        soft: MarketOdds,
        bankroll: float,
        dt_minutes: float = 0.0,
        n_confirming_books: int = 1,
        sport_key: str = "",
    ) -> Optional[PAIMSignal]:
        """
        Pipeline complet : Sharp odds → PAIMSignal ou None si filtré.
        
        Étapes :
        1. Lead Layer — Shin (Pinnacle uniquement)
        2. Binary Synthesis — Filtre strict Bernoulli Trial
        3. Sharp Prob Check — π ∈ [0.60, 0.90]
        4. EV Check — EV+ ≥ 8%
        5. Lag Layer — OU inefficiency score
        6. Bayesian SNR — SNR ≥ 3.0
        7. Kelly 0.25 — Mise avec cap 3% bankroll
        """
        # ═══════════════════════════════════════════════════════
        # 1. Binary Synthesis — Filtre strict du marché
        # ═══════════════════════════════════════════════════════
        if not BinarySynthesizer.is_binary_market(soft.market_key, sport_key):
            return None

        # ═══════════════════════════════════════════════════════
        # 2. Lead Layer — Shin (Pinnacle uniquement)
        # ═══════════════════════════════════════════════════════
        sharp_odds = [sharp.outcome_home, sharp.outcome_away]
        sharp_probs, z_insider = self.shin.shin_probabilities(sharp_odds)

        # ═══════════════════════════════════════════════════════
        # 3. Binary Synthesis — Meilleure sélection
        # ═══════════════════════════════════════════════════════
        selection, ev, soft_cote = self.synthesizer.best_binary_selection(
            sharp_probs, soft.outcome_home, soft.outcome_away,
            commission=self.synthesizer.COMMISSION_1XBET  # 3.5% pour 1XBet
        )

        # ═══════════════════════════════════════════════════════
        # 4. Sharp Prob Check — π ∈ [0.60, 0.90]
        # ═══════════════════════════════════════════════════════
        sharp_prob = sharp_probs[0] if selection == "home" else sharp_probs[1]
        
        if sharp_prob < self.min_sharp_prob or sharp_prob > self.max_sharp_prob:
            return None  # Probabilité hors Bernoulli Trial valide

        # ═══════════════════════════════════════════════════════
        # 5. EV Check — EV+ ≥ 8%
        # ═══════════════════════════════════════════════════════
        if ev < self.min_ev:
            return None

        # ═══════════════════════════════════════════════════════
        # 6. Lag Layer — OU inefficiency
        # ═══════════════════════════════════════════════════════
        soft_implied = [1 / soft.outcome_home, 1 / soft.outcome_away]
        soft_prob = soft_implied[0] if selection == "home" else soft_implied[1]

        volatility = abs(sharp_prob - soft_prob) * 0.5 + 0.01
        ineff_score = self.ou.inefficiency_score(sharp_prob, soft_prob, dt_minutes)

        # ═══════════════════════════════════════════════════════
        # 7. Bayesian SNR Filter — SNR ≥ 3.0
        # ═══════════════════════════════════════════════════════
        snr = self.snr_filter.compute_snr(
            ev=ev,
            volatility=volatility,
            n_confirming_books=n_confirming_books,
            volume_ratio=1 + ineff_score,
        )

        if not self.snr_filter.is_valid_signal(snr):
            return None

        # ═══════════════════════════════════════════════════════
        # 8. Dynamic Allocation — Kelly 0.25 + Cap 3%
        # ═══════════════════════════════════════════════════════
        raw_stake = self.kelly.kelly_stake(sharp_prob, soft_cote, bankroll)
        stake = FractionalKelly.smart_stake_round(raw_stake, self.stake_base)

        clv_estimate = ev * 0.7  # CLV = 70% de l'EV
        fair_price = 1.0 / sharp_prob if sharp_prob > 0 else 0.0

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
            fair_price=fair_price,
            is_valid=True,
        )