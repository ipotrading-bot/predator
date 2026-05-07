"""
core/math_engine.py — Shin Method optimisé NumPy (PhD MIT Standard)
Calcule les probabilités réelles sans marge via l'algorithme de bisection.
Sans scipy — compatible Vercel.
"""
import numpy as np


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