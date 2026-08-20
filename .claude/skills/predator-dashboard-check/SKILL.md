---
name: predator-dashboard-check
description: Spin up the PREDATOR Flask dashboard locally with zero credentials and smoke-test every route, the nav bar, and static assets. Use this after ANY change to api/index.py or templates/*.html, before telling the user a dashboard fix works — the test suite never renders templates nor hits Flask routes, so a local `python api/index.py` run is the only way to catch a template render error, a 404'd route, or a broken nav link before it ships to Vercel.
---

# PREDATOR dashboard smoke test

No Supabase credentials are needed for this — every route degrades gracefully to
an empty-state render when `_db()` returns `None` (see `api/index.py`), so this
only proves "does it render / route at all", not "is the data correct". That's
still worth checking every time, because template errors (undefined Jinja vars,
broken `{% %}` blocks) throw 500s even with no data.

## Steps

1. Confirm no stray instance is already bound to the port you're about to use:
   `ps aux | grep "app.run" | grep -v grep`.
2. Start the app in-process (don't rely on `python api/index.py` alone unless
   you've confirmed the `if __name__ == "__main__": app.run(...)` entrypoint is
   still present — it has gone missing before):
   ```bash
   nohup python3 -c "
   from api.index import app
   app.run(host='127.0.0.1', port=5099)
   " > /tmp/predator_smoketest.log 2>&1 &
   sleep 2
   cat /tmp/predator_smoketest.log   # check for import/syntax errors first
   ```
3. Hit every route and confirm status codes — anything other than 200 (or an
   expected 503/404 for unwired routes) needs investigation:
   ```bash
   for p in / /ledger /audit /performance /manifest.json /favicon.ico /api/health /api/signals; do
     code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:5099$p")
     echo "$p -> $code"
   done
   ```
4. Check the rendered HTML for leftover Jinja syntax or stack traces (a sign a
   template variable was referenced that the route never populates):
   ```bash
   curl -s http://127.0.0.1:5099/ | grep -n "{{\|{%\|Traceback\|Internal Server Error"
   ```
5. Nav-bar parity check — every page's `<div class="nav-pages">` block should list
   the same links (ACCUEIL/BILAN/AUDIT/PERF) unless a page is intentionally
   excluded. This has drifted silently before (some pages missing BILAN/AUDIT
   entirely):
   ```bash
   for f in index ledger audit performance; do
     echo "=== $f.html ==="
     awk '/<div class="nav-pages">/,/<\/div>/' templates/$f.html
   done
   ```
6. Local asset check — confirm every `src="/..."` / `href="/..."` on each page
   actually resolves:
   ```bash
   for p in / /ledger /audit /performance; do
     curl -s "http://127.0.0.1:5099$p" | grep -oP '(?<=src=")[^"]+|(?<=href=")[^"]+' \
       | grep -E "^/" | sort -u | while read -r path; do
         code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:5099${path%%#*}")
         [ "$code" != "200" ] && echo "  BROKEN ($code): $path"
       done
   done
   ```
7. Always clean up the background server before finishing:
   ```bash
   pkill -f "port=5099" 2>/dev/null
   ps aux | grep "port=5099" | grep -v grep || echo "clean"
   ```

If you need to see real data-driven rendering (not just empty-state), you'd need
real `SUPABASE_URL`/`SUPABASE_KEY` — see the `predator-pipeline` skill for why
those are usually not available in a fresh sandbox, and don't fabricate having
tested a data-populated view if you only tested the empty state.
