"""
api/scan.py — Vercel Serverless Function : POST /api/scan
Déclenchée par GitHub Actions (cron) ou le bouton FORCE SCAN du dashboard.
Timeout Vercel : 60s (configuré dans vercel.json)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler

# ── Sécurité : vérification du secret partagé ─────────────────────
PREDATOR_SECRET = os.environ.get("PREDATOR_SECRET", "")


def _verify_secret(provided: str) -> bool:
    """HMAC-safe comparison pour éviter les timing attacks."""
    if not PREDATOR_SECRET:
        return True  # Dev mode sans secret
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).hexdigest(),
        hashlib.sha256(PREDATOR_SECRET.encode()).hexdigest(),
    )


class handler(BaseHTTPRequestHandler):
    """Handler Vercel serverless — supporte GET (health) et POST (scan)."""

    def log_message(self, format, *args):
        pass  # Supprime les logs HTTP verbeux de BaseHTTPRequestHandler

    # ── OPTIONS (CORS preflight) ───────────────────────────────
    def do_OPTIONS(self):
        self._send_cors()
        self.send_response(204)
        self.end_headers()

    # ── GET (health check) ─────────────────────────────────────
    def do_GET(self):
        self._send_cors()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "online",
            "service": "PREDATOR PAIM v2.0",
            "timestamp": int(time.time()),
        }).encode())

    # ── POST (scan trigger) ────────────────────────────────────
    def do_POST(self):
        self._send_cors()

        # Auth
        secret = self.headers.get("X-Predator-Secret", "")
        if not _verify_secret(secret):
            self._json_response({"error": "Unauthorized"}, 401)
            return

        # Parse body
        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                pass

        session = body.get("session", "api")

        # Run async scan in sync context
        result = asyncio.run(self._execute_scan(session))
        self._json_response(result, 200 if result.get("success") else 500)

    # ── Exécution du scan ──────────────────────────────────────
    @staticmethod
    async def _execute_scan(session: str) -> dict:
        """
        Importe et exécute le scanner PAIM.
        Import retardé pour limiter le cold start Vercel.
        """
        start = time.monotonic()
        try:
            # Import tardif (cold start optimisation)
            from config import settings
            from signals.scanner import MarketScanner

            scanner = MarketScanner(bankroll=settings.starting_bankroll)
            scan_result = await scanner.run_scan()

            return {
                "success": True,
                "session": session,
                "events_analyzed": scan_result.events_analyzed,
                "signals_validated": scan_result.signals_validated,
                "signals_rejected": scan_result.signals_rejected,
                "duration_seconds": round(time.monotonic() - start, 2),
                "timestamp": int(time.time()),
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "session": session,
                "duration_seconds": round(time.monotonic() - start, 2),
                "timestamp": int(time.time()),
            }

    # ── Helpers ───────────────────────────────────────────────
    def _json_response(self, data: dict, status: int = 200) -> None:
        self._send_cors()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Predator-Secret")
