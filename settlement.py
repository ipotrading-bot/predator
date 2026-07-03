"""
settlement.py — PREDATOR Settlement Engine v1.0
Scrape résultats réels des matchs → Remplit ai_learning_ledger → CLV audit

Cron: 06:00 et 18:00 UTC (toutes les 12h)
Pipeline:
  1. Récupérer signaux non settlés (status=active, match_time < now)
  2. Scraper résultats (Flashscore + ESPN fallback)
  3. Calculer CLV réalisé (edge réel vs edge attendu)
  4. Insérer dans ai_learning_ledger
  5. Marquer signal comme settled
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ── Logger UTC ─────────────────────────────────────────────────────────
_fmt = logging.Formatter(
    fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_fmt.converter = time.gmtime
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("SETTLEMENT")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Timeouts et retries
SCRAPE_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# Headers pour éviter blocages
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


# ── Result Scraper ─────────────────────────────────────────────────────

def _scrape_flashscore(match_name: str, sport: str) -> Optional[Tuple[int, int]]:
    """
    Scrape Flashscore pour obtenir le score final.
    Retourne (home_score, away_score) ou None si non trouvé.
    """
    try:
        # Normaliser match_name pour URL Flashscore
        parts = match_name.split(" vs ")
        if len(parts) != 2:
            return None
        home, away = parts[0].strip(), parts[1].strip()
        
        # Flashscore search URL (simplifiée pour soccer)
        search_url = f"https://www.flashscore.com/search/?q={home}+vs+{away}"
        
        resp = requests.get(search_url, headers=HEADERS, timeout=SCRAPE_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        
        # Chercher le premier lien résultat
        match_link = soup.find("a", {"class": "eventRowLink"})
        if not match_link:
            log.debug("Flashscore: aucun lien trouvé pour %s", match_name)
            return None
        
        # Visiter la page du match
        match_url = "https://www.flashscore.com" + match_link.get("href", "")
        resp2 = requests.get(match_url, headers=HEADERS, timeout=SCRAPE_TIMEOUT)
        resp2.raise_for_status()
        soup2 = BeautifulSoup(resp2.content, "html.parser")
        
        # Extraire le score final (pattern: "1 - 2")
        score_text = soup2.find("span", {"class": "currentScore"})
        if score_text:
            match_obj = re.search(r"(\d+)\s*-\s*(\d+)", score_text.text)
            if match_obj:
                home_score = int(match_obj.group(1))
                away_score = int(match_obj.group(2))
                log.info("Flashscore OK: %s = %d-%d", match_name, home_score, away_score)
                return (home_score, away_score)
    except Exception as e:
        log.debug("Flashscore error [%s]: %s", match_name, str(e)[:80])
    
    return None


def _scrape_espn(match_name: str, sport: str) -> Optional[Tuple[int, int]]:
    """
    Scrape ESPN pour résultats (fallback si Flashscore échoue).
    Retourne (home_score, away_score) ou None.
    """
    try:
        parts = match_name.split(" vs ")
        if len(parts) != 2:
            return None
        home, away = parts[0].strip(), parts[1].strip()
        
        # ESPN search (example: soccer)
        sport_map = {
            "soccer": "soccer",
            "basketball": "nba",
            "baseball": "mlb",
            "hockey": "nhl",
        }
        sport_key = sport_map.get(sport, sport)
        
        search_url = f"https://www.espn.com/search?query={home}+{away}"
        resp = requests.get(search_url, headers=HEADERS, timeout=SCRAPE_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        
        # Chercher le premier résultat
        result_link = soup.find("a", {"class": "search-item-link"})
        if not result_link:
            log.debug("ESPN: aucun lien trouvé pour %s", match_name)
            return None
        
        match_url = result_link.get("href", "")
        if not match_url.startswith("http"):
            match_url = "https://www.espn.com" + match_url
        
        resp2 = requests.get(match_url, headers=HEADERS, timeout=SCRAPE_TIMEOUT)
        resp2.raise_for_status()
        soup2 = BeautifulSoup(resp2.content, "html.parser")
        
        # Extraire score (pattern: "1 - 2" ou "1-2")
        score_div = soup2.find("div", {"class": "score"})
        if score_div:
            match_obj = re.search(r"(\d+)\s*-\s*(\d+)", score_div.text)
            if match_obj:
                home_score = int(match_obj.group(1))
                away_score = int(match_obj.group(2))
                log.info("ESPN OK: %s = %d-%d", match_name, home_score, away_score)
                return (home_score, away_score)
    except Exception as e:
        log.debug("ESPN error [%s]: %s", match_name, str(e)[:80])
    
    return None


def _fetch_result(match_name: str, sport: str) -> Optional[Tuple[int, int]]:
    """
    Essayer Flashscore → ESPN fallback.
    Retourne (home_score, away_score) avec retry logic.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Tier 1: Flashscore
            result = _scrape_flashscore(match_name, sport)
            if result:
                return result
            
            # Tier 2: ESPN fallback
            result = _scrape_espn(match_name, sport)
            if result:
                return result
            
            log.warning("Fetch result attempt %d/%d: aucun score trouvé pour %s",
                       attempt, MAX_RETRIES, match_name)
            
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        except Exception as e:
            log.error("Fetch result error (attempt %d/%d) [%s]: %s",
                     attempt, MAX_RETRIES, match_name, str(e)[:80])
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    
    return None


