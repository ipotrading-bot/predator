"""
core/oracle.py — PAIM v7.6 — Gemini + Google Search → Sharp fair price
Multi-source: Pinnacle → Betfair Exchange → Circa Sports
Non-Pinnacle sources receive a 0.5% reference price penalty (conservative edge).
Returns (price: float | None, team_name: str | None)
"""
import os
import re
import time
import requests
from datetime import date as _date

from core.math_engine import calc_dnb

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"

# Fallback chain: (book_name, edge_penalty_pct)
# Penalty inflates the reference price → reduces effective edge → more conservative
_SHARP_BOOKS = [
    ("Pinnacle Sports",  0.0),   # Primary sharp — no penalty
    ("Betfair Exchange", 0.5),   # Sharp exchange — 0.5% conservative penalty
    ("Circa Sports",     0.5),   # Sharp US book — 0.5% penalty
]


def get_pinnacle_price(
    match_name: str,
    sport: str = "soccer",
    api_key: str = None,
    league: str = "",
    match_date: str = "",
) -> tuple[float | None, str | None]:
    """
    Returns (sharp_reference_price, favorite_team_name).
    Tries Pinnacle → Betfair Exchange → Circa Sports in order.
    Non-Pinnacle prices are inflated by penalty% (reduces effective edge shown to engine).
    Both may be None on complete failure.
    """
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Oracle] No GEMINI_API_KEY")
        return None, None

    if not match_date:
        match_date = _date.today().isoformat()

    context = match_name
    if league:
        context += f" ({league})"
    context += f" — {match_date}"

    for book_name, penalty in _SHARP_BOOKS:
        price, team = _query_book(context, sport, api_key, book_name)
        if price and price > 1.01:
            if penalty > 0:
                price = round(price * (1 + penalty / 100), 4)
                print(f"[Oracle] {book_name} fallback for {match_name} (+{penalty}% penalty)")
            return price, team

    return None, None


def _query_book(
    context: str,
    sport: str,
    api_key: str,
    book_name: str,
) -> tuple[float | None, str | None]:
    """Single Gemini call for one sportsbook. Returns (dnb_price, team) or (None, None)."""
    if sport == "soccer":
        prompt = (
            f"Use Google Search to find {book_name} current 1X2 odds for this soccer match:\n"
            f"{context}\n"
            f"Include both team names. Return ONLY valid JSON:\n"
            f'{{"home_team":"PSG","home":1.60,"draw":3.80,"away_team":"Lyon","away":9.00}}\n'
            f'If not found on {book_name}: {{"home":null}}'
        )
    else:
        sport_ctx = {"tennis": "tennis", "basketball": "NBA basketball"}.get(sport, sport)
        prompt = (
            f"Use Google Search to find {book_name} current Moneyline odds for this {sport_ctx}:\n"
            f"{context}\n"
            f"Return ONLY valid JSON with favorite decimal odd and team name:\n"
            f'{{"price":1.85,"team":"FavoriteTeam"}}\n'
            f'If not found on {book_name}: {{"price":null}}'
        )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 200},
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=25)
        except Exception as e:
            print(f"[Oracle] Request error ({book_name}): {e}")
            return None, None
        if r.status_code == 429:
            wait = 65 if attempt == 0 else 30
            print(f"[Oracle] Rate limit ({book_name}) — waiting {wait}s")
            time.sleep(wait)
            r = None
            continue
        break

    if r is None or r.status_code != 200:
        if r is not None:
            print(f"[Oracle] HTTP {r.status_code} from Gemini ({book_name})")
        return None, None

    try:
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text)
    except Exception as e:
        print(f"[Oracle] Parse error ({book_name}): {e}")
        return None, None

    if sport == "soccer":
        return _parse_soccer(text)
    return _parse_moneyline(text)


def _parse_soccer(text: str) -> tuple[float | None, str | None]:
    """Extract 1X2 odds + team names → compute AH 0.0 for the favorite."""
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

        if dnb_h >= dnb_a and dnb_h > 1.01:
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
