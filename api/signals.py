from http.server import BaseHTTPRequestHandler
import json, os, time

def _get(k): return os.environ.get(k, "")

def _next_scan_ts() -> int:
    """Returns unix timestamp of next scan (00:00, 08:00, 16:00 UTC)."""
    now = time.gmtime()
    hours = [0, 8, 16]
    current_hour = now.tm_hour
    for h in hours:
        if h > current_hour:
            next_h = h
            break
    else:
        next_h = hours[0] + 24
    import calendar
    base = calendar.timegm(time.strptime(
        f"{now.tm_year}-{now.tm_mon:02d}-{now.tm_mday:02d} {next_h % 24:02d}:00:00",
        "%Y-%m-%d %H:%M:%S"
    ))
    if next_h >= 24:
        base += 86400
    return base


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = _get("SUPABASE_URL")
        key = _get("SUPABASE_KEY")
        signals, error = [], None

        if url and key:
            try:
                from supabase import create_client
                db = create_client(url, key)
                rows = db.table("signals").select("*").order("created_at", desc=True).limit(20).execute()
                signals = rows.data or []
            except Exception as e:
                error = str(e)

        body = json.dumps({
            "signals": signals,
            "next_scan_ts": _next_scan_ts(),
            "error": error,
        }, default=str).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
