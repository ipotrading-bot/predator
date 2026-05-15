"""
core/harvester.py — PAIM v7.5 — Guerrilla Mode
Soft source : 1XBet direct feed (JSON) → Gemini fallback
Sharp source: Gemini 2.0 Flash + Google Search → Pinnacle prices
Sports: 1=Soccer, 3=Tennis, 4=Basketball
All timestamps : UTC/GMT.
"""
import logging
import os
import re
import json
import time
import random
import requests

from core.paim_engine import SPORT_LABELS

# ── UTC sub-logger (inherits handler from PREDATOR root) ─────────────
log = logging.getLogger("PREDATOR.harvester")

SPORT_IDS = {1: "soccer", 3: "tennis", 4: "basketball"}

XBET_FEED_TPLS = [
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&partner=157",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&country=255",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=1&partner=157",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=1",
    "https://1xbet.cm/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://1xbet.com/en/line/",
}
GEMINI_URL       = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
GEMINI_FLASH_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

_SPORT_PROMPTS = {
    "soccer":     "top European football/soccer",
    "tennis":     "ATP or WTA tennis (Roland Garros, Italian Open, or similar)",
    "basketball": "NBA playoff or top basketball",
}


def _odd(val):
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_xbet_json(data, sport_id):
    sport_name = SPORT_IDS.get(sport_id, "unknown")
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
                "sport":      sport_name,
                "sport_id":   sport_id,
                "odds_1xbet": {"1": o1, "X": ox, "2": o2},
            })
        except Exception:
            continue
    return matches


def _fetch_from_1xbet(sport_id):
    """Try each URL variant with a small random delay. Returns list of matches or []."""
    sport_name = SPORT_IDS.get(sport_id, str(sport_id))
    for tpl in XBET_FEED_TPLS:
        url = tpl.format(sport_id=sport_id)
        try:
            time.sleep(random.uniform(2, 5))
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                matches = _parse_xbet_json(data, sport_id)
                if matches:
                    log.info("1XBet %s OK: %d matches via %s", sport_name, len(matches), url.split("?")[0])
                    return matches
        except Exception as e:
            log.warning("1XBet %s fail (%s): %s", sport_name, url.split("?")[0], e)
    return []


def _fetch_from_gemini(sport_id):
    """Gemini fallback for a given sport. Returns list of validated matches or []."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return []

    sport_name = SPORT_IDS.get(sport_id, "sport")
    sport_desc = _SPORT_PROMPTS.get(sport_name, sport_name)
    is_soccer  = sport_id == 1

    from datetime import date
    today = date.today().isoformat()

    draw_field = ',"X":3.40' if is_soccer else ',"X":0'
    prompt = (
        f"Today is {today}. List 6 important {sport_desc} matches scheduled today or tomorrow. "
        f"For each match estimate realistic decimal odds for 1XBet. "
        f"{'IMPORTANT: always include the draw odd X for football.' if is_soccer else ''}"
        f"Return ONLY a valid JSON array:\n"
        f'[{{"match":"Team A vs Team B","home":"Team A","away":"Team B",'
        f'"league":"League Name","sport":"{sport_name}",'
        f'"odds_1xbet":{{"1":2.10{draw_field},"2":3.20}}}}]'
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
                log.warning("Gemini rate limit (%s) — waiting %ds", sport_name, wait)
                time.sleep(wait)
                continue
            break
        if r.status_code != 200:
            return []

        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text)
        m = re.search(r'\[[\s\S]*\]', text)
        if not m:
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
                ox = _odd(odds.get("X", 0))
                # Hard require draw odd for soccer — reject silently if missing
                if is_soccer and ox <= 1.01:
                    continue
                matches.append({
                    "id":         f"gemini_{sport_id}_{i}",
                    "match":      ev.get("match", f"{home} vs {away}"),
                    "home":       home,
                    "away":       away,
                    "league":     ev.get("league", "Unknown"),
                    "sport":      sport_name,
                    "sport_id":   sport_id,
                    "odds_1xbet": {
                        "1": _odd(odds.get("1")),
                        "X": ox,
                        "2": _odd(odds.get("2")),
                    },
                })
            except Exception:
                continue

        log.info("Gemini %s: %d valid matches", sport_name, len(matches))
        return matches

    except Exception as e:
        log.error("Gemini %s exception: %s", sport_name, e)
        return []


def fetch_matches():
    """Fetch matches for all configured sports. Returns combined list."""
    all_matches = []
    for sport_id in SPORT_IDS:
        matches = _fetch_from_1xbet(sport_id)
        if not matches:
            matches = _fetch_from_gemini(sport_id)
        all_matches.extend(matches)
    return all_matches


def fetch_pinnacle_prices(matches: list) -> dict:
    """
    Sharp source — Gemini 2.0 Flash + Google Search → Pinnacle decimal odds.
    Returns {match_name: {"1": float, "X": float, "2": float}}.
    Soccer entries include draw (X); tennis/basketball have X=0.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not matches:
        return {}

    names = [m["match"] for m in matches[:25]]
    match_list = "\n".join(f"- {n}" for n in names)

    prompt = (
        f"Use Google Search to find current Pinnacle sportsbook decimal odds for these matches:\n"
        f"{match_list}\n\n"
        f"For each match return the latest Pinnacle no-vig odds: "
        f"1=home win, X=draw (soccer only, else 0), 2=away win.\n"
        f"Return ONLY a valid JSON array, no extra text:\n"
        f'[{{"match":"Team A vs Team B","1":2.05,"X":3.35,"2":3.60}}]'
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
    }

    try:
        r = requests.post(f"{GEMINI_FLASH_URL}?key={api_key}", json=payload, timeout=60)
        if r.status_code != 200:
            log.error("Pinnacle/Gemini HTTP %d: %s", r.status_code, r.text[:200])
            return {}

        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m = re.search(r'\[[\s\S]*\]', text)
        if not m:
            log.warning("Pinnacle/Gemini: no JSON array in response")
            return {}

        raw = json.loads(m.group())
        result = {}
        for item in raw:
            name = item.get("match", "").strip()
            if not name:
                continue
            result[name] = {
                "1": _odd(item.get("1")),
                "X": _odd(item.get("X", 0)),
                "2": _odd(item.get("2")),
            }
        log.info("Pinnacle/Gemini: %d/%d prices received", len(result), len(names))
        return result

    except Exception as e:
        log.error("Pinnacle/Gemini exception: %s", e)
        return {}


def shin_edge(xbet_odd, pinnacle_price):
    """Legacy — kept for backward compat. Use core.paim_engine.compute_alpha."""
    if not xbet_odd or not pinnacle_price or xbet_odd <= 1.01 or pinnacle_price <= 1.01:
        return 0.0
    return round((xbet_odd / pinnacle_price - 1) * 100, 2)
