#!/usr/bin/env bash
# PreToolUse Edit|Write sur *.py — garde la règle dure n°3 : aucun nom de
# modèle IA en dur hors de core/ai_router.py (le paysage gratuit churne
# chaque mois ; une copie du nom finit par pointer un modèle mort en
# silence — récit dans INCIDENTS.md, « Couche IA »).
set -uo pipefail

input=$(cat)
file=$(echo "$input" | jq -r '.tool_input.file_path // empty')
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
case "$file" in
  *core/ai_router.py) exit 0 ;;   # le registre est LE lieu des noms de modèles
  *tests/*) exit 0 ;;             # un test a le droit de nommer pour vérifier
esac

texte=$(echo "$input" | jq -r '(.tool_input.new_string // .tool_input.content) // empty')
[ -z "$texte" ] && exit 0

if echo "$texte" | grep -qiE 'llama-3|llama-4|mixtral|mistral-(small|medium|large)|gemini-|gpt-|claude-|qwen|deepseek|gemma'; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "Règle dure n°3 : aucun nom de modèle IA en dur hors de core/ai_router.py. Le registre est la seule source (les listes se dérivent, tests/test_ai_router.py::TestAucunModeleEnDurHorsDuRegistre le garde). Voir INCIDENTS.md, « Couche IA »."}}'
fi
exit 0
