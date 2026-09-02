#!/usr/bin/env bash
# PreToolUse Edit|Write sur core/constants.py — garde la règle dure n°11 :
# TAX_RATE, SHADOW_SPORTS et le périmètre sportif sont des décisions
# OPÉRATEUR. Une session les a déjà « corrigés » contre instruction
# (TAX_RATE remis à 0.20 le 2026-08-27 → émission fermée 5 jours,
# INCIDENTS.md « TAX_RATE remis à 0.20 contre instruction »). On ne refuse
# pas : on force la question.
set -uo pipefail

input=$(cat)
file=$(echo "$input" | jq -r '.tool_input.file_path // empty')
case "$file" in
  *core/constants.py) ;;
  *) exit 0 ;;
esac

texte=$(echo "$input" | jq -r '((.tool_input.old_string // "") + "\n" + (.tool_input.new_string // "") + "\n" + (.tool_input.content // ""))')
if echo "$texte" | grep -qE 'TAX_RATE|SHADOW_SPORTS'; then
  raison="Décision opérateur (règle dure n°11) : TAX_RATE et SHADOW_SPORTS ne se modifient que sur instruction EXPLICITE donnée dans la session courante. Confirmer que cette instruction existe avant d'appliquer."
  jq -n --arg r "$raison" '{hookSpecificOutput: {hookEventName: "PreToolUse",
    permissionDecision: "ask", permissionDecisionReason: $r}}'
fi
exit 0
