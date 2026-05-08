"""
core/math_engine.py — Shin Method + Triple-Sharp Consensus (PAIM v5.0)
Calcule les probabilités réelles sans marge via l'algorithme de bisection.
Triple-Sharp Consensus: Pinnacle (50%) + Betfair (30%) + Marché secondaire (20%)
Sans scipy — compatible Vercel.
"""
import numpy as np
from typing import Optional, List, Dict


def calculate_shin_probabilities(odds):
    """
    Algorithme de Shin optimisé sans Scipy (PhD MIT Standard).
    
    Args:
        odds: Liste de cotes décimales
    
    Returns:
        Liste des probabilités réelles (Shin Method)
    """
    odds = np.array(odds, dtype=float)
    implied_probs = 1.0 / odds
    sum_p = np.sum(implied_probs)

    # Si déjà sans marge, retour direct
    if abs(sum_p - 1.0) < 1e-5:
        return implied_probs.tolist()

    # Recherche du paramètre z (marge d'insider) par bisection
    low, high = 0.0, sum_p - 1.0
    for _ in range(50):
        z = (low + high) / 2.0
        f = np.sum((np.sqrt(z**2 + 4 * (1 - z) * (implied_probs**2 / sum_p)) - z) / (2 * (1 - z))) - 1.0
        if f > 0:
            low = z
        else:
            high = z

    z = (low + high) / 2.0
    fair_probs = (np.sqrt(z**2 + 4 * (1 - z) * (implied_probs**2 / sum_p)) - z) / (2 * (1 - z))
    return fair_probs.tolist()


# ═══════════════════════════════════════════════════════════════════
# TRIPLE-SHARP CONSENSUS (PAIM v5.0 Perfection Institutionnelle)
# ═══════════════════════════════════════════════════════════════════

def calculate_triple_sharp_consensus(
    pinnacle_odds: Optional[List[float]],
    betfair_odds: Optional[List[float]] = None,
    secondary_odds: Optional[List[float]] = None,
    consensus_threshold: float = 0.05
) -> Dict:
    """
    Calcule un consensus pondéré entre les 3 sources sharp du marché.
    
    Poids institutionnels (PhD MIT):
    - Pinnacle: 50% (référence volume mondial)
    - Betfair Exchange: 30% (sagesse des foules, liquidité réelle)
    - Marché secondaire (Circa/BetCRIS): 20% (validation US)
    
    Args:
        pinnacle_odds: Cotes Pinnacle [home, away]
        betfair_odds: Cotes Betfair Exchange [back_home, back_away]
        secondary_odds: Cotes source secondaire [home, away]
        consensus_threshold: Écart max accepté entre sources (5%)
        
    Returns:
        Dict avec probas consensus, confiance et métriques
    """
    weights = {"pinnacle": 0.50, "betfair": 0.30, "secondary": 0.20}
    
    # Calcul des probas pour chaque source disponible
    sources = {}
    
    if pinnacle_odds and all(o > 1.0 for o in pinnacle_odds):
        sources["pinnacle"] = calculate_shin_probabilities(pinnacle_odds)
    
    if betfair_odds and all(o > 1.0 for o in betfair_odds):
        sources["betfair"] = calculate_shin_probabilities(betfair_odds)
        
    if secondary_odds and all(o > 1.0 for o in secondary_odds):
        sources["secondary"] = calculate_shin_probabilities(secondary_odds)
    
    # Si pas de Pinnacle, pas de consensus (rejet)
    if "pinnacle" not in sources:
        return {
            "consensus_prob": None,
            "confidence": 0.0,
            "sources_used": [],
            "divergence_alert": True,
            "error": "Pinnacle requis pour consensus"
        }
    
    # Normaliser les poids selon sources disponibles
    available_weights = {k: weights[k] for k in sources.keys()}
    total_weight = sum(available_weights.values())
    normalized_weights = {k: v / total_weight for k, v in available_weights.items()}
    
    # Calcul du consensus pondéré
    n_outcomes = len(sources["pinnacle"])
    consensus_probs = [0.0] * n_outcomes
    
    for source_name, probs in sources.items():
        weight = normalized_weights.get(source_name, 0)
        for i in range(n_outcomes):
            consensus_probs[i] += probs[i] * weight
    
    # Calcul de la divergence (écart-type entre sources)
    divergence = 0.0
    if len(sources) > 1:
        for i in range(n_outcomes):
            probs_i = [sources[s][i] for s in sources.keys()]
            mean_p = sum(probs_i) / len(probs_i)
            variance = sum((p - mean_p) ** 2 for p in probs_i) / len(probs_i)
            divergence += variance ** 0.5
        divergence /= n_outcomes
    
    # Score de confiance basé sur divergence
    confidence = 1.0 - min(divergence / consensus_threshold, 1.0)
    
    # Alert si divergence trop forte
    divergence_alert = divergence > consensus_threshold
    
    return {
        "consensus_prob": consensus_probs,
        "confidence": round(confidence, 3),
        "sources_used": list(sources.keys()),
        "divergence": round(divergence, 4),
        "divergence_alert": divergence_alert,
        "weights_applied": normalized_weights
    }


def validate_sharp_consensus(
    pinnacle_prob: float,
    betfair_prob: Optional[float] = None,
    max_spread: float = 0.03
) -> bool:
    """
    Valide que les probas sharp sont en accord (pas d'arbitrage interne).
    
    Args:
        pinnacle_prob: Probabilité Pinnacle
        betfair_prob: Probabilité Betfair (optionnel)
        max_spread: Écart max accepté (3%)
        
    Returns:
        True si consensus valide, False si divergence critique
    """
    if betfair_prob is None:
        return True  # Pas de comparaison possible
    
    spread = abs(pinnacle_prob - betfair_prob)
    return spread <= max_spread