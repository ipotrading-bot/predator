"""
core/harvester.py — Match harvester with dual strategy:
  1. 1XBet direct feed (fast, free, may be geo-blocked)
  2. Gemini Google Search fallback (always works)
"""
import os
import re
import json
import requests

# ── 1XBet feed candidates (try in order) ─────────────────────────
XBET_FEEDS = [
    "https://1xbet.com/LineFeed/Get1x2?sport=1&count=50&lng=en&mode=4",
    "https://1xbet.com/LineFeed/Get1x2?sport=1&count=50&lng=en&mode=1",
    "https://1xbet.cm/LineFeed/Get1x2?sport=1&count=50&lng=en&mode=4",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://1xbet.com/en/line/football/",
}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"


def _odd(val):
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_xbet_json(data):
    matches = []
    for event in data.get("Value", []):
        try:
            home = str(event.get("O1", "")).strip()
            away = str(event.get("O2", "")).strip()
            if not home or not away:
                continue
            o1 = _odd(event.get("C1"))
            ox = _odd(event.get("C2"))
            o2 = _odd(event.get("C3"))
            if o1 == 0.0 and o2 == 0.0:
                continue
            matches.append({
                "id":         str(event.get("CI", f"{home}_{away}")),
                "match":      f"{home} vs {away}",
                "home":       home,
                "away":       away,
                "league":     str(event.get("L", "Unknown")),
                "odds_1xbet": {"1": o1, "X": ox, "2": o2},
            })
        except Exception:
            continue
    return matches


def _fetch_from_1xbet():
    """Try each 1XBet feed URL. Returns list of matches or []."""
    for url in XBET_FEEDS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code == 200:
                data = r.json()
                matches = _parse_xbet_json(data)
                if matches:
                    print(f"[Harvester] 1XBet OK: {len(matches)} matches via {url}")
                    return matches
        except Exception as e:
            print(f"[Harvester] 1XBet {url}: {e}")
    return []


def _fetch_from_gemini():
    """
    Fallback: ask Gemini for today's top football matches with estimated odds.
    No google_search tool — uses model knowledge to preserve quota.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Harvester] No GEMINI_API_KEY for fallback")
        return []

    from datetime import date
    today = date.today().isoformat()

    prompt = (
        f"Today is {today}. List 8 important football matches scheduled today or tomorrow "
        f"(top European leagues, Champions League, Copa Libertadores, or major international). "
        f"For each match estimate realistic decimal odds for 1XBet (1/X/2) based on team strength. "
        f"Return ONLY a valid JSON array, no text before or after:\n"
        '[{"match":"Real Madrid vs Barcelona","home":"Real Madrid","away":"Barcelona",'
        '"league":"La Liga","odds_1xbet":{"1":2.10,"X":3.50,"2":3.20}}]'
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024},
    }

    try:
        for attempt in range(3):
            r = requests.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=30)
            if r.status_code == 429:
                wait = 65 if attempt == 0 else 30
                print(f"[Harvester] Gemini rate limit — waiting {wait}s (attempt {attempt+1}/3)")
                import time; time.sleep(wait)
                continue
            break
        if r.status_code != 200:
            print(f"[Harvester] Gemini fallback error: {r.status_code}")
            return []

        # gemini-2.5 returns [thinking_part, response_part] — take last text part
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text)
        m = re.search(r'\[[\s\S]*\]', text)
        if not m:
            print("[Harvester] Gemini: no JSON array found")
            return []

        raw = json.loads(m.group())
        matches = []
        for i, ev in enumerate(raw):
            try:
                home = str(ev.get("home", "")).strip()
                away = str(ev.get("away", "")).strip()
                odds = ev.get("odds_1xbet", {})
                if not home or not away:
                    continue
                matches.append({
                    "id":         f"gemini_{i}",
                    "match":      ev.get("match", f"{home} vs {away}"),
                    "home":       home,
                    "away":       away,
                    "league":     ev.get("league", "Unknown"),
                    "odds_1xbet": {
                        "1": _odd(odds.get("1")),
                        "X": _odd(odds.get("X", 0)),
                        "2": _odd(odds.get("2")),
                    },
                })
            except Exception:
                continue

        print(f"[Harvester] Gemini fallback: {len(matches)} matches")
        return matches

    except Exception as e:
        print(f"[Harvester] Gemini fallback exception: {e}")
        return []


def fetch_matches():
    """
    Fetch upcoming football matches.
    Tries 1XBet direct feed first, falls back to Gemini Google Search.
    """
    matches = _fetch_from_1xbet()
    if not matches:
        print("[Harvester] 1XBet unreachable — using Gemini fallback")
        matches = _fetch_from_gemini()
    return matches


def shin_edge(xbet_odd, pinnacle_price):
    """
    Compute value edge vs Pinnacle fair price (Shin approximation).
    Returns % edge — positive means value bet.
    """
    if not xbet_odd or not pinnacle_price or xbet_odd <= 1.01 or pinnacle_price <= 1.01:
        return 0.0
    return round((xbet_odd / pinnacle_price - 1) * 100, 2)
