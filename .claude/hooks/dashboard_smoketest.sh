#!/usr/bin/env bash
# Auto smoke-test for the PREDATOR dashboard after edits to api/index.py or
# templates/*.html — runs the same checks as the predator-dashboard-check
# skill, but deterministically (no model tokens) and in the background.
set -uo pipefail

input=$(cat)
file=$(echo "$input" | jq -r '.tool_input.file_path // empty')
[ -z "$file" ] && exit 0

case "$file" in
  *api/index.py) ;;
  *templates/*.html) ;;
  *) exit 0 ;;
esac

cd /workspaces/predator || exit 0
pkill -f "port=5099" 2>/dev/null
LOG=/tmp/predator_dashboard_hook.log
nohup python3 -c "
from api.index import app
app.run(host='127.0.0.1', port=5099)
" > "$LOG" 2>&1 &
sleep 2

fail=""
if grep -qE "Traceback|SyntaxError" "$LOG"; then
  fail="échec au démarrage (voir $LOG)"
fi
if [ -z "$fail" ]; then
  for p in / /ledger /audit /performance; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:5099$p" 2>/dev/null || echo "000")
    if [ "$code" != "200" ]; then
      fail="$p -> HTTP $code"
      break
    fi
  done
fi

pkill -f "port=5099" 2>/dev/null

if [ -n "$fail" ]; then
  printf '{"systemMessage": "\xe2\x9a\xa0\xef\xb8\x8f Dashboard smoke test FAILED apr\xc3\xa8s modification de %s : %s"}\n' "$file" "$fail"
else
  printf '{"systemMessage": "\xe2\x9c\x85 Dashboard smoke test OK apr\xc3\xa8s modification de %s (/, /ledger, /audit, /performance -> 200)"}\n' "$file"
fi
