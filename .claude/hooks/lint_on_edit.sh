#!/usr/bin/env bash
# PostToolUse Edit|Write sur **/*.py — pyflakes immédiat sur le fichier
# touché (erreurs → exit 2, Claude les voit et corrige tout de suite au lieu
# de les découvrir au commit), puis tests ciblés si tests/test_<basename>.py
# existe (résultat en systemMessage, jamais de log brut).
# LINT_ON_EDIT_SKIP_TESTS=1 saute la partie pytest (utilisé par les tests
# du hook lui-même, pour ne pas emboîter deux pytest).
set -uo pipefail

input=$(cat)
file=$(echo "$input" | jq -r '.tool_input.file_path // empty')
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

PY=python
command -v python >/dev/null 2>&1 || PY=python3
command -v "$PY" >/dev/null 2>&1 || exit 0
"$PY" -c "import pyflakes" 2>/dev/null || exit 0

lint=$("$PY" -m pyflakes "$file" 2>&1 || true)
if [ -n "$lint" ]; then
  echo "$lint" >&2
  exit 2
fi

[ "${LINT_ON_EDIT_SKIP_TESTS:-0}" = "1" ] && exit 0
[ -z "${CLAUDE_PROJECT_DIR:-}" ] && exit 0

base=$(basename "$file" .py)
cible="tests/test_${base}.py"
if [ -f "$CLAUDE_PROJECT_DIR/$cible" ]; then
  resume=$(cd "$CLAUDE_PROJECT_DIR" && "$PY" -m pytest "$cible" -q -x 2>&1 | tail -3)
  jq -n --arg m "Tests ciblés ($cible) après modification de $file : $resume" \
    '{systemMessage: $m}'
fi
exit 0
