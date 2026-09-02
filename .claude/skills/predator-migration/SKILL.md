---
name: predator-migration
description: Procédure complète d'un changement de schéma Supabase dans PREDATOR — nouveau fichier sql/migrate_vX_Y.sql, application MANUELLE, vérification. Use when a schema change is needed (new column/table, RLS, archive) or when a migration seems "not applied".
---

# Changement de schéma Supabase

## Les trois faits qui commandent tout

1. **Aucun runner de migration n'existe.** Un fichier `sql/migrate_vX_Y.sql`
   ne s'applique PAS tout seul : l'opérateur le colle dans le SQL Editor
   Supabase. Avant de supposer qu'une colonne existe, vérifier `sql/` ET la
   base (`python scripts/ops.py supabase sql "..."` ou le MCP read-only).
2. **Chaque changement = un NOUVEAU fichier** `sql/migrate_vX_Y.sql`
   (numéro suivant), jamais l'édition d'une migration passée — une consigne
   qui se contredit a déjà fait rejouer une migration appliquée
   (INCIDENTS.md, « Sources gratuites Asie »).
3. **Règle dure n°9** : les lignes de résultats s'ARCHIVENT
   (`..._archive` + bloc RESTAURATION, modèle `migrate_v10_5`), elles ne se
   suppriment jamais.

## Procédure

Délègue la rédaction au sub-agent **`migration-author`** : son Write est
mécaniquement borné à `sql/migrate_v*.sql` (hook frontmatter), il connaît
l'en-tête maison (`migrate_v10_9_scan_request_rpc.sql` en modèle), la
référence RLS (`migrate_v9_3_tighten_rls.sql`) et vérifie les colonnes
contre `core/db.py` et `AUDIT.md` §2.

Ensuite, côté session principale :

1. Ajouter/mettre à jour le test gardien qui suppose le nouveau schéma.
2. Dire à l'opérateur QUOI coller, et donner le SELECT témoin qui prouve
   l'application (modèle : `team_aliases`, vérifiée en base le 2026-08-22).
3. Mettre à jour `AUDIT.md` §2 si un invariant naît avec la table.
