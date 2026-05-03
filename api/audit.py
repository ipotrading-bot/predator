from http.server import BaseHTTPRequestHandler
import json, os

def _get(k): return os.environ.get(k, "")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = _get("SUPABASE_URL")
        key = _get("SUPABASE_KEY")
        result = {
            "total_bets": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "total_profit": 0,
            "clv_avg": 0, "brier_score": None,
            "equity_curve": [], "error": None,
        }

        if url and key:
            try:
                from supabase import create_client
                db = create_client(url, key)

                # Settled signals
                rows = db.table("signals").select("*").eq("status", "settled").execute().data or []
                total = len(rows)
                wins = sum(1 for r in rows if r.get("outcome") == 1)
                profit = sum(r.get("profit_eur") or 0 for r in rows)
                clv_vals = [r["clv_estimate"] for r in rows if r.get("clv_estimate")]
                clv_avg = sum(clv_vals) / len(clv_vals) if clv_vals else 0

                # Brier score
                brier_vals = [(r["sharp_prob"], r["outcome"]) for r in rows
                              if r.get("sharp_prob") is not None and r.get("outcome") is not None]
                brier = sum((p - o) ** 2 for p, o in brier_vals) / len(brier_vals) if brier_vals else None

                # Equity curve
                snaps = db.table("bankroll_snapshots").select("timestamp,balance").order("timestamp").execute().data or []

                result.update({
                    "total_bets": total,
                    "wins": wins,
                    "losses": total - wins,
                    "win_rate": round(wins / total, 4) if total else 0,
                    "total_profit": round(profit, 2),
                    "clv_avg": round(clv_avg, 4),
                    "brier_score": round(brier, 4) if brier is not None else None,
                    "equity_curve": snaps,
                })
            except Exception as e:
                result["error"] = str(e)

        body = json.dumps(result, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
