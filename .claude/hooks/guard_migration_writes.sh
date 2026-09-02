#!/usr/bin/env bash
# PreToolUse Write — câblé dans le frontmatter de l'agent `migration-author`
# UNIQUEMENT : cet agent n'écrit que des migrations. Tout Write hors
# `sql/migrate_v*.sql` est refusé — la promesse « Write restreint à sql/ »
# est ainsi mécanique, pas déclarative.
set -uo pipefail

input=$(cat)
file=$(echo "$input" | jq -r '.tool_input.file_path // empty')
[ -z "$file" ] && exit 0

case "$file" in
  *sql/migrate_v*.sql) exit 0 ;;
esac

jq -n --arg f "$file" '{hookSpecificOutput: {hookEventName: "PreToolUse",
  permissionDecision: "deny",
  permissionDecisionReason: ("L agent migration-author n écrit QUE des fichiers sql/migrate_v*.sql — refusé : " + $f + ". Pour tout autre fichier, revenir à la session principale.")}}'
exit 0
