import math

def calculate_shin_probabilities(odds):
    """
    Calcule les probabilités réelles sans marge (Shin Method) via l'algorithme de la bisection.
    Évite l'utilisation de scipy pour optimiser la taille du bundle Vercel.
    """
    implied_probs = [1.0 / float(o) for o in odds]
    sum_implied = sum(implied_probs)
    
    if abs(sum_implied - 1.0) < 1e-5:
        return implied_probs

    # Algorithme de la bisection pour trouver le paramètre z de Shin
    low, high = 0.0, 1.0 - 1e-5
    z = 0.0
    
    for _ in range(100):
        mid = (low + high) / 2.0
        current_sum = 0.0
        for p_implied in implied_probs:
            # Équation de Shin pour la probabilité réelle p_i
            numerator = math.sqrt(mid**2 + 4 * (1.0 - mid) * (p_implied**2 / sum_implied)) - mid
            denominator = 2 * (1.0 - mid)
            current_sum += numerator / denominator if denominator != 0 else p_implied
            
        if abs(current_sum - 1.0) < 1e-6:
            z = mid
            break
        elif current_sum > 1.0:
            low = mid
        else:
            high = mid
            
    fair_probs = []
    for p_implied in implied_probs:
        numerator = math.sqrt(z**2 + 4 * (1.0 - z) * (p_implied**2 / sum_implied)) - z
        denominator = 2 * (1.0 - z)
        p_fair = numerator / denominator if denominator != 0 else p_implied
        fair_probs.append(p_fair)
        
    return fair_probs
