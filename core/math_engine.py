"""
core/math_engine.py — Moteur mathématique PAIM (sans scipy)
Implémentation pure Python/Numpy de la méthode de Shin
"""
import numpy as np


def _bisection_root(func, a, b, tol=1e-8, max_iter=1000):
    """
    Méthode de la bisection pour trouver la racine d'une fonction.
    Remplace scipy.optimize.brentq pour éviter la dépendance scipy.
    
    Args:
        func: Fonction continue dont on cherche la racine
        a, b: Intervalle de recherche (doit contenir la racine)
        tol: Tolérance de convergence
        max_iter: Nombre maximum d'itérations
    
    Returns:
        float: Valeur approchée de la racine
    
    Raises:
        ValueError: Si func(a) et func(b) ont le même signe
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


def shin_method(odds, tolerance=1e-8):
    """
    Calculate true probabilities using Shin's method.
    odds: list or numpy array of decimal odds.
    
    Uses pure Python bisection instead of scipy.optimize.brentq.
    """
    odds = np.array(odds, dtype=np.float64)
    
    # Raw probabilities (reciprocals)
    pi = 1.0 / odds
    
    # Function to find z such that sum of probabilities = 1
    def func(z):
        # Avoid division by zero or negative values
        if z <= 0:
            z = 1e-10
        discriminant = z**2 + 4.0 * pi * z
        # Ensure non-negative discriminant
        discriminant = np.maximum(discriminant, 0)
        return np.sum(np.sqrt(discriminant) - z) / 2.0 - 1.0
    
    # Solve for z in range (0, 1) using bisection
    try:
        z = _bisection_root(func, 1e-10, 1.0 - 1e-10, tol=tolerance)
    except ValueError:
        # Fallback: use simple additive de-marginalization
        total_implied = np.sum(pi)
        return pi / total_implied
    
    # Calculate true probabilities
    discriminant = z**2 + 4.0 * pi * z
    discriminant = np.maximum(discriminant, 0)
    true_probs = (np.sqrt(discriminant) - z) / 2.0
    
    return true_probs