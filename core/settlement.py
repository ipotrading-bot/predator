"""
core/settlement.py — PAIM v8.5 — Match Settlement Engine
Fetches actual match scores via web search (Groq/Tavily, see core/ai_search.py)
→ determines WIN/LOSS/PUSH → updates signal status to 'settled' in Supabase.
"""
import json
import logging
import re

from core.ai_search import ai_available, ai_search_complete
from core.db import log_to_ledger, replace_signal_row
from core.paim_engine import resolve_selection_side

log = logging.getLogger("PREDATOR.settlement")

_SETTLEMENT_OPTIONAL = frozenset({"outcome", "settled_at"})


def fetch_match_result(match_name: str, sport: str, match_date: str = "") -> dict | None:
    """
    Recherche web → final score of a completed match.
    Returns {"home_score": int, "away_score": int, "completed": True} or None.
    """
    if not ai_available():
        return None

    sport_ctx = {"soccer": "football/soccer",
                 "basketball": "NBA basketball",
                 "euroleague_basketball": "Euroleague basketball",
                 "americanfootball": "NFL american football",
                 # NCAAF : « NFL » biaiserait la recherche vers la mauvaise ligue
                 "college_football": "NCAA college football",
                 "tennis": "tennis"}.get(sport, sport)
    context = match_name + (f" (date: {match_date})" if match_date else "")

    prompt = (
        f"Search the web to find the FINAL SCORE of this {sport_ctx} match:\n"
        f"{context}\n\n"
        f"Return ONLY valid JSON. If match finished:\n"
        f'{{"completed":true,"home_score":2,"away_score":1}}\n'
        f"If not finished or not found:\n"
        f'{{"completed":false}}'
    )

    text = ai_search_complete(
        prompt,
        queries=[f"{match_name} {sport_ctx} final score result {match_date}".strip()],
        label=f"Settlement/{match_name}",
        max_tokens=500, temperature=0.0, timeout=45,
        # Lane SACRÉE : sa réserve journalière ne peut être entamée par aucune
        # autre lane. Le 2026-08-02, le scan avait épuisé le TPD Groq et le
        # settlement n'a plus rien réglé de la journée — ledger vide,
        # /performance figé. Voir AI_SETTLEMENT_RESERVE dans core/ai_router.py.
        lane="settlement",
    )
    if not text:
        return None

    try:
        text  = re.sub(r'```(?:json)?|```', '', text)
        m     = re.search(r'\{[^{}]+\}', text)
        if not m:
            log.warning("settlement no-JSON [%s]: text=%r", match_name, text[:200])
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
    """Returns 'WIN', 'LOSS', 'PUSH', or 'UNKNOWN'."""
    sel = (selection_name or "").lower().strip()

    if market_key == "h2h" and sport == "soccer":
        # Resolve the side BEFORE the draw check would otherwise mask an
        # unresolvable selection — an ambiguous selection on a genuine draw
        # happens to be PUSH regardless of side, but a draw is not the only
        # score that reaches this branch.
        is_home = resolve_selection_side(selection_name, home, away)
        if is_home is None:
            return "UNKNOWN"
        if home_score == away_score:
            return "PUSH"
        won = (is_home and home_score > away_score) or (not is_home and away_score > home_score)
        return "WIN" if won else "LOSS"

    if market_key == "h2h":
        is_home = resolve_selection_side(selection_name, home, away)
        if is_home is None:
            return "UNKNOWN"
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
    # NOT real CLV: xbet_odd/pinnacle_price are the exact same two values
    # already used to compute edge_pct at scan time (see paim_engine.compute_alpha),
    # so this is a re-derivation of the entry edge, not a closing-line
    # comparison — it is already stored honestly as `initial_edge` in the
    # ledger (core/db.py:log_to_ledger). Kept here only to populate the
    # legacy `signals.clv_pct` display column until core/audit_engine.py's
    # real closing-line pipeline (Task 3) lands. Never treat this as CLV.
    entry_edge_pct = round((sig["xbet_odd"] / orig_pin - 1) * 100, 2) if orig_pin > 1.01 else 0.0

    # DELETE + INSERT — RLS blocks UPDATE outright on this table's policies.
    # Shared with core/audit_engine.py's _update_signal() — see
    # core/db.py:replace_signal_row for the implementation.
    merged = {**sig, **{
        "status":    "settled",
        "clv_pct":   float(entry_edge_pct),
        "closed_at": now_iso,
        "outcome":   outcome,
    }}
    if not replace_signal_row(sb, sig["id"], merged, optional_cols=_SETTLEMENT_OPTIONAL):
        return False
    log.info("SETTLED  | %s %d-%d | outcome=%s | entry edge %+.2f%%", match, hs, as_, outcome, entry_edge_pct)

    # Feed ai_learning_ledger with the real settled outcome — this is what
    # core/learning_layer.py must key off of (never the clv_final/entry-edge
    # value below, which cannot vary with the actual match result).
    log_to_ledger(sb, sig, float(entry_edge_pct), outcome)

    return True
