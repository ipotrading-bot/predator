# Vercel — déploiement gated et secrets d'environnement

> Rédigé le 2026-08-26 (chantier C de la refonte CI). Les commandes `gh secret`
> sont à exécuter **par l'opérateur** : le token de session est un token d'App
> sans le scope `secrets` (`gh secret list` → `HTTP 403: Resource not
> accessible by integration`).

## Ce qui a été changé dans le dépôt

`vercel.json` désactive le déploiement Git automatique de `main` :

```json
"git": { "deploymentEnabled": { "main": false } }
```

**Pourquoi c'est le cœur du chantier, pas une finition.** Mesuré via l'API
Vercel le 2026-08-26, chaque commit touchant `api/`, `templates/` ou `core/`
était déployé **deux fois**, en course :

| source | état | commit |
|--------|------|--------|
| `git`  | READY | 482ab2a |
| `cli`  | READY | 249ac32 |
| `git`  | READY | 249ac32 |
| `cli`  | READY | 3d639b4 |
| `git`  | READY | 3d639b4 |

La voie `git` ne connaît pas la suite de tests. Tant qu'elle est active, le
`needs: test` de `ci.yml` a l'apparence d'un gate sans en être un : une
régression part en production pendant que les tests tournent encore.
Gardé par `tests/test_workflow_secrets.py::test_vercel_ne_deploie_pas_aussi_depuis_git`.

## Ce qui reste à faire, dans cet ordre

L'ordre compte, et il est sûr : un job portant `environment: production` voit
les secrets **de l'environnement ET du dépôt** (l'environnement surcharge).
Rien ne casse entre l'étape 1 et l'étape 3.

### 1. Merger cette branche

Le déploiement continue de fonctionner avec les `VERCEL_*` actuels, au niveau
du dépôt. GitHub crée l'environnement `production` tout seul au premier run du
job `deploy`.

⚠️ **Le dépôt a déjà 12 environnements**, dont un nommé `Production`
(majuscule, créé par l'intégration Vercel le 2026-05-03) et onze
`Production – predator*`. Les noms d'environnement GitHub sont uniques
**sans distinction de casse** : `environment: production` se rattachera à
`Production`. C'est voulu — inutile d'en créer un treizième.

### 2. Poser les trois secrets dans l'environnement

Les valeurs sont dans votre `.env` local (jamais lues ici).

```bash
gh secret set VERCEL_TOKEN      --env production
gh secret set VERCEL_ORG_ID     --env production
gh secret set VERCEL_PROJECT_ID --env production
```

Si `gh` refuse (`Resource not accessible by integration`), le chemin UI est :
**Settings → Environments → Production → Environment secrets → Add secret**.

### 3. Vérifier un déploiement vert, PUIS retirer les secrets du dépôt

C'est cette étape qui produit le bénéfice : tant que les `VERCEL_*` sont au
niveau du dépôt, ils sont dans le `toJSON(secrets)` de **tous** les workflows —
donc dans la portée de chaque step de chaque scan, actions tierces comprises.
`scripts/ci_env.py` les filtre avant de lancer le process, mais il ne peut rien
contre ce qui lit `SECRETS_JSON` directement.

```bash
python scripts/ops.py vercel deployments | head -3   # READY, source cli, pas ERROR
curl -s https://<domaine>/api/health                  # version attendue
# seulement alors :
gh secret delete VERCEL_TOKEN
gh secret delete VERCEL_ORG_ID
gh secret delete VERCEL_PROJECT_ID
```

## Effet de bord assumé

Un push purement documentaire ne redéploie plus. Avant, la voie Git déployait
*tout* push sur `main` (c'est ce qu'on voit sur 482ab2a, un commit de docs seul).
Désormais seul `ci.yml` déploie, sur ses `paths` : `**.py`, `api/**`,
`templates/**`, `requirements.txt`, `vercel.json`. Un commit qui ne touche
aucun de ces chemins ne change rien au site — c'est le comportement correct,
mais ce n'est plus le précédent.
