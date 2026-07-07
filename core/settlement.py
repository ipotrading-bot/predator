"""
core/settlement.py — PAIM v8.5 — Match Settlement Engine
Fetches actual match scores via Gemini Search → determines WIN/LOSS/PUSH
→ updates signal status to 'settled' in Supabase.
"""
import json
import logging
import os
import re

from core.http_utils import post_with_retry

log = logging.getLogger("PREDATOR.settlement")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
_SETTLEMENT_OPTIONAL = {"outcome", "settled_at"}


def fetch_match_result(match_name: str, sport: str, match_date: str = "") -> dict | None:
    """
    Gemini Search → final score of a completed match.
    Returns {"home_score": int, "away_score": int, "completed": True} or None.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    sport_ctx = {"soccer": "football/soccer",
                 "basketball": "NBA basketball",
                 "tennis": "tennis"}.get(sport, sport)
    context = match_name + (f" (date: {match_date})" if match_date else "")

    prompt = (
        f"Use Google Search to find the FINAL SCORE of this {sport_ctx} match:\n"
        f"{context}\n\n"
        f"Return ONLY valid JSON. If match finished:\n"
        f'{{"completed":true,"home_score":2,"away_score":1}}\n'
        f"If not finished or not found:\n"
        f'{{"completed":false}}'
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 80},
    }

    r = post_with_retry(f"{GEMINI_URL}?key={api_key}", payload, timeout=25,
                         max_attempts=2, label=f"Settlement/{match_name}")

    if r is None or r.status_code != 200:
        return None

    try:
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text  = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text  = re.sub(r'```(?:json)?|```', '', text)
        m     = re.search(r'\{[^{}]+\}', text)
        if not m:
            return None
        data  = json.loads(m.group())
        if data.get("completed"):
            return {
                "home_score": int(data.get("home_score", 0)),
                "away_score": int(data.get("away_score", 0)),
                "completed": True,
            }
    except Exception as e:
        log.error("settlement parse [%s]: %s", match_name, e)
    return None


def determine_outcome(sport: str, market_key: str, selection_name: str,
                      home: str, away: str,
                      home_score: int, away_score: int) -> str:
    """Returns 'WIN', 'LOSS', or 'PUSH'."""
    sel    = (selection_name or "").lower().strip()
    home_l = (home or "").lower().strip()
    is_home = bool(sel and (sel in home_l or home_l in sel or sel == home_l))

    if market_key == "h2h" and sport == "soccer":
        if home_score == away_score:
            return "PUSH"
        won = (is_home and home_score > away_score) or (not is_home and away_score > home_score)
        return "WIN" if won else "LOSS"

    if market_key == "h2h":
        won = (is_home and home_score > away_score) or (not is_home and away_score > home_score)
        return "WIN" if won else "LOSS"

    if "totals" in market_key:
        total = home_score + away_score
        try:
            line = float(re.search(r'[\d.]+', sel).group())
        except Exception:
            return "UNKNOWN"
        if total == line:
            return "PUSH"
        return "WIN" if ("over" in sel and total > line) or ("under" in sel and total < line) else "LOSS"

    if "spreads" in market_key:
        try:
            point = float(re.search(r'[-+]?[\d.]+', sel).group())
        except Exception:
            return "UNKNOWN"
        if "spreads_home" in market_key:
            adjusted = home_score + point
        else:
            adjusted = away_score + point
        opp = away_score if "spreads_home" in market_key else home_score
        if adjusted == opp:
            return "PUSH"
        return "WIN" if adjusted > opp else "LOSS"

    return "UNKNOWN"


def settle_signal(sb, sig: dict, now_iso: str) -> bool:
    """
    Try to settle one signal using real match score.
    Returns True if settled, False if score not found.
    """
    match   = sig["match"]
    sport   = sig.get("sport", "soccer")
    scanned = (sig.get("scanned_at") or "")[:10]

    # Use match_time date for Gemini search accuracy (not scanned_at)
    match_date = (sig.get("match_time") or sig.get("scanned_at") or "")[:10]

    result = fetch_match_result(match, sport, match_date)
    if not result or not result.get("completed"):
        return False

    hs  = result["home_score"]
    as_ = result["away_score"]
    home = match.split(" vs ")[0].strip() if " vs " in match else ""
    away = match.split(" vs ")[1].strip() if " vs " in match else ""
    outcome = determine_outcome(
        sport, sig.get("market_key", "h2h"),
        sig.get("selection_name", ""),
        home, away, hs, as_,
    )

    orig_pin = sig.get("pinnacle_price") or 0.0
    clv = round((sig["xbet_odd"] / orig_pin - 1) * 100, 2) if orig_pin > 1.01 else 0.0

    match_time = sig.get("match_time")
    scanned_at = sig.get("scanned_at")
    ttm = None
    if match_time and scanned_at:
        try:
            from datetime import datetime as _dt
            mt = _dt.fromisoformat(match_time.replace("Z", "+00:00"))
            sc = _dt.fromisoformat(scanned_at.replace("Z", "+00:00"))
            ttm = int((mt - sc).total_seconds() / 60)
        except Exception:
            pass

    # DELETE + INSERT — RLS anon key blocks UPDATE
    merged = {**sig, **{
        "status":    "settled",
        "clv_pct":   float(clv),
        "closed_at": now_iso,
        "outcome":   outcome,
    }}
    sig_id = merged.pop("id", None)
    try:
        sb.table("signals").delete().eq("id", sig_id).execute()
    except Exception as e:
        log.error("settle_signal delete [%s]: %s", match, e)
        return False
    try:
        sb.table("signals").insert(merged).execute()
        log.info("SETTLED  | %s %d-%d | outcome=%s | CLV %+.2f%%",
                 match, hs, as_, outcome, clv)
    except Exception as e:
        if any(c in str(e) for c in _SETTLEMENT_OPTIONAL):
            core = {k: v for k, v in merged.items() if k not in _SETTLEMENT_OPTIONAL}
            try:
                sb.table("signals").insert(core).execute()
                log.info("SETTLED (schema fallback) | %s", match)
            except Exception as e2:
                log.critical("SIGNAL %s LOST after delete — settle fallback failed: %s", sig_id, e2)
                return False
        else:
            log.critical("SIGNAL %s LOST after delete — settle insert failed: %s", sig_id, e)
            return False

    # Feed ai_learning_ledger with real settled outcome
    try:
        sb.table("ai_learning_ledger").insert({
            "signal_id":             sig_id,
            "match":                 match,
            "sport":                 sport,
            "league":                sig.get("league"),
            "market_type":           sig.get("market_key"),
            "market":                sig.get("market"),
            "selection":             sig.get("selection_name"),
            "odds":                  sig.get("xbet_odd"),
            "time_to_match_minutes": ttm,
            "initial_edge":          sig.get("edge_pct"),
            "clv_final":             float(clv),
            "was_clv_positive":      clv > 0,
            "outcome":               outcome,
        }).execute()
    except Exception as e:
        log.warning("ai_learning_ledger [%s]: %s", match, e)

    return True
