"""
api/scan.py — Endpoint FastAPI pour Vercel Serverless + Cron Jobs
GET /api/scan  →  déclenche un scan PAIM complet (Lead/Lag/Bayesian/Kelly)
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import settings
from signals.scanner import MarketScanner
from core.notifications import send_telegram_ticket

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.scan")

app = FastAPI(title="Predator PAIM API", version="1.0.0")

_scanner: MarketScanner | None = None


def get_scanner() -> MarketScanner:
    global _scanner
    if _scanner is None:
        _scanner = MarketScanner(bankroll=settings.starting_bankroll)
    return _scanner


@app.get("/api/scan")
async def run_scan() -> JSONResponse:
    """
    Déclenché par Vercel Cron (0 */8 * * *).
    Pipeline: OddsFetcher → PAIM Engine → Gemini → Système 7/9 → Telegram
    """
    logger.info("🕐 Scan PAIM démarré")
    try:
        scanner = get_scanner()
        result = await scanner.run_scan()

        if not result.signals:
            logger.info("ℹ️ Aucune anomalie EV+ détectée.")
            return JSONResponse({
                "status": "success",
                "message": "Aucune anomalie EV+ détectée.",
                "events_analyzed": result.events_analyzed,
            })

        # Envoi du ticket système 7/9 via Telegram
        await send_telegram_ticket(
            signals=result.signals,
            token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
        )

        return JSONResponse({
            "status": "success",
            "message": "Ticket d'Élite envoyé.",
            "events_analyzed": result.events_analyzed,
            "signals_found": result.signals_found,
            "signals_validated": result.signals_validated,
            "signals_rejected": result.signals_rejected,
            "duration_seconds": round(result.duration_seconds, 2),
        })

    except Exception as e:
        logger.error(f"❌ Erreur scan: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "1.0.0"})
