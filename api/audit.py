"""
api/audit.py — GET /api/audit
Données analytics pour la Page 4 (Audit Quantitatif) :
CLV Index, Brier Score, Equity Curve, Performance mensuelle.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler

from supabase import create_client


class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self._cors(); self.send_response(204); self.end_headers()

    def do_GET(self):
        self._cors()
        try:
            client = create_client(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_KEY"],
            )

            # Performance globale (depuis la VIEW)
            perf = client.table("performance_summary").select("*").execute()

            # Equity curve (derniers 200 points)
            equity = (
                client.table("bankroll_snapshots")
                .select("timestamp,balance,roi,drawdown")
                .order("timestamp", desc=False)
                .limit(200)
                .execute()
            )

            # Performance mensuelle
            monthly = client.table("monthly_performance").select("*").execute()

            # Brier Score récent
            brier = (
                client.table("brier_scores")
                .select("brier_score,sample_size,computed_at")
                .order("computed_at", desc=True)
                .limit(1)
                .execute()
            )

            # Derniers scan logs
            scans = (
                client.table("scan_logs")
                .select("*")
                .order("scanned_at", desc=True)
                .limit(5)
                .execute()
            )

            self._json({
                "performance": perf.data[0] if perf.data else {},
                "equity_curve": equity.data,
                "monthly": monthly.data,
                "brier": brier.data[0] if brier.data else {},
                "scan_logs": scans.data,
            })
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _json(self, data: dict, status: int = 200):
        self._cors()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
