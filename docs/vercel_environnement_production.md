# Vercel — déploiement gated et secrets d'environnement

> **FAIT le 2026-08-26.** Ce document enregistre ce qui a été exécuté et
> vérifié, pas une procédure à suivre. Les trois secrets `VERCEL_*` ne sont
> plus au niveau du dépôt.

## Ce qui a été fait

1. **`vercel.json` désactive le déploiement Git de `main`** :
   `"git": {"deploymentEnabled": {"main": false}}`.
2. **Les trois secrets vivent dans l'environnement GitHub `Production`** —
   posés par API (chiffrement libsodium contre la clé publique de
   l'environnement), puis **supprimés du niveau dépôt** (HTTP 204 sur les
   trois). Vérifié après coup : `VERCEL_*` restants au niveau dépôt = `[]`,
   environnement `Production` = `['VERCEL_ORG_ID', 'VERCEL_PROJECT_ID',
   'VERCEL_TOKEN']`.
3. **Valeurs** : le token vient du `.env` de l'opérateur ; `VERCEL_ORG_ID`
   (`accountId`) et `VERCEL_PROJECT_ID` (`id`) ont été **dérivés de l'API
   Vercel** pour ce projet — pas recopiés, donc pas de risque de faute de
   frappe.
4. **Ordre de bascule** : les secrets d'environnement priment sur ceux du
   dépôt, donc le déploiement suivant les a mis à l'épreuve avant toute
   suppression. `deploy` vert sur `e1bfd47`, un seul déploiement (`source:
   cli`), `/api/health` → `db_configured: true`.

Le dépôt avait déjà un environnement nommé **`Production`** (créé par
l'intégration Vercel le 2026-05-03). Les noms d'environnement GitHub sont
uniques **sans distinction de casse** : le `environment: production` de
`ci.yml` s'y rattache, et aucun treizième environnement n'a été créé (total
inchangé : 12).

## Pourquoi c'était le cœur du chantier

Mesuré via l'API Vercel avant le changement : chaque commit touchant `api/`,
`templates/` ou `core/` était déployé **deux fois**, en course — une fois par
l'intégration Git, une fois par le CLI de `deploy.yml`.

| source | état | commit |
|--------|------|--------|
| `cli`  | READY | 249ac32 |
| `git`  | READY | 249ac32 |
| `cli`  | READY | 3d639b4 |
| `git`  | READY | 3d639b4 |

La voie Git ne connaît pas la suite de tests. Tant qu'elle était active, le
`needs: test` de `ci.yml` avait l'apparence d'un gate sans en être un.

**Le gate a été prouvé sur un cas réel** : `c6cc762`, dont la CI était rouge,
n'a été déployé ni par le CLI ni par Git. Avant ce chantier, la voie Git
l'aurait mis en production sans attendre.

Gardiens : `tests/test_workflow_secrets.py::test_vercel_ne_deploie_pas_aussi_depuis_git`
et `::test_le_deploiement_est_le_seul_job_a_porter_un_environnement`.

## Effet de bord assumé

Un push purement documentaire ne redéploie plus. Avant, la voie Git déployait
*tout* push sur `main`. Désormais seul `ci.yml` déploie, sur ses `paths` :
`**.py`, `api/**`, `templates/**`, `requirements.txt`, `vercel.json`,
`.github/**`. Un commit qui ne touche aucun de ces chemins ne change rien au
site — c'est correct, mais ce n'est plus le comportement précédent.

## Ce qu'il reste à faire (opérateur)

**Révoquer le PAT** utilisé pour cette bascule : il a transité par une
conversation, il doit être considéré comme compromis.
https://github.com/settings/tokens → révoquer. Il n'est plus nécessaire à rien :
aucun workflow ne le lit, et `GITHUB_PAT` dans `.env` ne sert qu'aux gestes
manuels depuis un poste de dev.
