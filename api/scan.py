"""
api/scan.py — Endpoint FastAPI pour Vercel Serverless + Cron Jobs
GET /api/scan?session=Europe  →  déclenche un scan PAIM complet
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

# Config doit être chargé avant les imports PAIM
os.environ.setdefault("ODDS_API_KEY", os.getenv("ODDS_API_KEY", ""))
os.environ.setdefault("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", ""))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", ""))
os.environ.setdefault("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))
os.environ.setdefault("SUPABASE_URL", os.getenv("SUPABASE_URL", ""))
os.environ.setdefault("SUPABASE_KEY", os.getenv("SUPABASE_KEY", ""))

from config import settings
from signals.scanner import MarketScanner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.scan")

app = FastAPI(title="Predator PAIM API", version="1.0.0")

# Singleton scanner (réutilisé entre invocations chaudes)
_scanner: MarketScanner | None = None


def get_scanner() -> MarketScanner:
    global _scanner
    if _scanner is None:
        _scanner = MarketScanner(bankroll=settings.starting_bankroll)
    return _scanner


@app.get("/api/scan")
async def run_scan(session: str = Query(default="manual")) -> JSONResponse:
    """Déclenche un scan PAIM complet. Appelé par Vercel Cron."""
    logger.info(f"🕐 Scan déclenché — session: {session}")
    try:
        scanner = get_scanner()
        result = await scanner.run_scan()
        return JSONResponse({
            "status": "ok",
            "session": session,
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
