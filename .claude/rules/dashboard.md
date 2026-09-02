---
paths:
  - "api/**"
  - "templates/**"
---

# Règles — dashboard Flask

- Le dashboard est en LECTURE SEULE : aucune clé d'écriture
  (`tests/test_api_admin_auth.py::TestLeDashboardNaPlusDeCleDEcriture`,
  vérifié sur l'AST). `/api/scan` passe par la fonction Postgres
  `demander_scan()` ; `/api/audit/run` exige `DASHBOARD_ADMIN_TOKEN` et
  échoue FERMÉ — ne pas « réparer » son 401.
- **Règle dure n°5** : une suite verte ne prouve RIEN sur le déploiement.
  Après toute retouche de `api/index.py` (ou `vercel.json`,
  `.python-version`, `requirements*`) : skill `predator-release`
  (`ops.py vercel deployments` puis `curl …/api/health`). Le hook
  `verify_before_stop.sh` rappelle cette check-list.
- La suite ne rend AUCUN template et n'appelle aucune route : après toute
  retouche de rendu, skill `predator-dashboard-check` (smoke test local) —
  le hook `dashboard_smoketest.sh` en fait une version automatique.
- `/ledger` et `/audit` sont volontairement HORS des menus (décision
  opérateur) : servies, non liées. Ne pas les « re-corriger ».
- Une seule version (`DASHBOARD_VERSION`), injectée — jamais un numéro dans
  un pied de page. Tables sport INJECTÉES, jamais redéfinies dans un
  template (règle n°6). Zone jouable : `_is_playable()`, partagée.
- `.python-version` (3.12) appartient à VERCEL — règle dure n°4, l'aligner
  sur 3.11 casse le déploiement.
