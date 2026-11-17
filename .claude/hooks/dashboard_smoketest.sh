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

# Chemin via $CLAUDE_PROJECT_DIR, jamais un chemin absolu machine.
[ -z "${CLAUDE_PROJECT_DIR:-}" ] && exit 0
cd "$CLAUDE_PROJECT_DIR" || exit 0
# Sans Flask dans l'interpréteur, le smoke test n'a rien à vérifier : sortie
# silencieuse (l'environnement n'est pas celui du dashboard).
python3 -c "import flask" 2>/dev/null || exit 0
pkill -f "port=5099" 2>/dev/null
LOG=/tmp/predator_dashboard_hook.log
# Depuis le 2026-11-17 le dashboard est derrière une PORTE (api/index.py,
# « LA PORTE ») : sans session, une page rend 303 vers /login. Le serveur de
# test reçoit donc un jeton d'admin JETABLE, propre à ce processus — il n'a
# rien de commun avec celui du déploiement — et les pages sont lues avec.
JETON="smoke-$$-$(date +%s)"
DASHBOARD_ADMIN_TOKEN="$JETON" nohup python3 -c "
from api.index import app
app.run(host='127.0.0.1', port=5099)
" > "$LOG" 2>&1 &
# Attente bornée du démarrage (un `sleep 2` fixe rendait « HTTP 000 » quand
# l'import prenait plus de deux secondes).
for _ in $(seq 1 20); do
  curl -s -o /dev/null "http://127.0.0.1:5099/api/health" 2>/dev/null && break
  sleep 0.5
done

fail=""
if grep -qE "Traceback|SyntaxError" "$LOG"; then
  fail="échec au démarrage (voir $LOG)"
fi
if [ -z "$fail" ]; then
  for p in / /ledger /audit /performance /login; do
    code=$(curl -s -o /dev/null -w "%{http_code}" -H "X-Predator-Token: $JETON" "http://127.0.0.1:5099$p" 2>/dev/null)
    if [ "$code" != "200" ]; then
      fail="$p -> HTTP ${code:-000}"
      break
    fi
  done
fi
# La porte elle-même : sans jeton ni session, une page doit REFUSER. Un 200
# ici voudrait dire que le dashboard est redevenu public.
if [ -z "$fail" ]; then
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:5099/" 2>/dev/null)
  [ "$code" != "303" ] && fail="porte OUVERTE : / sans session -> HTTP ${code:-000} (attendu 303)"
fi

pkill -f "port=5099" 2>/dev/null

if [ -n "$fail" ]; then
  printf '{"systemMessage": "\xe2\x9a\xa0\xef\xb8\x8f Dashboard smoke test FAILED apr\xc3\xa8s modification de %s : %s"}\n' "$file" "$fail"
else
  printf '{"systemMessage": "\xe2\x9c\x85 Dashboard smoke test OK apr\xc3\xa8s modification de %s (/, /ledger, /audit, /performance, /login -> 200 ; porte ferm\xc3\xa9e sans session)"}\n' "$file"
fi
