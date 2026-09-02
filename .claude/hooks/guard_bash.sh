#!/usr/bin/env bash
# PreToolUse Bash — refuse les commandes destructrices ou exfiltrantes.
# permissions.deny (settings.json) couvre les formes par PRÉFIXE
# (`cat .env*`, `git push --force *`, `rm -rf *`) ; ce hook attrape les mêmes
# gestes ENFOUIS dans une commande composée. Ceinture ET bretelles.
set -uo pipefail

input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0

deny() {
  jq -n --arg r "$1" '{hookSpecificOutput: {hookEventName: "PreToolUse",
    permissionDecision: "deny", permissionDecisionReason: $r}}'
  exit 0
}

# rm -rf (toute combinaison de flags portant r et f) hors /tmp.
if echo "$cmd" | grep -qE '(^|[;&|[:space:]])rm[[:space:]]+-[a-zA-Z]*([rR][a-zA-Z]*[fF]|[fF][a-zA-Z]*[rR])\b'; then
  ligne=$(echo "$cmd" | grep -E '(^|[;&|[:space:]])rm[[:space:]]+-' | head -1)
  reste=$(echo "$ligne" | sed -E 's/.*(^|[;&|[:space:]])rm[[:space:]]+//')
  for tok in $reste; do
    case "$tok" in
      -*) continue ;;                       # flags
      \;|\||\&\&|\&) break ;;              # fin de la commande rm
      /tmp|/tmp/*) continue ;;             # le scratch est la seule cible admise
      *) deny "rm -rf hors /tmp refusé (cible : $tok). Ce dépôt archive, il ne détruit pas (règle dure n°9) ; pour un vrai nettoyage, viser /tmp ou demander à l'opérateur." ;;
    esac
  done
fi

echo "$cmd" | grep -qE 'git[[:space:]]+push[[:space:]]+[^;|&]*(--force|-f([[:space:]]|$))' \
  && deny "git push --force refusé : l'historique distant est la trace du pipeline, il ne se réécrit pas."

echo "$cmd" | grep -qE 'git[[:space:]]+reset[[:space:]]+[^;|&]*--hard' \
  && deny "git reset --hard refusé : il détruit le travail non committé sans trace. Préférer git stash, ou un checkout ciblé."

echo "$cmd" | grep -qE '(curl|wget)[^;|&]*\|[[:space:]]*(sudo[[:space:]]+)?(ba)?sh([[:space:]]|$)' \
  && deny "curl … | sh refusé : ne jamais exécuter du code téléchargé sans le lire (même principe que l'épinglage des dépendances)."

echo "$cmd" | grep -qE '(^|[^A-Za-z0-9_])\.env([^A-Za-z0-9_.]|$)' \
  && deny "Commande touchant .env refusée : le fichier de credentials ne se lit ni ne se copie depuis une session (CLAUDE.md : credentials dans .env, gitignoré)."

echo "$cmd" | grep -qE 'supabase[[:space:]]+db[[:space:]]+reset' \
  && deny "supabase db reset refusé : il détruirait la base de production — les migrations s'appliquent À LA MAIN dans le SQL Editor (convention du dépôt)."

if [ -z "${GITHUB_ACTIONS:-}" ]; then
  echo "$cmd" | grep -qE 'vercel[[:space:]]+[^;|&]*--prod' \
    && deny "vercel --prod hors CI refusé : le déploiement passe par le job deploy de ci.yml, après suite verte (règle dure n°5)."
fi

exit 0
