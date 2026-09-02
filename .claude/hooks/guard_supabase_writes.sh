#!/usr/bin/env bash
# PreToolUse mcp__supabase__.* — garde la règle dure n°9 : les lignes de
# résultats s'ARCHIVENT, elles ne se suppriment jamais (seule trace
# empirique ; les détruire crée un biais de survie). Le serveur MCP est déjà
# --read-only (.mcp.json) : ce hook est la ceinture, le flag les bretelles.
#   DELETE / DROP / TRUNCATE / ALTER TABLE … DROP  → deny
#   INSERT / UPDATE                                → ask (écriture assumée)
set -uo pipefail

input=$(cat)
# Tous les champs texte de tool_input, quel que soit l'outil MCP appelé.
texte=$(echo "$input" | jq -r '[.tool_input // {} | .. | strings] | join("\n")')
[ -z "$texte" ] && exit 0

# Hors commentaires SQL : on retire `-- …` et les blocs /* … */ mono-ligne.
sql=$(echo "$texte" | sed -e 's/--.*$//' -e 's|/\*[^*]*\*/||g')

if echo "$sql" | grep -qiE '\bDELETE\b|\bDROP\b|\bTRUNCATE\b|ALTER[[:space:]]+TABLE.*DROP'; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "Règle dure n°9 : archiver, JAMAIS supprimer des lignes de résultats (seule trace empirique — biais de survie sinon). DELETE/DROP/TRUNCATE refusés par principe ; une suppression légitime passe par une migration sql/migrate_vX_Y.sql avec bloc RESTAURATION, appliquée par l’opérateur."}}'
  exit 0
fi

if echo "$sql" | grep -qiE '\bINSERT\b|\bUPDATE\b'; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse",
    permissionDecision: "ask",
    permissionDecisionReason: "Écriture Supabase demandée via MCP. Les écritures normales passent par le pipeline ou scripts/ops.py sous contrôle opérateur — confirmer que celle-ci est voulue."}}'
fi
exit 0
