---
paths:
  - "sql/**"
---

# Règles — migrations Supabase

- **Aucun runner de migration.** Chaque changement de schéma = un NOUVEAU
  `sql/migrate_vX_Y.sql` (numéro suivant), collé À LA MAIN par l'opérateur
  dans le SQL Editor Supabase. Ne jamais éditer une migration passée, ne
  jamais supposer qu'un fichier présent est appliqué : vérifier en base.
- ⛔ **Règle dure n°9** : les lignes de résultats s'ARCHIVENT
  (`..._archive` + bloc RESTAURATION, modèle `migrate_v10_5`), elles ne se
  suppriment JAMAIS — seule trace empirique, les détruire crée un biais de
  survie. Le hook `guard_supabase_writes.sh` refuse DELETE/DROP/TRUNCATE
  via MCP ; une suppression légitime passe par une migration opérateur.
- RLS : `migrate_v9_3_tighten_rls.sql` est la référence — écriture réservée
  à `service_role`, et `meta` a déjà été oublié une fois (v10_9).
- Colonnes : chaque nom se vérifie contre `core/db.py` et `AUDIT.md` §2
  (règle n°6 — les listes qui divergent).
- Procédure complète : skill `predator-migration` (rédaction déléguée au
  sub-agent `migration-author`, Write borné à `sql/migrate_v*.sql`).
