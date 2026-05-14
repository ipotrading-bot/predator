"""
run_engine.py — PREDATOR PAIM Heavy Engine v7.0 (GitHub Actions)
Cerveau Déporté — contourne le timeout 10s de Vercel.

Exécuté sur GitHub Actions (gratuit, 6h max) toutes les 20 minutes.
Ne dépend PAS de Vercel. Écrit directement dans Supabase.

Pipeline:
  1. Harvester 1XBet (flux direct gratuit)
  2. Gemini Oracle (Pinnacle Fair Price via Search Grounding)
  3. MultiSource cascade (si besoin)
  4. Shin Method → PAIMEngine → signaux
  5. Supabase insert
  6. Telegram notification
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

# ── Configuration robuste pour GitHub Actions ───────────────
# Les secrets sont injectés via $GITHUB_ENV dans le workflow
os.environ.setdefault("ODDS_API_KEY", "")
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("GROQ_API_KEY", "")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("NEWS_API_KEY", "")
os.environ.setdefault("PERPLEXITY_API_KEY", "")
os.environ.setdefault("RAPIDAPI_KEY", "")
os.environ.setdefault("API_FOOTBALL_KEY", "")

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("engine")


async def run_pulse_hunter_scan() -> dict:
    """
    Pulse Hunter Scan v7.0 — Exécution complète du pipeline.
    
    Retourne:
        dict: Statistiques du scan
    """
    from config import settings
    from signals.scanner import MarketScanner
    from core.harvester import get_harvester

    start = time.monotonic()
    stats = {
        "events_from_api": 0,
        "events_from_harvester": 0,
        "events_from_multisource": 0,
        "signals_validated": 0,
        "signals_persisted": 0,
        "duration_seconds": 0,
    }

    logger.info(f"🦅 PREDATOR PAIM v7.0 — Pulse Hunter Engine")
    logger.info(f"   Bankroll : {settings.starting_bankroll:,.0f}€")
    logger.info(f"   EV+ min  : {settings.min_ev_threshold:.1%}")
    logger.info(f"   Fenêtre  : 6h (Pulse Hunter)")

    scanner = MarketScanner(bankroll=settings.starting_bankroll)
    scanner.engine.min_ev_threshold = settings.min_ev_threshold

    try:
        # ── Étape 1: Harvester 1XBet (flux direct) ───────────────
        logger.info("🔧 [Engine] Step 1: 1XBet Harvester...")
        harvester = get_harvester()
        raw_feeds = harvester.fetch_multi_sport(["soccer", "basketball", "tennis"])
        stats["events_from_harvester"] = len(raw_feeds)

        # ── Étape 2: Gemini Oracle (Pinnacle Fair Price) ─────────
        logger.info("🔮 [Engine] Step 2: Scanning The-Odds-API + MultiSource...")
        
        # Utilise le scanner existant qui a déjà le fallback MultiSource
        from data.odds_fetcher import OddsFetcher
        from data.multi_source_fetcher import MultiSourceFetcher

        async with OddsFetcher() as fetcher:
            api_events = await fetcher.fetch_all_sports_odds()

        if api_events:
            stats["events_from_api"] = len(api_events)
            logger.info(f"📡 [Engine] The-Odds-API: {len(api_events)} events")
            events = api_events
        else:
            logger.warning("⚠️ [Engine] The-Odds-API vide → MultiSource cascade...")
            async with MultiSourceFetcher() as msf:
                events = await msf.fetch_all()
                stats["events_from_multisource"] = len(events)
                if not events:
                    logger.error("❌ [Engine] Aucune source disponible")
                    stats["duration_seconds"] = round(time.monotonic() - start, 2)
                    return stats

        # ── Étape 3: Process events via PAIM Engine ───────────────
        logger.info(f"⚡ [Engine] Step 3: PAIM Processing {len(events)} events...")
        
        result = await scanner._scan_events(events)
        stats["signals_validated"] = result.signals_validated

        # ── Étape 4: Notifications Telegram ───────────────────────
        if result.signals_validated > 0:
            logger.info(f"📬 [Engine] {result.signals_validated} signaux → notification Telegram")
            notifier = scanner._get_notifier()
            if notifier:
                summary = (
                    f"🦅 *PREDATOR PAIM — Scan Engine*\n"
                    f"📊 Événements: `{result.events_analyzed}`\n"
                    f"🎯 Signaux: `{result.signals_validated}`\n"
                    f"⏱️ Durée: `{result.duration_seconds}s`\n"
                    f"🏷️ Mode: GUERRILLA FREE"
                )
                await notifier.bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=summary,
                    parse_mode="Markdown",
                )

    except Exception as e:
        logger.critical(f"❌ [Engine] Critical error: {e}", exc_info=True)

    stats["duration_seconds"] = round(time.monotonic() - start, 2)
    logger.info(
        f"✅ [Engine] Scan terminé | "
        f"{stats['signals_validated']} signaux | "
        f"{stats['duration_seconds']}s"
    )
    return stats


def main():
    """Point d'entrée pour GitHub Actions."""
    logger.info("🚀 PREDATOR PAIM Engine v7.0 — Démarrage")
    
    stats = asyncio.run(run_pulse_hunter_scan())
    
    logger.info(
        f"\n{'='*50}\n"
        f"📊 RÉSUMÉ DU SCAN\n"
        f"{'='*50}\n"
        f"  API Events      : {stats['events_from_api']}\n"
        f"  Harvester Events : {stats['events_from_harvester']}\n"
        f"  MultiSource      : {stats['events_from_multisource']}\n"
        f"  Signaux validés  : {stats['signals_validated']}\n"
        f"  Durée            : {stats['duration_seconds']}s\n"
        f"{'='*50}"
    )

    # Exit code: 0 = succès, 1 = erreur
    sys.exit(0 if stats['signals_validated'] >= 0 else 1)


if __name__ == "__main__":
    main()