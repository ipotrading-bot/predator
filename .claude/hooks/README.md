# Hooks Claude Code — les règles dures, en code

Ces scripts rendent DÉTERMINISTES les règles de `CLAUDE.md` qui n'étaient
que de la prose : un hook s'exécute quoi que le modèle décide. Chacun est
branché dans `.claude/settings.json` (source « Project Settings », visible
par `/hooks`) et gardé par `tests/test_claude_config.py` — cas bloquant ET
cas neutre, via subprocess.

## Contrat commun

- `#!/usr/bin/env bash`, `set -uo pipefail`, stdin lu par `input=$(cat)`,
  parsing `jq`, chemins via `"$CLAUDE_PROJECT_DIR"` — jamais de chemin
  absolu machine.
- **Exit 0 silencieux** quand le hook ne s'applique pas ; stdout = uniquement
  du JSON valide (`hookSpecificOutput`) quand il décide.
- Ne pas mettre d'apostrophe ASCII dans un programme jq mono-quoté : passer
  les textes par `--arg` (le premier bug de cette série).

## Tableau événement → script → règle gardée

| Script | Événement / filtre | Règle dure | Comportement |
|---|---|---|---|
| `guard_workflows.sh` | PreToolUse `Edit\|Write`, `if: Edit/Write(.github/**)` | n°1, n°2 | `toJSON(secrets)` → **deny** ; bloc `env:` touché → rappel `ci_env.py --write` (additionalContext, pas de refus) |
| `guard_ai_models.sh` | PreToolUse `Edit\|Write`, `if: Edit/Write(**/*.py)` | n°3 | nom de modèle IA hors `core/ai_router.py` et hors `tests/` → **deny** |
| `guard_operator_decisions.sh` | PreToolUse `Edit\|Write`, `if: Edit/Write(core/constants.py)` | n°11 | diff touchant `TAX_RATE`/`SHADOW_SPORTS` → **ask** (confirmer l'instruction opérateur) |
| `guard_supabase_writes.sh` | PreToolUse `mcp__supabase__.*` | n°9 | DELETE/DROP/TRUNCATE/ALTER…DROP (hors commentaires SQL) → **deny** ; INSERT/UPDATE → **ask**. Ceinture — `.mcp.json --read-only` est les bretelles |
| `guard_bash.sh` | PreToolUse `Bash` | n°9, n°5 + hygiène | deny : `rm -rf` hors /tmp, `git push --force/-f`, `git reset --hard`, `curl \| sh`, `.env` en token, `supabase db reset`, `vercel --prod` hors CI. Complète `permissions.deny` (préfixes) en attrapant les formes ENFOUIES |
| `guard_migration_writes.sh` | PreToolUse `Write` (frontmatter de l'agent `migration-author`) | conv. migrations | Write hors `sql/migrate_v*.sql` → **deny** (l'agent n'écrit que des migrations) |
| `lint_on_edit.sh` | PostToolUse `Edit\|Write`, `if: Edit/Write(**/*.py)`, async+rewake, 120 s | lint permanent | pyflakes sur le fichier → erreurs = **exit 2** (stderr) ; puis `tests/test_<basename>.py` s'il existe → résumé en `systemMessage`. `LINT_ON_EDIT_SKIP_TESTS=1` saute pytest |
| `verify_before_stop.sh` | Stop, 180 s | suite à 0 échec, n°5 | un `.py` modifié → `pytest tests/ -q` ; rouge = **exit 2** (30 dernières lignes en stderr, l'arrêt est bloqué) ; fichier de déploiement touché → check-list règle n°5 rappelée, jamais exécutée (credentials opérateur). `stop_hook_active` → sortie immédiate, pas de boucle |
| `session_context.sh` | SessionStart `startup\|resume`, 15 s | contexte | branche, `git log -5`, `gh run list --limit 5` (si gh authentifié), rappel INCIDENTS.md |
| `dashboard_smoketest.sh` | PostToolUse `Edit\|Write` (api/index.py, templates), async, 30 s | garde dashboard | démarre Flask sur :5099, vérifie /, /ledger, /audit, /performance → `systemMessage` |

Pourquoi le blocage du Stop passe par **exit 2 + stderr** et non par un JSON
`decision` : c'est la seule forme documentée comme NON contournable et stable
entre versions de Claude Code (« Blocking error. Stops the action regardless
of JSON output »).

## Tester un script à la main

Chaque hook se pipe : un JSON d'exemple sur stdin, la décision sur stdout,
le code de sortie derrière. ⚠️ Depuis une session Claude, lancer ces exemples
via un FICHIER script : la commande qui contiendrait `git push --force` en
littéral serait elle-même refusée par `guard_bash` (vécu).

```bash
export CLAUDE_PROJECT_DIR=$(git rev-parse --show-toplevel)

# Cas bloquant / cas neutre — guard_workflows
echo '{"tool_input":{"file_path":".github/workflows/scan.yml","new_string":"env: ${{ toJSON(secrets) }}"}}' \
  | bash .claude/hooks/guard_workflows.sh          # → permissionDecision: deny
echo '{"tool_input":{"file_path":".github/workflows/scan.yml","new_string":"timeout-minutes: 30"}}' \
  | bash .claude/hooks/guard_workflows.sh; echo $?  # → rien, 0

# guard_ai_models
echo '{"tool_input":{"file_path":"core/settlement.py","new_string":"m = \"llama-3.3\""}}' \
  | bash .claude/hooks/guard_ai_models.sh           # → deny
echo '{"tool_input":{"file_path":"core/ai_router.py","new_string":"m = \"gemini-2.0\""}}' \
  | bash .claude/hooks/guard_ai_models.sh; echo $?  # → rien, 0

# guard_operator_decisions
echo '{"tool_input":{"file_path":"core/constants.py","old_string":"TAX_RATE = 0.0","new_string":"TAX_RATE = 0.2"}}' \
  | bash .claude/hooks/guard_operator_decisions.sh  # → ask

# guard_supabase_writes
echo '{"tool_name":"mcp__supabase__execute_sql","tool_input":{"query":"DELETE FROM signals"}}' \
  | bash .claude/hooks/guard_supabase_writes.sh     # → deny
echo '{"tool_name":"mcp__supabase__execute_sql","tool_input":{"query":"SELECT 1"}}' \
  | bash .claude/hooks/guard_supabase_writes.sh; echo $?  # → rien, 0

# guard_bash (échantillon)
echo '{"tool_input":{"command":"git reset --hard HEAD~1"}}' \
  | bash .claude/hooks/guard_bash.sh                # → deny
echo '{"tool_input":{"command":"git status"}}' \
  | bash .claude/hooks/guard_bash.sh; echo $?       # → rien, 0

# lint_on_edit
printf 'import os\n' > /tmp/casse.py
echo '{"tool_input":{"file_path":"/tmp/casse.py"}}' \
  | LINT_ON_EDIT_SKIP_TESTS=1 bash .claude/hooks/lint_on_edit.sh; echo $?  # → stderr + 2

# verify_before_stop (garde anti-boucle)
echo '{"stop_hook_active":true}' \
  | bash .claude/hooks/verify_before_stop.sh; echo $?  # → rien, 0

# session_context
echo '{"session_start_reason":"startup"}' | bash .claude/hooks/session_context.sh
```

La même batterie, exécutée par pytest : `python -m pytest tests/test_claude_config.py -q`.
