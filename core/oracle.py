"""
core/oracle.py — PAIM v7.5 — Gemini + Google Search → Pinnacle fair price + team name
Soccer:              asks for 1X2 with team names, computes AH 0.0 internally
Tennis / Basketball: asks for Moneyline with team name
Returns (price: float | None, team_name: str | None)
"""
import os
import re
import requests

from core.math_engine import calc_dnb

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"


def get_pinnacle_price(
    match_name: str,
    sport: str = "soccer",
    api_key: str = None,
) -> tuple[float | None, str | None]:
    """
    Returns (pinnacle_fair_price, favorite_team_name).
    Both may be None on failure.

    Soccer  → AH 0.0 (DNB) price of the Pinnacle favorite.
    Others  → Moneyline price of the Pinnacle favorite.
    """
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Oracle] No GEMINI_API_KEY")
        return None, None

    if sport == "soccer":
        prompt = (
            f"Use Google Search to find Pinnacle Sports current 1X2 odds for: {match_name}\n"
            f"Return ONLY valid JSON — include both team names:\n"
            f'{{"home_team":"PSG","home":1.60,"draw":3.80,"away_team":"Lyon","away":9.00}}\n'
            f'If not found: {{"home":null}}'
        )
    else:
        sport_ctx = {"tennis": "tennis", "basketball": "NBA basketball"}.get(sport, sport)
        prompt = (
            f"Use Google Search to find Pinnacle Sports current Moneyline odds for this {sport_ctx} match: {match_name}\n"
            f"Return ONLY valid JSON with the favorite's decimal odd and team name:\n"
            f'{{"price":1.85,"team":"FavoriteTeam"}}\n'
            f'If not found: {{"price":null}}'
        )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 150},
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
            return None, None

        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text)

        if sport == "soccer":
            return _parse_soccer(text)
        return _parse_moneyline(text)

    except Exception as e:
        print(f"[Oracle] Error for {match_name}: {e}")
        return None, None


def _parse_soccer(text: str) -> tuple[float | None, str | None]:
    """Extract Pinnacle 1X2 with team names → compute AH 0.0 for the favorite."""
    m_ht = re.search(r'"home_team"\s*:\s*"([^"]+)"', text)
    m_at = re.search(r'"away_team"\s*:\s*"([^"]+)"', text)
    m_h  = re.search(r'"home"\s*:\s*(\d+\.\d+)', text)
    m_d  = re.search(r'"draw"\s*:\s*(\d+\.\d+)', text)
    m_a  = re.search(r'"away"\s*:\s*(\d+\.\d+)', text)

    if m_h and m_d and m_a:
        home_odd = float(m_h.group(1))
        draw_odd = float(m_d.group(1))
        away_odd = float(m_a.group(1))
        dnb_h = calc_dnb(home_odd, draw_odd)
        dnb_a = calc_dnb(away_odd, draw_odd)

        home_name = m_ht.group(1) if m_ht else ""
        away_name = m_at.group(1) if m_at else ""

        if dnb_h > 1.01 and (dnb_a <= 1.01 or dnb_h <= dnb_a):
            return (dnb_h, home_name) if home_name else (None, None)
        elif dnb_a > 1.01:
            return (dnb_a, away_name) if away_name else (None, None)

    return None, None


def _parse_moneyline(text: str) -> tuple[float | None, str | None]:
    """Extract Pinnacle Moneyline price and team name."""
    m_p = re.search(r'"price"\s*:\s*(\d+\.\d+)', text)
    m_t = re.search(r'"team"\s*:\s*"([^"]+)"', text)
    price = float(m_p.group(1)) if m_p else None
    team  = m_t.group(1) if m_t else None
    if price and 1.05 < price < 20.0:
        return price, team
    nums = re.findall(r'\b(\d+\.\d{2})\b', text)
    valid = [float(n) for n in nums if 1.05 < float(n) < 20.0]
    return (valid[0], None) if valid else (None, None)
