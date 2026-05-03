import numpy as np
from scipy.optimize import brentq

def shin_method(odds, tolerance=1e-8):
    """
    Calculate true probabilities using Shin's method.
    odds: list or numpy array of decimal odds.
    """
    odds = np.array(odds)
    # Raw probabilities (reciprocals)
    pi = 1 / odds
    
    # Function to find z such that sum of probabilities = 1
    def func(z):
        return np.sum(np.sqrt(z**2 + 4 * pi * z) - z) / 2 - 1

    # Solve for z in range (0, 1)
    z = brentq(func, 0, 1)
    
    # Calculate true probabilities
    true_probs = (np.sqrt(z**2 + 4 * pi * z) - z) / 2
    
    return true_probs
