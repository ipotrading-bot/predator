"""
main.py — Point d'entrée principal : Scheduler + 3 scans / jour
Sessions: Asie (00:00) · Europe (08:00) · USA (16:00) UTC
"""
import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from signals.scanner import MarketScanner

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

# Singleton scanner
scanner = MarketScanner(bankroll=settings.starting_bankroll)


async def run_scan_job(session_name: str) -> None:
    """Job planifié pour chaque session de scan."""
    logger.info(f"🕐 Démarrage scan session: {session_name}")
    try:
        result = await scanner.run_scan()
        logger.info(
            f"✅ Scan {session_name} terminé | "
            f"{result.events_analyzed} événements | "
            f"{result.signals_validated} signaux | "
            f"{result.duration_seconds:.1f}s"
        )
    except Exception as e:
        logger.error(f"❌ Erreur scan {session_name}: {e}", exc_info=True)


async def reset_daily_job() -> None:
    """Reset du compteur journalier à minuit."""
    scanner.risk.reset_daily()
    logger.info("🔄 Reset journalier effectué.")


def build_scheduler() -> AsyncIOScheduler:
    """Configure le scheduler avec les 3 scans quotidiens."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        lambda: asyncio.create_task(run_scan_job("Asie")),
        CronTrigger(hour=0, minute=0),
        id="scan_asia",
        name="Scan Session Asie",
    )

    scheduler.add_job(
        lambda: asyncio.create_task(run_scan_job("Europe")),
        CronTrigger(hour=8, minute=0),
        id="scan_europe",
        name="Scan Session Europe",
    )

    scheduler.add_job(
        lambda: asyncio.create_task(run_scan_job("USA")),
        CronTrigger(hour=16, minute=0),
        id="scan_usa",
        name="Scan Session USA",
    )

    scheduler.add_job(
        lambda: asyncio.create_task(reset_daily_job()),
        CronTrigger(hour=23, minute=55),
        id="daily_reset",
        name="Reset Journalier",
    )

    return scheduler


async def main() -> None:
    logger.info("🦅 Predator PAIM démarrage...")
    logger.info(f"   Bankroll initiale : {settings.starting_bankroll:,.0f}€")
    logger.info(f"   EV+ minimum       : {settings.min_ev_threshold:.0%}")
    logger.info(f"   Kill-Switch       : {settings.max_drawdown_pct:.0%} drawdown")
    logger.info(f"   Sports cibles     : {len(settings.target_sports)}")

    scheduler = build_scheduler()
    scheduler.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig_name: str) -> None:
        logger.info(f"Signal {sig_name} reçu — arrêt gracieux...")
        scheduler.shutdown(wait=False)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _shutdown(s.name))

    logger.info("✅ Scheduler actif — 3 scans/jour planifiés.")
    logger.info("   Ctrl+C pour arrêter.")

    await stop_event.wait()
    logger.info("🛑 Predator PAIM arrêté.")


if __name__ == "__main__":
    asyncio.run(main())
