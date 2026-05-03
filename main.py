"""
main.py — Point d'entrée unifié v2.0
Modes:
  python main.py               → Scheduler local APScheduler (dev)
  python main.py --once        → Scan unique (GitHub Actions)
  python main.py --session USA → Scan unique avec session nommée
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time

# ── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("predator_paim.log"),
    ],
)
logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Predator PAIM v2.0")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exécute un scan unique puis quitte (mode GitHub Actions)",
    )
    parser.add_argument(
        "--session",
        default="auto",
        help="Nom de session : asie | europe | usa | manual | auto",
    )
    return parser.parse_args()


def detect_session() -> str:
    """Détecte la session selon l'heure UTC."""
    hour = time.gmtime().tm_hour
    if hour < 4:
        return "Asie"
    elif hour < 12:
        return "Europe"
    else:
        return "USA"


async def run_once(session_name: str) -> int:
    """Mode GitHub Actions : un scan, puis exit code 0/1."""
    from config import settings
    from signals.scanner import MarketScanner

    logger.info(f"🦅 Predator PAIM v2.0 — Scan {session_name}")
    logger.info(f"   Bankroll  : {settings.starting_bankroll:,.0f}€")
    logger.info(f"   EV+ min   : {settings.min_ev_threshold:.0%}")
    logger.info(f"   Kill-SW   : {settings.max_drawdown_pct:.0%}")

    try:
        scanner = MarketScanner(bankroll=settings.starting_bankroll)
        result = await scanner.run_scan()

        logger.info(
            f"✅ Terminé | {result.events_analyzed} events | "
            f"{result.signals_validated} signaux | {result.duration_seconds:.1f}s"
        )
        # Persist scan log
        await _save_scan_log(result, session_name)
        return 0

    except Exception as e:
        logger.critical(f"❌ Scan échoué: {e}", exc_info=True)
        return 1


async def _save_scan_log(result, session_name: str) -> None:
    """Enregistre le journal de scan dans Supabase."""
    try:
        from data.supabase_client import SupabaseClient
        db = SupabaseClient()
        db._client.table("scan_logs").insert({
            "session_name": session_name,
            "events_analyzed": result.events_analyzed,
            "signals_found": result.signals_found,
            "signals_validated": result.signals_validated,
            "signals_rejected": result.signals_rejected,
            "duration_seconds": round(result.duration_seconds, 2),
        }).execute()
    except Exception as e:
        logger.warning(f"Scan log non sauvegardé: {e}")


async def run_scheduler() -> None:
    """Mode développement local avec APScheduler."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from config import settings
    from signals.scanner import MarketScanner

    scanner = MarketScanner(bankroll=settings.starting_bankroll)

    async def _job(session: str):
        try:
            await scanner.run_scan()
            await _save_scan_log(None, session)
        except Exception as e:
            logger.error(f"Erreur scan {session}: {e}", exc_info=True)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(lambda: asyncio.create_task(_job("Asie")),
                      CronTrigger(hour=0, minute=0))
    scheduler.add_job(lambda: asyncio.create_task(_job("Europe")),
                      CronTrigger(hour=8, minute=0))
    scheduler.add_job(lambda: asyncio.create_task(_job("USA")),
                      CronTrigger(hour=16, minute=0))
    scheduler.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info("✅ Scheduler actif — Ctrl+C pour arrêter.")
    await stop.wait()
    scheduler.shutdown(wait=False)


def main() -> None:
    args = parse_args()

    if args.once:
        session = args.session if args.session != "auto" else detect_session()
        exit_code = asyncio.run(run_once(session))
        sys.exit(exit_code)
    else:
        logger.info("🔄 Mode scheduler local activé")
        asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
