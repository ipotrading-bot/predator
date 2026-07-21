"""
core/harvester.py — PAIM v7.5 — Guerrilla Mode
Soft source : 1XBet direct feed (JSON) → recherche web fallback
Sharp source: recherche web (Groq/Tavily, core/ai_search.py) → Pinnacle prices
Sports: 1=Soccer, 3=Tennis, 4=Basketball
All timestamps : UTC/GMT.

2026-07-21 : Gemini supprimé partout — grounding gratuit mort (limit: 0
sans facturation prépayée Gemini, vérifié sur 4 clés/projets). Remplacé
par core/ai_search.py : groq/compound-mini (recherche web intégrée) +
fallback Tavily/llama-3.3-70b. Les GEMINI_API_KEY* ne sont plus lus.
"""
import logging
import os
import re
import json
import time
import random
import requests
from datetime import datetime, timedelta, timezone

from core.ai_search import ai_available, ai_complete, ai_search_complete
from core.paim_engine import SPORT_LABELS, strict_team_match

# ── UTC sub-logger (inherits handler from PREDATOR root) ─────────────
log = logging.getLogger("PREDATOR.harvester")

SPORT_IDS = {1: "soccer", 3: "tennis", 4: "basketball", 5: "mma"}

XBET_FEED_TPLS = [
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&partner=157",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&country=255",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=1&partner=157",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=1",
    "https://1xbet.cm/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
]

# Task 6 — additional soft books for line shopping. Melbet/22bet are widely
# documented as running the same LineFeed backend as 1xbet (same platform
# family, near-identical site/app), so the endpoint SHAPE below mirrors
# XBET_FEED_TPLS exactly — but the exact URL/partner id for each was NOT
# live-verified from this sandbox: outbound requests to 1xbet.com itself
# get Cloudflare-redirected to /en/block from this environment's IP (bot/geo
# gate), so even the already-working 1xbet integration can't be exercised
# here, let alone a brand-new one. _fetch_from_book() below degrades to []
# on any failure (same as the pre-existing 1xbet behavior), so a wrong URL
# here just means that book contributes nothing — confirm these actually
# return data (check the Predator Engine GitHub Actions logs for
# "Melbet <sport> OK" / "22bet <sport> OK" lines) before relying on them.
MELBET_FEED_TPLS = [
    "https://melbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&partner=169",
    "https://melbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
]
BET22_FEED_TPLS = [
    "https://22bet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
]