# ── CLV Calculation ────────────────────────────────────────────────────

def _calculate_clv(
    xbet_odd: float,
    actual_result: int,  # 1=WIN, 0=LOSS
    sharp_prob: float,
) -> float:
    """
    CLV réalisé = (odd - 1) × result - (1 - result)
    
    Exemple:
      - Pari gagné (result=1) à 2.50 : CLV = (2.50 - 1) × 1 = 1.50 unités
      - Pari perdu (result=0) à 2.50 : CLV = (2.50 - 1) × 0 - 1 = -1.00 unité
    
    CLV attendu (edge) = sharp_prob × (xbet_odd - 1) - (1 - sharp_prob)
    """
    clv_realized = (xbet_odd - 1.0) * actual_result - (1.0 - actual_result)
    return round(clv_realized, 4)


def _calculate_profit_units(
    xbet_odd: float,
    actual_result: int,
    kelly_fraction: float = 0.25,
    bankroll: float = 1000.0,
) -> float:
    """
    Profit réel en unités (sur bankroll de référence).
    
    stake = bankroll × kelly_fraction × (prob × odds - 1) / odds
    profit = stake × (odds - 1) × result - stake × (1 - result)
    """
    # Simplifié : profit_units = CLV × stake normalisée
    # Pour 1000€ bankroll, 25% kelly :
    clv = _calculate_clv(xbet_odd, actual_result, 0.5)  # prob simplifiée
    stake = (bankroll * kelly_fraction) / 100  # ~2.5€ par signal
    profit = clv * stake
    return round(profit, 2)


# ── Main Settlement Pipeline ───────────────────────────────────────────

