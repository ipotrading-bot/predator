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

from core.paim_engine import SPORT_LABELS, strict_team_match

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


def _fuzzy_match_name(ret_name: str, orig_names: list) -> str | None:
    """Map a Gemini-returned match name back to the original 1XBet name using team fuzzy matching."""
    if " vs " not in ret_name:
        return None
    ret_home, ret_away = [x.strip() for x in ret_name.split(" vs ", 1)]
    for orig in orig_names:
        if " vs " not in orig:
            continue
        orig_home, orig_away = [x.strip() for x in orig.split(" vs ", 1)]
        if strict_team_match(ret_home, orig_home) and strict_team_match(ret_away, orig_away):
            return orig
    return None


def fetch_pinnacle_prices(matches: list) -> dict:
    """
    Sharp source — Gemini 2.0 Flash + Google Search → Pinnacle decimal odds.
    Falls back to Betfair Exchange per match when Pinnacle line is unavailable.
    Uses fuzzy team matching to handle 1XBet abbreviation vs Pinnacle full name divergence.
    Returns {match_name: {"1": float, "X": float, "2": float}}.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not matches:
        return {}

    from datetime import date
    today = date.today().isoformat()

    names = [m["match"] for m in matches[:25]]
    match_list = "\n".join(
        f"- {m['match']} [{m.get('league', '?')}, {m.get('sport', 'soccer')}]"
        for m in matches[:25]
    )

    prompt = (
        f"Today is {today}. Use Google Search to find current decimal odds for each match below.\n"
        f"Search PINNACLE SPORTS first. If Pinnacle has no line for a match, search BETFAIR EXCHANGE.\n"
        f"Add a 'source' field: 'Pinnacle' or 'Betfair'.\n\n"
        f"Matches (name [league, sport]):\n{match_list}\n\n"
        f"Return ONLY a valid JSON array. Use the exact match name from the list:\n"
        f'[{{"match":"Team A vs Team B","1":2.05,"X":3.35,"2":3.60,"source":"Pinnacle"}}]\n'
        f"X=0 for non-soccer. Omit matches with no odds found on either book."
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.post(f"{GEMINI_FLASH_URL}?key={api_key}", json=payload, timeout=60)
        except Exception as e:
            log.error("Pinnacle/Gemini request error: %s", e)
            return {}
        if r.status_code == 429:
            wait = 65 if attempt == 0 else 30
            log.warning("Pinnacle/Gemini rate limit — waiting %ds (attempt %d)", wait, attempt + 1)
            time.sleep(wait)
            r = None
            continue
        break

    if r is None or r.status_code != 200:
        if r is not None:
            log.error("Pinnacle/Gemini HTTP %d: %s", r.status_code, r.text[:200])
        return {}

    try:
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m_arr = re.search(r'\[[\s\S]*\]', text)
        if not m_arr:
            log.warning("Pinnacle/Gemini: no JSON array in response")
            return {}

        raw = json.loads(m_arr.group())
    except Exception as e:
        log.error("Pinnacle/Gemini parse exception: %s", e)
        return {}

    names_set = set(names)
    result = {}
    for item in raw:
        ret_name = item.get("match", "").strip()
        source   = item.get("source", "Pinnacle")
        if not ret_name:
            continue

        # Exact match first, then fuzzy fallback
        matched = ret_name if ret_name in names_set else _fuzzy_match_name(ret_name, names)
        if not matched:
            log.debug("Pinnacle/Gemini: no local match for '%s'", ret_name)
            continue

        odds = {
            "1": _odd(item.get("1")),
            "X": _odd(item.get("X", 0)),
            "2": _odd(item.get("2")),
        }
        # 0.5% conservative penalty for non-Pinnacle sources
        if source.lower() not in ("pinnacle", "pinnacle sports"):
            odds = {k: round(v * 1.005, 4) if v > 1.01 else v for k, v in odds.items()}
            log.info("Betfair fallback for %s (+0.5%% penalty)", matched)

        result[matched] = odds

    log.info("Pinnacle/Gemini: %d/%d prices received", len(result), len(names))
    return result


def fetch_estimated_prices(matches: list) -> dict:
    """
    Tier 3 fallback — Gemini internal knowledge (NO web search).
    Asks Gemini to estimate fair decimal odds from training data.
    Applies a 2% conservative margin on all estimated prices.
    Always returns prices (no 'introuvable') — useful when Odds API quota is exhausted
    and Gemini Search fails to find real Pinnacle lines.
    Returns {match_name: {"1": float, "X": float, "2": float}}.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not matches:
        return {}

    from datetime import date
    today = date.today().isoformat()

    names = [m["match"] for m in matches[:20]]
    match_list = "\n".join(
        f"- {m['match']} ({m.get('league', '?')}, {m.get('sport', 'soccer')})"
        for m in matches[:20]
    )

    prompt = (
        f"Today is {today}. You are a professional sports analyst with expertise in "
        f"football, tennis, and basketball betting markets.\n\n"
        f"For each match below, estimate the fair decimal odds reflecting the TRUE probability "
        f"of each outcome. Base this on team quality, head-to-head history, recent form, "
        f"and typical market pricing for this competition. Be precise — use realistic odds "
        f"typical of Pinnacle or Betfair closing lines.\n\n"
        f"Matches:\n{match_list}\n\n"
        f"IMPORTANT: Do NOT search the web. Use only your training knowledge.\n"
        f"Return ONLY a valid JSON array:\n"
        f'[{{"match":"Team A vs Team B","1":2.10,"X":3.40,"2":3.20}}]\n'
        f"X=0 for tennis/basketball. Include ALL matches from the list."
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=45)
        except Exception as e:
            log.error("Estimator/Gemini request error: %s", e)
            return {}
        if r.status_code == 429:
            wait = 65 if attempt == 0 else 30
            log.warning("Estimator/Gemini rate limit — waiting %ds", wait)
            time.sleep(wait)
            r = None
            continue
        break

    if r is None or r.status_code != 200:
        if r is not None:
            log.error("Estimator/Gemini HTTP %d", r.status_code)
        return {}

    try:
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m_arr = re.search(r'\[[\s\S]*\]', text)
        if not m_arr:
            log.warning("Estimator/Gemini: no JSON array in response")
            return {}
        raw = json.loads(m_arr.group())
    except Exception as e:
        log.error("Estimator/Gemini parse error: %s", e)
        return {}

    names_set = set(names)
    result = {}
    # Conservative margin: inflate estimated prices by 2% (reduces apparent edge)
    MARGIN = 1.02

    for item in raw:
        ret_name = item.get("match", "").strip()
        if not ret_name:
            continue
        matched = ret_name if ret_name in names_set else _fuzzy_match_name(ret_name, names)
        if not matched:
            continue
        odds = {
            "1": round(_odd(item.get("1")) * MARGIN, 4) if _odd(item.get("1")) > 1.01 else 0.0,
            "X": round(_odd(item.get("X", 0)) * MARGIN, 4) if _odd(item.get("X", 0)) > 1.01 else 0.0,
            "2": round(_odd(item.get("2")) * MARGIN, 4) if _odd(item.get("2")) > 1.01 else 0.0,
        }
        result[matched] = odds

    log.info("Estimator/Gemini: %d/%d estimated prices", len(result), len(names))
    return result