# name -> (url templates, referer) — extend this dict to add more books;
# _fetch_multi_book() below iterates it generically.
SOFT_BOOKS = {
    "1xbet":  (XBET_FEED_TPLS,  "https://1xbet.com/en/line/"),
    "melbet": (MELBET_FEED_TPLS, "https://melbet.com/en/line/"),
    "22bet":  (BET22_FEED_TPLS,  "https://22bet.com/en/line/"),
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://1xbet.com/en/line/",
}
_SPORT_PROMPTS = {
    "soccer":     "top European football/soccer",
    "tennis":     "ATP or WTA tennis (Roland Garros, Italian Open, or similar)",
    "basketball": "NBA playoff or top basketball",
    "mma":        "UFC or major MMA fights",
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


def _fetch_from_book(book: str, url_templates: list, referer: str, sport_id: int) -> list:
    """Try each URL variant for one soft book with a small random delay.
    Returns list of matches (odds keyed "odds_1xbet" regardless of book —
    see _fetch_multi_book for why) or [] on total failure. Generalized
    from the original 1xbet-only fetch (Task 6) so the same retry/parse
    logic covers every book in SOFT_BOOKS without duplication."""
    sport_name = SPORT_IDS.get(sport_id, str(sport_id))
    headers = {**HEADERS, "Referer": referer}
    for tpl in url_templates:
        url = tpl.format(sport_id=sport_id)
        try:
            time.sleep(random.uniform(2, 5))
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                matches = _parse_xbet_json(data, sport_id)
                if matches:
                    log.info("%s %s OK: %d matches via %s", book, sport_name, len(matches), url.split("?")[0])
                    return matches
        except Exception as e:
            log.warning("%s %s fail (%s): %s", book, sport_name, url.split("?")[0], e)
    return []


def _fetch_from_1xbet(sport_id):
    """Back-compat wrapper — 1xbet alone, no line shopping. Prefer
    _fetch_multi_book() for the best-price-across-books behavior."""
    tpls, referer = SOFT_BOOKS["1xbet"]
    return _fetch_from_book("1xbet", tpls, referer, sport_id)


def _fuzzy_match_event(candidate: dict, pool: list[dict]) -> dict | None:
    """Find `candidate`'s counterpart in `pool` by team-name fuzzy match
    (core.paim_engine.strict_team_match) — used to line up the same
    real-world match across different soft books before comparing prices."""
    for other in pool:
        if strict_team_match(candidate["home"], other["home"]) and \
           strict_team_match(candidate["away"], other["away"]):
            return other
    return None


def _fetch_multi_book(sport_id: int) -> list:
    """
    Task 6 — line shopping: fetch every configured soft book (SOFT_BOOKS)
    for this sport and, for each real-world match found on 2+ books, keep
    the BEST (highest) price per outcome across all of them — not just
    whichever book happened to respond first. `_soft_source` on the
    returned match records which book contributed each surviving price
    (or a "+" joined list when outcomes came from different books), for
    display/debugging attribution.

    Falls back gracefully: if only one book responds, its prices are used
    as-is (identical behavior to the old single-book fetch).
    """
    per_book: dict[str, list] = {}
    for book, (tpls, referer) in SOFT_BOOKS.items():
        found = _fetch_from_book(book, tpls, referer, sport_id)
        if found:
            per_book[book] = found

    if not per_book:
        return []

    books_in_order = list(per_book.keys())
    merged: list[dict] = list(per_book[books_in_order[0]])
    for m in merged:
        m["_soft_source"] = books_in_order[0]

    for book in books_in_order[1:]:
        for cand in per_book[book]:
            existing = _fuzzy_match_event(cand, merged)
            if existing is None:
                cand["_soft_source"] = book
                merged.append(cand)
                continue
            # Same real-world match found on another book — keep the
            # better price per outcome (line shopping), track provenance.
            sources = set(existing["_soft_source"].split("+"))
            improved = False
            for key in ("1", "X", "2"):
                new_odd = cand["odds_1xbet"].get(key, 0.0)
                cur_odd = existing["odds_1xbet"].get(key, 0.0)
                if new_odd > cur_odd:
                    existing["odds_1xbet"][key] = new_odd
                    improved = True
            if improved:
                sources.add(book)
                existing["_soft_source"] = "+".join(sorted(sources))

    return merged


def _fetch_from_gemini(sport_id):
    """Recherche web fallback — finds REAL upcoming matches. Returns list or []."""
    if not ai_available():
        return []

    sport_name = SPORT_IDS.get(sport_id, "sport")
    sport_desc = _SPORT_PROMPTS.get(sport_name, sport_name)
    is_soccer  = sport_id == 1

    from datetime import date
    today = date.today().isoformat()

    draw_field = ',"X":3.40' if is_soccer else ',"X":0'
    prompt = (
        f"Today is {today}. Search the web to find 6 REAL {sport_desc} matches "
        f"actually scheduled today or in the next 48 hours. "
        f"DO NOT invent or hallucinate matches — only include confirmed scheduled games. "
        f"For each real match found, estimate realistic 1XBet decimal odds. "
        f"{'Include the draw odd X for every football match.' if is_soccer else 'Set X to 0 for non-soccer.'}"
        f"\nReturn ONLY a valid JSON array (no other text):\n"
        f'[{{"match":"Team A vs Team B","home":"Team A","away":"Team B",'
        f'"league":"League Name","sport":"{sport_name}",'
        f'"odds_1xbet":{{"1":2.10{draw_field},"2":3.20}}}}]'
    )

    text = ai_search_complete(
        prompt,
        queries=[f"{sport_desc} matches today schedule"],
        label=f"Harvest/{sport_name}",
        max_tokens=1024, temperature=0.1, timeout=60,
    )
    if not text:
        return []

    try:
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

        log.info("Harvest/%s: %d valid matches", sport_name, len(matches))
        return matches

    except Exception as e:
        log.error("Harvest/%s exception: %s", sport_name, e)
        return []


_GEMINI_INTER_SPORT_SLEEP = 5  # seconds — évite un burst rate-limit Groq quand tous les sports tombent en fallback

def fetch_matches():
    """Fetch matches for all configured sports, line-shopping the best
    price per outcome across every book in SOFT_BOOKS (Task 6). Returns
    combined list."""
    all_matches = []
    gemini_calls = 0
    for sport_id in SPORT_IDS:
        matches = _fetch_multi_book(sport_id)
        if not matches:
            if gemini_calls > 0:
                time.sleep(_GEMINI_INTER_SPORT_SLEEP)
            matches = _fetch_from_gemini(sport_id)
            gemini_calls += 1
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
    Sharp source — recherche web (Groq/Tavily) → Pinnacle decimal odds.
    Falls back to Betfair Exchange per match when Pinnacle line is unavailable.
    Uses fuzzy team matching to handle 1XBet abbreviation vs Pinnacle full name divergence.
    Returns {match_name: {"1": float, "X": float, "2": float}}.
    """
    if not ai_available() or not matches:
        return {}

    from datetime import date
    today = date.today().isoformat()

    names = [m["match"] for m in matches[:25]]
    match_list = "\n".join(
        f"- {m['match']} [{m.get('league', '?')}, {m.get('sport', 'soccer')}]"
        for m in matches[:25]
    )

    prompt = (
        f"Today is {today}. Search the web to find current decimal odds for each match below.\n"
        f"Search PINNACLE SPORTS first. If Pinnacle has no line for a match, search BETFAIR EXCHANGE.\n"
        f"Add a 'source' field: 'Pinnacle' or 'Betfair'.\n\n"
        f"Matches (name [league, sport]):\n{match_list}\n\n"
        f"Return ONLY a valid JSON array. Use the exact match name from the list:\n"
        f'[{{"match":"Team A vs Team B","1":2.05,"X":3.35,"2":3.60,"source":"Pinnacle"}}]\n'
        f"X=0 for non-soccer. Omit matches with no odds found on either book."
    )

    # Budget Tavily : 4 requêtes max sur les premiers matchs — compound-mini
    # (étage 1) fait sa propre recherche et n'en consomme aucune.
    text = ai_search_complete(
        prompt,
        queries=[f"Pinnacle odds {n}" for n in names[:4]],
        label="Pinnacle/Search",
        max_tokens=2048, temperature=0.1, timeout=90,
    )
    if not text:
        return {}

    try:
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m_arr = re.search(r'\[[\s\S]*\]', text)
        if not m_arr:
            log.warning("Pinnacle/Search: no JSON array in response")
            return {}

        raw = json.loads(m_arr.group())
    except Exception as e:
        log.error("Pinnacle/Search parse exception: %s", e)
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
            log.debug("Pinnacle/Search: no local match for '%s'", ret_name)
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

    log.info("Pinnacle/Search: %d/%d prices received", len(result), len(names))
    return result


def fetch_estimated_prices(matches: list) -> dict:
    """
    Tier 3 fallback — connaissance interne du LLM (NO web search).
    Asks the model to estimate fair decimal odds from training data.
    Applies a conservative margin on all estimated prices.
    Always returns prices (no 'introuvable') — useful when Odds API quota is exhausted
    and web search fails to find real Pinnacle lines.
    Returns {match_name: {"1": float, "X": float, "2": float}}.
    """
    if not ai_available() or not matches:
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

    text = ai_complete(prompt, label="Estimator/AI",
                       max_tokens=2048, temperature=0.2, timeout=60)
    if not text:
        return {}

    try:
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m_arr = re.search(r'\[[\s\S]*\]', text)
        if not m_arr:
            log.warning("Estimator/AI: no JSON array in response")
            return {}
        raw = json.loads(m_arr.group())
    except Exception as e:
        log.error("Estimator/AI parse error: %s", e)
        return {}

    names_set = set(names)
    result = {}
    # Conservative margin: inflate estimated prices by 0.5% (was 2%, artificially killed edges)
    MARGIN = 1.005

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

    log.info("Estimator/AI: %d/%d estimated prices", len(result), len(names))
    return result


def fetch_mma_events() -> list[dict]:
    """
    Recherche web (Groq/Tavily) → upcoming UFC/MMA fights with BOTH
    Melbet (soft) and Pinnacle (sharp) decimal ML odds.
    Returns events in the standard engine format with _soft_source="Melbet".
    No OddsAPI calls — fully search-based to avoid quota usage.
    """
    if not ai_available():
        return []

    from datetime import date
    today = date.today().isoformat()

    prompt = (
        f"Today is {today}. Search the web to find all upcoming UFC or major MMA fights "
        f"scheduled in the next 7 days.\n\n"
        f"For each fight, search for:\n"
        f"1. Current Melbet decimal moneyline odds (no draw — only winner odds)\n"
        f"2. Current Pinnacle decimal moneyline odds\n\n"
        f"Return ONLY a valid JSON array. Omit any fight where you cannot confirm both books have lines:\n"
        f'[{{"match":"Fighter A vs Fighter B","home":"Fighter A","away":"Fighter B",'
        f'"event":"UFC 317","commence_time":"2026-05-17T02:00:00Z",'
        f'"odds_melbet":{{"1":1.85,"X":0,"2":2.00}},'
        f'"odds_pinnacle":{{"1":1.75,"X":0,"2":2.10}}}}]\n\n'
        f"X must always be 0 (no draw in MMA). Include the event name. "
        f"Only include fights with confirmed lines on BOTH Melbet AND Pinnacle."
    )

    text = ai_search_complete(
        prompt,
        queries=["UFC upcoming fights this week odds moneyline",
                 "UFC next event fight card Pinnacle odds"],
        label="MMA/Search",
        max_tokens=2048, temperature=0.1, timeout=90,
    )
    if not text:
        return []

    try:
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m_arr = re.search(r'\[[\s\S]*\]', text)
        if not m_arr:
            log.warning("MMA/Search: no JSON array in response")
            return []
        raw = json.loads(m_arr.group())
    except Exception as e:
        log.error("MMA/Search parse error: %s", e)
        return []

    events = []
    for i, ev in enumerate(raw):
        try:
            home = str(ev.get("home", "")).strip()
            away = str(ev.get("away", "")).strip()
            if not home or not away:
                continue
            om = ev.get("odds_melbet", {})
            op = ev.get("odds_pinnacle", {})
            xbet_h = _odd(om.get("1")); xbet_a = _odd(om.get("2"))
            pin_h  = _odd(op.get("1")); pin_a  = _odd(op.get("2"))
            if xbet_h <= 1.01 or xbet_a <= 1.01 or pin_h <= 1.01 or pin_a <= 1.01:
                continue
            events.append({
                "id":            f"mma_gemini_{i}",
                "match":         ev.get("match", f"{home} vs {away}"),
                "home":          home,
                "away":          away,
                "league":        ev.get("event", "UFC"),
                "sport":         "mma",
                "sport_id":      5,
                "commence_time": ev.get("commence_time", ""),
                "odds_1xbet":    {"1": xbet_h, "X": 0.0, "2": xbet_a},
                "odds_pinnacle": {"1": pin_h,  "X": 0.0, "2": pin_a},
                "_soft_source":  "Melbet",
            })
        except Exception:
            continue

    log.info("MMA/Search: %d events found (Melbet vs Pinnacle)", len(events))
    return events


def fetch_esports_events() -> list[dict]:
    """
    Recherche web (Groq/Tavily) → upcoming eSports matches with BOTH
    1XBet (soft) and Pinnacle (sharp) decimal ML odds.
    Cibles : CS2, League of Legends, Valorant, DOTA2 — tournois top tier.
    """
    if not ai_available():
        return []

    from datetime import date
    today = date.today().isoformat()

    prompt = (
        f"Today is {today}. Search the web to find upcoming eSports matches "
        f"in CS2, League of Legends, Valorant, or DOTA2 scheduled in the next 3 days. "
        f"Focus on top-tier tournaments (ESL Pro League, BLAST, LCS, LEC, VCT, The International).\n\n"
        f"For each match, search for:\n"
        f"1. Current 1XBet decimal moneyline odds (no draw — only winner odds)\n"
        f"2. Current Pinnacle decimal moneyline odds\n\n"
        f"Return ONLY a valid JSON array. Omit any match where you cannot confirm both books have lines:\n"
        f'[{{"match":"Team A vs Team B","home":"Team A","away":"Team B",'
        f'"game":"CS2","event":"ESL Pro League","commence_time":"2026-05-17T18:00:00Z",'
        f'"odds_1xbet":{{"1":1.85,"X":0,"2":2.00}},'
        f'"odds_pinnacle":{{"1":1.75,"X":0,"2":2.10}}}}]\n\n'
        f"X must always be 0 (no draw in eSports). "
        f"Only include matches with confirmed lines on BOTH 1XBet AND Pinnacle."
    )

    text = ai_search_complete(
        prompt,
        queries=["CS2 LoL Valorant Dota2 upcoming matches betting odds"],
        label="eSports/Search",
        max_tokens=2048, temperature=0.1, timeout=90,
    )
    if not text:
        return []

    try:
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m_arr = re.search(r'\[[\s\S]*\]', text)
        if not m_arr:
            log.warning("eSports/Search: aucun tableau JSON dans la réponse")
            return []
        raw = json.loads(m_arr.group())
    except Exception as e:
        log.error("eSports/Search parse error: %s", e)
        return []

    events = []
    for i, ev in enumerate(raw):
        try:
            home = str(ev.get("home", "")).strip()
            away = str(ev.get("away", "")).strip()
            if not home or not away:
                continue
            om = ev.get("odds_1xbet", {})
            op = ev.get("odds_pinnacle", {})
            xbet_h = _odd(om.get("1")); xbet_a = _odd(om.get("2"))
            pin_h  = _odd(op.get("1")); pin_a  = _odd(op.get("2"))
            if xbet_h <= 1.01 or xbet_a <= 1.01 or pin_h <= 1.01 or pin_a <= 1.01:
                continue
            game = ev.get("game", "eSports")
            events.append({
                "id":            f"esports_gemini_{i}",
                "match":         ev.get("match", f"{home} vs {away}"),
                "home":          home,
                "away":          away,
                "league":        f"{game} — {ev.get('event', 'Tournoi')}",
                "sport":         "esports",
                "sport_id":      9,
                "commence_time": ev.get("commence_time", ""),
                "odds_1xbet":    {"1": xbet_h, "X": 0.0, "2": xbet_a},
                "odds_pinnacle": {"1": pin_h,  "X": 0.0, "2": pin_a},
            })
        except Exception:
            continue

    log.info("eSports/Search: %d matchs trouvés", len(events))
    return events


def fetch_alternative_sports_batch() -> list[dict]:
    """
    UN SEUL appel recherche web pour Table Tennis + Volleyball + Handball.
    Groupé en une seule requête pour éviter les cascades de rate-limit 429.
    Retourne les événements au format standard moteur.
    """
    if not ai_available():
        return []

    from datetime import date
    today = date.today().isoformat()

    prompt = (
        f"Today is {today}. Search the web to find upcoming matches in the next 3 days "
        f"for these 3 sports: Table Tennis, Volleyball (men/women), Handball.\n\n"
        f"For each match find current 1XBet AND Pinnacle decimal moneyline odds.\n"
        f"Target tournaments:\n"
        f"- Table Tennis: ITTF World Tour, Bundesliga TT, Champions League TT, top Asian leagues\n"
        f"- Volleyball: CEV Champions League, Bundesliga, top European leagues (men/women)\n"
        f"- Handball: EHF Champions League, Bundesliga, Liga ASOBAL\n\n"
        f"Return ONLY a valid JSON array. Omit any match without confirmed odds on BOTH books:\n"
        f'[{{"match":"Team A vs Team B","home":"Team A","away":"Team B","sport":"tabletennis",'
        f'"event":"ITTF World Tour","commence_time":"2026-05-18T14:00:00Z",'
        f'"odds_1xbet":{{"1":1.70,"X":0,"2":2.10}},'
        f'"odds_pinnacle":{{"1":1.60,"X":0,"2":2.25}}}}]\n\n'
        f'sport field must be exactly: "tabletennis", "volleyball", or "handball". '
        f"X=0 always (no draw in these sports). Only include matches with confirmed lines on BOTH books."
    )

    text = ai_search_complete(
        prompt,
        queries=["table tennis volleyball handball matches today betting odds"],
        label="AltSports/Search",
        max_tokens=3000, temperature=0.1, timeout=90,
    )
    if not text:
        return []

    try:
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        m_arr = re.search(r'\[[\s\S]*\]', text)
        if not m_arr:
            log.warning("AltSports/Search: aucun tableau JSON")
            return []
        raw = json.loads(m_arr.group())
    except Exception as e:
        log.error("AltSports/Search parse error: %s", e)
        return []

    _SPORT_IDS = {"tabletennis": 14, "volleyball": 13, "handball": 15}
    events = []
    counts: dict[str, int] = {}
    for i, ev in enumerate(raw):
        try:
            home = str(ev.get("home", "")).strip()
            away = str(ev.get("away", "")).strip()
            if not home or not away:
                continue
            sport = str(ev.get("sport", "")).strip().lower()
            if sport not in _SPORT_IDS:
                continue
            om = ev.get("odds_1xbet", {})
            op = ev.get("odds_pinnacle", {})
            xbet_h = _odd(om.get("1")); xbet_a = _odd(om.get("2"))
            pin_h  = _odd(op.get("1")); pin_a  = _odd(op.get("2"))
            if xbet_h <= 1.01 or xbet_a <= 1.01 or pin_h <= 1.01 or pin_a <= 1.01:
                continue
            events.append({
                "id":            f"{sport}_gemini_{i}",
                "match":         ev.get("match", f"{home} vs {away}"),
                "home":          home,
                "away":          away,
                "league":        ev.get("event", sport.title()),
                "sport":         sport,
                "sport_id":      _SPORT_IDS[sport],
                "commence_time": ev.get("commence_time", ""),
                "odds_1xbet":    {"1": xbet_h, "X": 0.0, "2": xbet_a},
                "odds_pinnacle": {"1": pin_h,  "X": 0.0, "2": pin_a},
            })
            counts[sport] = counts.get(sport, 0) + 1
        except Exception:
            continue

    log.info("AltSports/Search: %d matchs — %s",
             len(events), " | ".join(f"{s}={n}" for s, n in counts.items()))
    return events


# ── Betfair Exchange (Tier 1.5 — sharp prices peer-to-peer) ──────────

_BETFAIR_LOGIN_URL      = "https://identitysso.betfair.com/api/login"
_BETFAIR_CERTLOGIN_URL  = "https://identitysso-cert.betfair.com/api/certlogin"
_BETFAIR_API_URL        = "https://api.betfair.com/exchange/betting/rest/v1.0"
_BETFAIR_COMMISSION     = 0.05   # Standard 5% commission on net winnings

_BETFAIR_EVENT_TYPES: dict[str, str] = {
    "soccer":           "1",
    "tennis":           "2",
    "basketball":       "7522",
    "cricket":          "4",
    "rugby":            "451485",
    "boxing":           "6",
    "mma":              "26420387",
    "hockey":           "7524",
    "americanfootball": "6423",
    "darts":            "3503",
    "baseball":         "7511",
}

_betfair_session: dict = {}


def _betfair_login() -> bool:
    """
    Non-Interactive (bot) login via client-cert mutual TLS — the ONLY login
    method Betfair supports for unattended/automated callers. The old
    identitysso.betfair.com/api/login endpoint is the Interactive method
    (meant for a human completing a browser session) and returns an empty/
    non-JSON body when called headlessly — confirmed live 2026-07-09
    ("Betfair login: Expecting value: line 1 column 1 (char 0)" on every
    scan, 0 markets ever fetched). BETFAIR_CERT/BETFAIR_CERT_KEY hold the
    PEM cert/key content as GitHub secrets; written to temp files here
    because requests' `cert=` param requires filesystem paths, not PEM
    strings. See https://identitysso-cert.betfair.com/api/certlogin docs:
    response uses `sessionToken`/`loginStatus`, NOT `token`/`status` like
    the interactive endpoint.
    """
    username  = os.environ.get("BETFAIR_USERNAME", "")
    password  = os.environ.get("BETFAIR_PASSWORD", "")
    app_key   = os.environ.get("BETFAIR_APP_KEY",  "")
    cert_pem  = os.environ.get("BETFAIR_CERT", "")
    key_pem   = os.environ.get("BETFAIR_CERT_KEY", "")
    if not all([username, password, app_key, cert_pem, key_pem]):
        return False
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False) as cf, \
             tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=False) as kf:
            cf.write(cert_pem)
            kf.write(key_pem)
            cert_path, key_path = cf.name, kf.name
        try:
            r = requests.post(
                _BETFAIR_CERTLOGIN_URL,
                data={"username": username, "password": password},
                headers={
                    "Content-Type":  "application/x-www-form-urlencoded",
                    "Accept":        "application/json",
                    "X-Application": app_key,
                },
                cert=(cert_path, key_path),
                timeout=15,
            )
        finally:
            os.unlink(cert_path)
            os.unlink(key_path)
        data = r.json()
        if data.get("loginStatus") == "SUCCESS":
            _betfair_session["token"]   = data["sessionToken"]
            _betfair_session["app_key"] = app_key
            log.info("Betfair: session ouverte (cert login)")
            return True
        log.warning("Betfair login: %s", data.get("loginStatus", "FAILED"))
        return False
    except Exception as e:
        log.error("Betfair login: %s", e)
        return False


def _bf_request(endpoint: str, body: dict):
    token   = _betfair_session.get("token",   "")
    app_key = _betfair_session.get("app_key", "")
    if not token:
        return None
    try:
        r = requests.post(
            f"{_BETFAIR_API_URL}/{endpoint}/",
            json=body,
            headers={
                "X-Authentication": token,
                "X-Application":    app_key,
                "Content-Type":     "application/json",
                "Accept":           "application/json",
            },
            timeout=20,
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.error("Betfair %s: %s", endpoint, e)
        return None


def fetch_betfair_prices(sports: list = None, hours_ahead: int = 48) -> dict:
    """
    Betfair Exchange Tier 1.5 — back prices for Win/MATCH_ODDS markets.
    Returns {norm_key: {"match": str, "home": str, "away": str,
                        "1": float, "X": float, "2": float}}
    norm_key = "home_lower_away_lower" for fuzzy lookup.
    Prices are commission-adjusted (×0.95 on profit) — comparable to Pinnacle closing lines.
    Returns {} when BETFAIR_APP_KEY is not set or login fails.
    """
    if not os.environ.get("BETFAIR_APP_KEY"):
        return {}
    if not _betfair_session.get("token"):
        if not _betfair_login():
            return {}

    if sports is None:
        sports = ["soccer", "tennis", "basketball", "hockey", "mma", "cricket"]

    event_type_ids = [_BETFAIR_EVENT_TYPES[s] for s in sports if s in _BETFAIR_EVENT_TYPES]
    if not event_type_ids:
        return {}

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)

    catalogue = _bf_request("listMarketCatalogue", {
        "filter": {
            "eventTypeIds":    event_type_ids,
            "marketTypeCodes": ["MATCH_ODDS"],
            "marketStartTime": {
                "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":   until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "inPlayOnly": False,
        },
        "maxResults":       "50",
        "marketProjection": ["RUNNER_DESCRIPTION", "EVENT"],
        "sort":             "FIRST_TO_START",
    })

    if not catalogue:
        log.info("Betfair: 0 marchés retournés")
        return {}

    market_ids = [m["marketId"] for m in catalogue][:25]
    books = _bf_request("listMarketBook", {
        "marketIds":       market_ids,
        "priceProjection": {
            "priceData":             ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {"bestPricesDepth": 1},
        },
    })

    if not books:
        return {}

    cat_map = {m["marketId"]: m for m in catalogue}
    result: dict = {}

    for book in books:
        try:
            mid      = book["marketId"]
            cat_desc = cat_map.get(mid, {}).get("runners", [])
            names    = {r["selectionId"]: r.get("runnerName", "?") for r in cat_desc}

            prices: dict[str, float] = {}
            for runner in book.get("runners", []):
                sid   = runner["selectionId"]
                rname = names.get(sid, "?")
                backs = runner.get("ex", {}).get("availableToBack", [])
                if backs and float(backs[0].get("price", 0)) > 1.01:
                    raw = float(backs[0]["price"])
                    # Commission-adjust so price is net of 5% Betfair fee
                    prices[rname] = round(1 + (raw - 1) * (1 - _BETFAIR_COMMISSION), 4)

            if len(prices) < 2:
                continue

            draw_price = next((p for n, p in prices.items() if "draw" in n.lower()), 0.0)
            teams      = [(n, p) for n, p in prices.items() if "draw" not in n.lower()]
            if len(teams) < 2:
                continue

            home_name, home_p = teams[0]
            away_name, away_p = teams[1]
            norm_key = f"{home_name.lower().strip()}_{away_name.lower().strip()}"

            result[norm_key] = {
                "match": f"{home_name} vs {away_name}",
                "home":  home_name,
                "away":  away_name,
                "1":     home_p,
                "X":     draw_price,
                "2":     away_p,
            }
        except Exception:
            continue

    log.info("Betfair: %d marchés avec prix (commission -5%%)", len(result))
    return result