def _settle_signals(sb, now: datetime):
    """
    Boucle principale : récupérer signaux actifs + scraper résultats.
    """
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("SETTLEMENT ENGINE — %s", now.strftime("%Y-%m-%d %H:%M UTC"))
    
    # ── 1. Récupérer signaux non-settlés (match_time < now, status=active) ──
    try:
        now_iso = now.isoformat()
        res = (sb.table("signals")
               .select("*")
               .eq("status", "active")
               .lt("match_time", now_iso)
               .order("match_time", desc=False)
               .execute())
        signals_to_settle = res.data or []
    except Exception as e:
        log.error("Supabase query failed: %s", e)
        return
    
    if not signals_to_settle:
        log.info("Aucun signal à settler (tous settlés ou en futur)")
        return
    
    log.info("📋 %d signaux à vérifier...", len(signals_to_settle))
    
    settled_count = 0
    settled_list = []
    
    # ── 2. Pour chaque signal : scraper résultat ──
    for sig in signals_to_settle:
        sig_id = sig.get("id")
        match_name = sig.get("match", "?")
        sport = sig.get("sport", "soccer")
        xbet_odd = sig.get("xbet_odd", 0)
        sharp_prob = sig.get("sharp_prob", 0.5)
        market = sig.get("market", "")
        
        log.info("Settling: %s [%s] @ %.2f", match_name, sport, xbet_odd)
        
        # Scraper le résultat
        result_tuple = _fetch_result(match_name, sport)
        if not result_tuple:
            log.warning("⚠️ Impossible de trouver le score: %s — SKIP", match_name)
            continue
        
        home_score, away_score = result_tuple
        
        # ── 3. Déterminer WIN/LOSS selon le marché ──
        actual_result = _determine_result(
            market=market,
            home_score=home_score,
            away_score=away_score,
            sport=sport,
        )
        
        if actual_result is None:
            log.warning("⚠️ Impossible de déterminer WIN/LOSS: %s — SKIP", match_name)
            continue
        
        # ── 4. Calculer CLV + Profit ──
        clv_final = _calculate_clv(xbet_odd, actual_result, sharp_prob)
        profit_units = _calculate_profit_units(xbet_odd, actual_result)
        was_clv_positive = clv_final > 0
        
        result_str = "✅ WIN" if actual_result == 1 else "❌ LOSS"
        log.info("%s: CLV=%.4f | Profit=%.2f€", result_str, clv_final, profit_units)
        
        # ── 5. Insérer dans ai_learning_ledger ──
        try:
            ledger_entry = {
                "signal_id": sig_id,
                "sport": sport,
                "league": sig.get("league", ""),
                "market_type": market,
                "time_to_match_minutes": int(
                    (datetime.fromisoformat(sig.get("match_time", now_iso).replace("Z", "+00:00")) - now).total_seconds() / 60)
                    if sig.get("match_time") else 0,
                "initial_edge": sig.get("edge_pct", 0),
                "sharp_divergence_std": sig.get("sharp_divergence_std"),
                "ai_confidence_score": sig.get("consensus_score"),
                "news_sentiment_score": None,  # Pas disponible
                "clv_final": clv_final,
                "actual_result": actual_result,
                "profit_units": profit_units,
                "was_clv_positive": was_clv_positive,
                "bookmaker_adjustment_speed_seconds": None,
                "created_at": now.isoformat(),
            }
            
            sb.table("ai_learning_ledger").insert(ledger_entry).execute()
            log.info("✓ Ledger inserted: signal_id=%s", sig_id)
            settled_list.append({
                "signal_id": sig_id,
                "clv": clv_final,
                "profit": profit_units,
                "result": result_str,
            })
            settled_count += 1
        except Exception as e:
            log.error("Ledger insert failed [signal %s]: %s", sig_id, str(e)[:80])
    
    # ── 6. Résumé ──
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("Settlement complet: %d/%d settlés", settled_count, len(signals_to_settle))
    
    if settled_list:
        wins = sum(1 for s in settled_list if "WIN" in s["result"])
        total_clv = sum(s["clv"] for s in settled_list)
        total_profit = sum(s["profit"] for s in settled_list)
        log.info("Results: %d wins | CLV total=%.4f | Profit=%.2f€",
                wins, total_clv, total_profit)


def _determine_result(market: str, home_score: int, away_score: int, sport: str) -> Optional[int]:
    """
    Déterminer WIN/LOSS selon le marché et le score.
    
    Retourne : 1=WIN, 0=LOSS, None=impossible
    """
    try:
        # ── H2H markets (Moneyline / DNB) ──
        if "DNB" in market or "AH 0" in market:
            # Draw = LOSS (DNB = no bet on draw)
            if home_score == away_score:
                return 0
            # Favori déterminé par... qui ? On cherche dans le market label
            if "Home" in market or market.startswith("1"):
                return 1 if home_score > away_score else 0
            elif "Away" in market or market.startswith("2"):
                return 1 if away_score > home_score else 0
            return None
        
        # ── Totals (Over/Under) ──
        if "Over" in market or "Under" in market:
            # Parser la ligne du total (ex: "Over 2.5")
            match_obj = re.search(r"(Over|Under)\s+([\d.]+)", market)
            if not match_obj:
                return None
            
            is_over = match_obj.group(1) == "Over"
            total_line = float(match_obj.group(2))
            total_goals = home_score + away_score
            
            if is_over:
                return 1 if total_goals > total_line else 0
            else:
                return 1 if total_goals < total_line else 0
        
        # ── Spreads (Handicap) ──
        if "Spread" in market or "Handicap" in market:
            match_obj = re.search(r"(Home|Away)\s*([-+]?[\d.]+)", market)
            if not match_obj:
                return None
            
            team = match_obj.group(1)
            spread = float(match_obj.group(2))
            
            if team == "Home":
                adjusted = home_score + spread
                return 1 if adjusted > away_score else 0
            else:
                adjusted = away_score + spread
                return 1 if adjusted > home_score else 0
        
        log.warning("⚠️ Unknown market type: %s", market)
        return None
    
    except Exception as e:
        log.error("determine_result error [%s]: %s", market, str(e)[:80])
        return None


def run():
    """Entry point du Settlement Engine."""
    now = datetime.now(timezone.utc)
    
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        _settle_signals(sb, now)
    except Exception as e:
        log.error("Settlement FAILED: %s", e)


if __name__ == "__main__":
    run()
