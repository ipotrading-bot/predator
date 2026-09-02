#!/usr/bin/env bash
# PreToolUse Edit|Write sur .github/** — garde les règles dures n°1 et n°2.
# n°1 : ${{ toJSON(secrets) }} fait REFUSER le workflow par GitHub (zéro job,
#       aucun log) — récit dans INCIDENTS.md, « Les blocs de secrets ».
# n°2 : les blocs env: sont GÉNÉRÉS par scripts/ci_env.py --write — on ne
#       refuse pas l'édition, on rappelle la source de vérité.
set -uo pipefail

input=$(cat)
file=$(echo "$input" | jq -r '.tool_input.file_path // empty')
case "$file" in
  *.github/*) ;;
  *) exit 0 ;;
esac

texte=$(echo "$input" | jq -r '(.tool_input.new_string // .tool_input.content) // empty')
[ -z "$texte" ] && exit 0

if echo "$texte" | grep -q 'toJSON(secrets)'; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "Règle dure n°1 : JAMAIS ${{ toJSON(secrets) }} dans un workflow — GitHub refuse de le faire tourner (zéro job, aucun log). Voir INCIDENTS.md, « Les blocs de secrets »."}}'
  exit 0
fi

if echo "$texte" | grep -qE '(^|[[:space:]])env:'; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse",
    additionalContext: "Les blocs env: des workflows sont générés par `python scripts/ci_env.py --write` (règle dure n°2) ; ne pas les éditer à la main — modifier les pools dans scripts/ci_env.py puis régénérer."}}'
fi
exit 0
