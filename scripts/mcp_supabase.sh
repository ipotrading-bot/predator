#!/usr/bin/env bash
# Lance le serveur MCP Supabase en LECTURE SEULE, avec le jeton pris dans .env.
#
# POURQUOI CE PONT EXISTE (2026-09-20)
# `.mcp.json` est VERSIONNÉ : y écrire SUPABASE_ACCESS_TOKEN committerait un
# secret (`.gitignore` ne couvre que `.env` exact). Et sa forme
# `${SUPABASE_ACCESS_TOKEN}` n'est résolue que depuis l'environnement du
# SHELL qui lance Claude Code — où rien n'est exporté, le dépôt gardant tout
# dans `.env`. Résultat : le serveur MCP démarrait sans jeton et répondait
# « Unauthorized » même avec un jeton valide dans `.env`.
#
# Ce script est le seul endroit qui fait le pont. Il ne COPIE le jeton nulle
# part : il le charge en mémoire le temps d'un exec, ce qui respecte la règle
# de `.claude/hooks/guard_bash.sh` (les credentials ne se recopient pas).
#
# Le cwd d'un serveur MCP déclaré dans un `.mcp.json` de projet est la racine
# du projet ; on ne s'y fie pas, le chemin est dérivé de $0 (jamais
# /workspaces/predator en dur — voir la mémoire « local vert n'est pas CI
# verte »).
set -euo pipefail

racine="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fichier_credentials="$racine/.env"

if [ -f "$fichier_credentials" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$fichier_credentials"
  set +a
fi

if [ -z "${SUPABASE_ACCESS_TOKEN:-}" ]; then
  echo "mcp_supabase.sh : SUPABASE_ACCESS_TOKEN absent (ni dans l'environnement," \
       "ni dans le fichier de credentials) — le serveur répondrait Unauthorized." >&2
  exit 1
fi

exec npx -y @supabase/mcp-server-supabase@0.11.0 "$@"
