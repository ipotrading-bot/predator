#!/usr/bin/env bash
# Stop — avant de rendre la main : si un .py a été modifié (suivi ou non),
# la suite complète doit être verte, sinon l'arrêt est BLOQUÉ (exit 2, les
# 30 dernières lignes en stderr). Si le diff touche un fichier de
# déploiement (vercel.json, .python-version, requirements*, api/index.py),
# la check-list de la règle dure n°5 est rappelée — PAS exécutée : elle
# demande des credentials opérateur (ops.py vercel, curl de production).
# `stop_hook_active` vrai → sortie immédiate, jamais de boucle.
set -uo pipefail

input=$(cat)
actif=$(echo "$input" | jq -r '.stop_hook_active // false')
[ "$actif" = "true" ] && exit 0

[ -z "${CLAUDE_PROJECT_DIR:-}" ] && exit 0
cd "$CLAUDE_PROJECT_DIR" || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

modifies=$( { git diff --name-only HEAD 2>/dev/null;
              git ls-files --others --exclude-standard 2>/dev/null; } | sort -u )
[ -z "$modifies" ] && exit 0

deploiement=$(echo "$modifies" | grep -E '^(vercel\.json|\.python-version|requirements[^/]*\.txt|api/index\.py)$' || true)
checklist="Règle dure n°5 : une suite verte ne prouve RIEN sur le déploiement. Après cette retouche de fichier(s) de déploiement ($(echo "$deploiement" | tr '\n' ' ')) : 1) python scripts/ops.py vercel deployments | head -3 → READY, pas ERROR ; 2) curl -s https://predator-two.vercel.app/api/health. Non exécutés par ce hook (credentials opérateur)."

PY=python
command -v python >/dev/null 2>&1 || PY=python3

if echo "$modifies" | grep -qE '\.py$' && [ -d tests ]; then
  if ! sortie=$("$PY" -m pytest tests/ -q 2>&1); then
    {
      echo "Suite de tests ROUGE — l'arrêt est bloqué tant qu'elle ne repasse pas (CLAUDE.md : 0 échec). Dernières lignes :"
      echo "$sortie" | tail -30
      [ -n "$deploiement" ] && echo "$checklist"
    } >&2
    exit 2
  fi
fi

if [ -n "$deploiement" ]; then
  jq -n --arg m "$checklist" '{systemMessage: $m}'
fi
exit 0
