#!/usr/bin/env bash
# SessionStart (startup|resume) — texte factuel injecté en contexte : où en
# est la branche, ce que la CI vient de faire, et le rappel qui évite les
# re-diagnostics à froid. Sortie = texte brut (exit 0 → contexte).
set -uo pipefail

input=$(cat)  # consommé, non utilisé

if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || true
fi

echo "État du dépôt PREDATOR au démarrage de session :"
branche=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "inconnue")
echo "- branche courante : $branche"
echo "- cinq derniers commits :"
git log -5 --oneline 2>/dev/null | sed 's/^/    /' || true

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  echo "- cinq derniers runs GitHub Actions :"
  gh run list --limit 5 2>/dev/null | sed 's/^/    /' || true
fi

echo "INCIDENTS.md est à lire avant tout diagnostic."
exit 0
