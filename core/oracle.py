"""
core/oracle.py — PAIM v7.2 — Gemini + Google Search → Pinnacle fair price
Soccer:              asks for 1X2, computes AH 0.0 internally → returns DNB price
Tennis / Basketball: asks for Moneyline → returns favorite price
"""
import os
import re
import requests

from core.math_engine import calc_dnb

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"


def get_pinnacle_price(match_name: str, sport: str = "soccer", api_key: str = None) -> float | None:
    """
    Returns Pinnacle's fair price for the given match, in the correct market:
    - Soccer  → AH 0.0 (DNB) price of the favorite
    - Others  → Moneyline price of the favorite
    Returns None if not found.
    """
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Oracle] No GEMINI_API_KEY")
        return None

    if sport == "soccer":
        prompt = (
            f"Use Google Search to find Pinnacle Sports current 1X2 odds for this football match: {match_name}\n"
            f"Return ONLY valid JSON with three fields: "
            f'{{\"home\": 2.10, \"draw\": 3.40, \"away\": 3.20}}\n'
            f"If odds are not available: {{\"home\": null}}"
        )
    else:
        sport_ctx = {"tennis": "tennis", "basketball": "NBA basketball"}.get(sport, sport)
        prompt = (
            f"Use Google Search to find Pinnacle Sports current Moneyline odds for this {sport_ctx} match: {match_name}\n"
            f"Return ONLY valid JSON with the favorite's decimal odd: "
            f'{{\"price\": 1.85, \"team\": \"FavoriteTeam\"}}\n'
            f"If not found: {{\"price\": null}}"
        )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 128},
    }

    try:
        import time
        for attempt in range(2):
            r = requests.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=20)
            if r.status_code == 429:
                print(f"[Oracle] Rate limit for {match_name} — waiting 65s")
                time.sleep(65)
                continue
            break
        if r.status_code != 200:
            print(f"[Oracle] Gemini error {r.status_code} for {match_name}")
            return None

        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text)

        if sport == "soccer":
            return _parse_soccer_price(text)
        return _parse_moneyline_price(text)

    except Exception as e:
        print(f"[Oracle] Error for {match_name}: {e}")
        return None


def _parse_soccer_price(text: str) -> float | None:
    """Extract Pinnacle 1X2, compute AH 0.0 DNB for the favorite."""
    m_h = re.search(r'"home"\s*:\s*(\d+\.\d+)', text)
    m_d = re.search(r'"draw"\s*:\s*(\d+\.\d+)', text)
    m_a = re.search(r'"away"\s*:\s*(\d+\.\d+)', text)
    if m_h and m_d and m_a:
        home = float(m_h.group(1))
        draw = float(m_d.group(1))
        away = float(m_a.group(1))
        dnb_h = calc_dnb(home, draw)
        dnb_a = calc_dnb(away, draw)
        candidates = [v for v in [dnb_h, dnb_a] if v > 1.01]
        return min(candidates) if candidates else None
    return None


def _parse_moneyline_price(text: str) -> float | None:
    """Extract Pinnacle Moneyline price for the favorite."""
    m = re.search(r'"price"\s*:\s*(\d+\.\d+)', text)
    if m:
        return float(m.group(1))
    nums = re.findall(r'\b(\d+\.\d{2})\b', text)
    valid = [float(n) for n in nums if 1.05 < float(n) < 20.0]
    return valid[0] if valid else None
