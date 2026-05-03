"""
api/signals.py — GET /api/signals
Retourne les signaux du cycle actuel pour la Page 2 (Live Signals).
"""
from __future__ import annotations

import json
import os
import time
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

            # Paramètre de filtre depuis query string
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            limit = int(params.get("limit", ["9"])[0])
            status = params.get("status", ["pending"])[0]

            resp = (
                client.table("signals")
                .select(
                    "id,event_name,sport,match_time,market_key,selection,"
                    "bookmaker_target,ev_plus,snr_ratio,sharp_prob,"
                    "implied_prob_soft,recommended_stake,clv_estimate,"
                    "ai_context,status,outcome,profit_eur,created_at"
                )
                .eq("status", status)
                .order("ev_plus", desc=True)
                .limit(limit)
                .execute()
            )

            self._json({"signals": resp.data, "count": len(resp.data)})
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
