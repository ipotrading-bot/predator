# PREDATOR PAIM v3.0 Evolution Plan

This document outlines the proposed architectural changes for PREDATOR PAIM v3.0, focusing on advanced quantification and adaptive intelligence.

## 1. Core Engine Upgrades (`core/paim_engine.py`, `core/math_engine.py`)

### Binary Synthesis
- **Goal**: Convert complex markets (e.g., Asian Handicap, Totals) into synthetic binary outcomes (Win/Loss) to expand the scope beyond simple Moneyline.
- **Implementation**: Add `BinarySynthesisEngine` to `math_engine.py` to calculate fair probabilities for combined outcomes.

### Adaptive Thresholds (`Seuil adaptatif`)
- **Goal**: Dynamically adjust `min_ev_threshold` and `min_snr_ratio` based on market volatility and sport-specific liquidity.
- **Implementation**: Update `PAIMEngine` to accept a context (e.g., sport, time to event) and return dynamic thresholds.

### Confidence Score
- **Goal**: Move beyond binary signal validation (Approved/Rejected) to a 0.0-1.0 confidence score.
- **Implementation**: Update `PAIMSignal` dataclass to include `confidence_score`.

## 2. Intelligence & Grounding (`api/index.py`)

### Gemini Grounding
- **Goal**: Ensure AI-generated notes in the dashboard are grounded in real-time data to reduce hallucinations.
- **Implementation**: Integrate search-based grounding in the Gemini API call within `api/index.py`.

## 3. Configuration & Data (`config.py`, Database)

### Config Updates (`config.py`)
- Remove hardcoded `min_ev_threshold` and `min_snr_ratio`.
- Introduce `adaptive_threshold_factors` (dictionary mapping sport/market to base sensitivity).

### Database Schema Updates
- Add columns to `signals` table in Supabase:
    - `confidence_score` (float)
    - `synthesis_type` (text)
    - `is_adaptive` (boolean)
