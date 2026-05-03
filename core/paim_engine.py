from core.math_engine import shin_method
from core.notifications import send_elite_ticket
import numpy as np

def calculate_ev(odds_xbet, true_prob_pinnacle):
    """
    EV = (Probability_true * Odds_xbet) - 1
    """
    return (true_prob_pinnacle * odds_xbet) - 1

def process_market(pinnacle_odds, xbet_odds):
    """
    1. Shin's method on Pinnacle odds -> true probabilities.
    2. Compare with 1XBet odds.
    3. Calculate EV.
    """
    # Assuming Pinnacle odds are [O1, O2] for binary outcome
    true_probs = shin_method(pinnacle_odds)
    
    # EV for each outcome
    evs = [(true_probs[i] * xbet_odds[i]) - 1 for i in range(len(xbet_odds))]
    
    return evs, true_probs

def select_top_signals(signals, limit=9):
    # Sort signals by EV descending
    sorted_signals = sorted(signals, key=lambda x: x['ev'], reverse=True)
    return sorted_signals[:limit]

def process_elite_ticket(ticket_data):
    """
    Integrates 7/9 ticket processing with elite notification.
    """
    # Assuming the logic is to notify for all elite tickets
    return send_elite_ticket(ticket_data)
