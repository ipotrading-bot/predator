"""
main.py — Point d'entree principal : Scheduler + 3 scans / jour
Sessions: Asie (00:00) - Europe (08:00) - USA (16:00) UTC
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
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("predator_paim.log", encoding="utf-8"),
    ],
)
# Fix Windows console encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logger = logging.getLogger("main")

# Singleton scanner
scanner = MarketScanner(bankroll=settings.starting_bankroll)


async def run_scan_job(session_name: str) -> None:
    logger.info(f"[SCAN] Demarrage session: {session_name}")
    try:
        result = await scanner.run_scan()
        logger.info(
            f"[SCAN] {session_name} termine | "
            f"{result.events_analyzed} evenements | "
            f"{result.signals_validated} signaux | "
            f"{result.duration_seconds:.1f}s"
        )
    except Exception as e:
        logger.error(f"[SCAN] Erreur {session_name}: {e}", exc_info=True)


async def reset_daily_job() -> None:
    scanner.risk.reset_daily()
    logger.info("[RESET] Reset journalier effectue.")


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: asyncio.create_task(run_scan_job("Asie")),
        CronTrigger(hour=0, minute=0),
        id="scan_asia", name="Scan Session Asie",
    )
    scheduler.add_job(
        lambda: asyncio.create_task(run_scan_job("Europe")),
        CronTrigger(hour=8, minute=0),
        id="scan_europe", name="Scan Session Europe",
    )
    scheduler.add_job(
        lambda: asyncio.create_task(run_scan_job("USA")),
        CronTrigger(hour=16, minute=0),
        id="scan_usa", name="Scan Session USA",
    )
    scheduler.add_job(
        lambda: asyncio.create_task(reset_daily_job()),
        CronTrigger(hour=23, minute=55),
        id="daily_reset", name="Reset Journalier",
    )
    return scheduler


async def main() -> None:
    logger.info("Predator PAIM demarrage...")
    logger.info(f"  Bankroll : {settings.starting_bankroll:,.0f} EUR")
    logger.info(f"  EV+ min  : {settings.min_ev_threshold:.0%}")
    logger.info(f"  Kill-SW  : {settings.max_drawdown_pct:.0%} drawdown")
    logger.info(f"  Sports   : {len(settings.target_sports)}")

    scheduler = build_scheduler()
    scheduler.start()

    stop_event = asyncio.Event()

    def _shutdown() -> None:
        logger.info("Arret gracieux...")
        scheduler.shutdown(wait=False)
        stop_event.set()

    # Windows ne supporte pas loop.add_signal_handler
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: _shutdown())
        signal.signal(signal.SIGTERM, lambda *_: _shutdown())

    logger.info("Scheduler actif - 3 scans/jour planifies. Ctrl+C pour arreter.")

    # Force immediate scan on startup
    logger.info("[BOOT] Scan de demarrage force...")
    await run_scan_job("Boot")

    await stop_event.wait()
    logger.info("Predator PAIM arrete.")


if __name__ == "__main__":
    asyncio.run(main())
