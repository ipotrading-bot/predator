---
paths:
  - "core/ai_router.py"
  - "core/ai_search.py"
---

# Règles — couche IA

- ⛔ **Règle dure n°3** : aucun nom de modèle IA en dur hors de
  `core/ai_router.py`. Le paysage des paliers gratuits churne chaque mois ;
  trois copies à la main des modèles Groq ont déjà fait marteler un modèle
  mort en 404 jusqu'au timeout de 540 s qui tuait le Deep Scan. Les listes
  se DÉRIVENT du registre ; gardien :
  `tests/test_ai_router.py::TestAucunModeleEnDurHorsDuRegistre` (AST — un
  commentaire a le droit de nommer un modèle mort pour raconter pourquoi).
  Le hook `guard_ai_models.sh` refuse le littéral partout ailleurs.
- Le registre (`REGISTRY`) est la source : lanes, budgets, `terms_flag`
  (non_commercial/evaluation = exclu de production EXPRÈS), disjoncteur
  (3 échecs → 30 min). Un fournisseur sans clé est ignoré SANS erreur —
  propriété désirable, mais c'est la panne « capacité morte en silence » :
  toute liste liée au registre doit être dérivée ou testée contre lui.
- Un catalogue lisible ne prouve RIEN (200 sur /models et 402 à
  l'inférence, vécu trois fois). **`python scripts/ops.py ai` fait une VRAIE
  inférence — c'est le SEUL diagnostic qui tranche sur un fournisseur.**
- Un 401/403 SANS clé ne prouve jamais qu'un palier a fermé ; il faut une
  clé INVALIDE pour trancher.
- Groq et Tavily sont SUPPRIMÉS (2026-09-02) : le settlement est 100 %
  déterministe (`core/score_sources.py`). Ne pas les réintroduire.
