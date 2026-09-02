---
name: predator-release
description: Check-list de mise en production du dashboard PREDATOR après toute retouche de vercel.json, .python-version, requirements* ou api/index.py. Use BEFORE telling the user a deploy-adjacent change is done — a green test suite proves NOTHING about the deployment (hard rule #5).
---

# Vérifier un déploiement — règle dure n°5

Une suite verte ne prouve RIEN sur le déploiement : aucun test ne déploie.
1012 tests verts ont déjà accompagné une production cassée (AUDIT.md §3.8).
Le hook `verify_before_stop.sh` rappelle cette check-list quand un fichier
de déploiement est touché — il ne l'exécute pas (credentials opérateur).

## La check-list, dans l'ordre

1. Le push ne déploie pas : le déploiement Git Vercel est DÉSACTIVÉ
   (`vercel.json`) ; c'est le job `deploy` de `ci.yml` qui pousse en CLI si
   la suite est verte. Vérifier que la CI du commit est passée :
   `gh run list --workflow ci.yml --limit 3`.
2. `python scripts/ops.py vercel deployments | head -3` → l'état doit être
   **READY**, pas ERROR.
3. `curl -s https://predator-two.vercel.app/api/health` → version attendue,
   `db_configured: true`.
4. Une SEULE source de déploiement : les deployments récents doivent venir
   de `cli`, jamais de `git` — sinon le gate `needs: test` est décoratif
   (`tests/test_workflow_secrets.py::test_vercel_ne_deploie_pas_aussi_depuis_git`).

## Les deux interpréteurs — ne jamais « aligner »

`.python-version` et `vercel.json` disent **3.12** et appartiennent à
Vercel (son image de build n'a pas 3.11) ; les workflows et le dev local
sont en **3.11**. L'« alignement » est le geste exact qui a laissé la
production sans correctif de sécurité (AUDIT.md §3.8). `permissions.deny`
bloque l'édition de `.python-version` ; la divergence est VOULUE et testée.

## Si le rendu a changé (api/index.py, templates)

Skill `predator-dashboard-check` AVANT de conclure : la suite ne rend aucun
template — seul un `python api/index.py` local attrape un 500 de template.
