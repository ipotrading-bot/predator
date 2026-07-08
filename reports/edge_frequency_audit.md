# Audit empirique : amplitude × fréquence de l'edge réelle
**Date de l'audit : 2026-07-08 (mise à jour 13:29 UTC — voir §-1 ; complété §-2/§-3, pas de nouveau run — voir ces sections)**
**Outil : `scripts/edge_frequency_audit.py` (testé, 21 tests unitaires, exécuté contre les données réelles de production — dernier run réel : le jeu de 12 signaux/2 jours ci-dessous, PRÉ-fix du bug `compute_alpha()`, voir §-2)**

## Réponse directe

**Aucun k (2 à 12) ne peut être validé aujourd'hui avec les données réelles disponibles — ni positivement, ni négativement**, et pire : le volume de données n'augmente plus du tout depuis le déploiement de ce matin. Ce n'est plus seulement "pas encore assez de temps a passé" — **voir §-1, découvert en ré-exécutant cet audit l'après-midi du même jour : le pipeline génère actuellement ~0 signal par cycle de scan**, pour une raison identifiée et chiffrée ci-dessous, distincte de la question de fréquence posée par ce prompt. Tant que ce point n'est pas corrigé, aucune quantité de temps supplémentaire n'accumulera de données exploitables.

---

## §-1 — MISE À JOUR CRITIQUE (2026-07-08 13:29 UTC) : le pipeline ne produit plus de signaux

Ré-exécution de cet audit ~9h après le déploiement du code v9.5. Constat en base : `signals` est passé de 11 à **3 lignes actives** (purge normale, aucune ligne ajoutée), `ai_learning_ledger` toujours à **1 ligne**. Pourtant, au moins 8 cycles de scan (`Predator Engine`/`Golden Hour`/`Deep Scan`) ont tourné avec succès sur cette fenêtre.

Inspection directe des logs GitHub Actions du run `Predator Engine` le plus récent : **22 candidats évalués, 22 `DISCARD`, 0 signal émis.** Exemple concret dans les logs : `SOC Under 2.75 — edge 11.35%` — un edge de 11.35%, qui aurait été un signal évident sous l'ancien système, a été rejeté.

**Cause identifiée et vérifiée par calcul direct** (`core.constants.min_edge_for_k`) : pour un marché totals/spreads proche de l'équilibre (`true_prob` 0.50-0.56, cotes Pinnacle 1.80-2.00 typiques), le plancher fiscal par-signal exigé avant toute formation de système est de **12.8% à 14.4%** — un edge de 11.35% reste donc insuffisant à *n'importe quelle* cote réaliste dans cette fourchette.

**Le vrai problème n'est pas le calcul en lui-même (il est mathématiquement cohérent), c'est où il est appliqué :** `compute_alpha()` (Task 2) filtre chaque candidat *individuellement* avec le plancher `min_edge_for_k(k=1, ...)` — le seuil le PLUS exigeant possible (rappel de l'audit précédent : le plancher par-jambe *diminue* avec k). Résultat : une jambe qui serait parfaitement viable *en tant que partie d'un système à 4-6 jambes* (plancher ~5-8%) est rejetée avant même d'atteindre `suggest_system()`, qui ne voit jamais l'occasion de la combiner. Le garde-fou du Task 2 bloque ce que le Task 5 est censé rendre possible.

**Bug secondaire confirmé en même temps** (logs du même run) : `check_circuit_breaker: column ai_learning_ledger.kelly_pct does not exist` — confirme que les migrations v9.5-v9.8 ne sont toujours pas appliquées ; dégradation gracieuse comme prévu (pas de crash), mais le circuit breaker, le ROI réel et la capture de closing line tournent tous à vide depuis ce matin.

**CORRIGÉ (2026-07-08, validé avant implémentation) :** `compute_alpha()` ne gate plus sur `min_edge_for_k(1, ...)` — cette fonction a été retirée de `core/constants.py` (elle n'avait plus aucun appelant). Le seul filtre restant en amont de la formation de système est le plancher appris par sport (`learning_layer`) + le plafond `MAX_EDGE`. `core.tax_engine.suggest_system()`/`is_combo_tax_viable()` reste le seul vrai juge de la viabilité fiscale, évalué sur la combinaison réellement assemblée plutôt que sur une approximation par-jambe au pire cas (k=1). 168 tests passent après le correctif (voir `tests/test_math_engine.py::test_near_coinflip_edge_not_gated_on_tax_here`, qui garde une trace explicite de cet incident).

**Migrations v9.5-v9.8 :** toujours non appliquées à cette heure — reste à faire manuellement dans l'éditeur SQL Supabase avant que le circuit breaker, le ROI réel et la capture de closing line ne deviennent pleinement fonctionnels.

---

## §-2 — Vérification post-fix (2026-07-08, suite) : l'audit a-t-il tourné sur des données réelles APRÈS le fix ?

**Non — pas au moment où cette section a été écrite.** Réponse directe et vérifiée à nouveau ce jour :

- Au moment de l'audit ci-dessous, le fix de `compute_alpha()` décrit en §-1 existait **uniquement dans l'arbre de travail, non commité** (`core/paim_engine.py`, `core/constants.py`, ce rapport, `scripts/edge_frequency_audit.py`, `tests/test_math_engine.py`, tous `M` depuis `f3ab949`). Il n'était donc **pas déployé en production** — ni sur GitHub Actions, ni sur Vercel.
- **Mise à jour :** ce fix a depuis été committé et poussé sur `origin/main` (commit `a30cd39`). Il sera actif au **prochain** cycle de scan planifié (`Predator Engine`/`Golden Hour`/`Deep Scan`) — pas avant. Aucun cycle n'a encore tourné dessus au moment de la rédaction de ce paragraphe : le jeu de données présenté en §1-§5 ci-dessous (12 signaux, 2 jours) reste exactement celui qui a servi à *diagnostiquer* le bug, pas un nouveau run post-fix. **La question de fond posée par ce prompt reste donc entièrement d'actualité : l'audit empirique (Étapes 1-7) n'a toujours pas tourné sur des données post-fix** — seul l'obstacle au déploiement (le commit non poussé) a été levé. Il faut maintenant laisser tourner au moins quelques cycles avant de ré-exécuter `scripts/edge_frequency_audit.py` avec un espoir de résultat différent.
- Tentative de vérification en direct dans cette session : requête Supabase via l'outil MCP (`mcp__supabase__execute_sql`) → `Unauthorized` (un problème de token distinct du bug de project-ref tronqué déjà corrigé le 07-07 par `6f9b24b` — le `--project-ref` dans `.mcp.json` est correct). Aucune variable `SUPABASE_URL`/`SUPABASE_KEY` n'est présente dans ce shell pour lancer `scripts/edge_frequency_audit.py` directement non plus. **Donc : ni un nouveau run du script, ni une requête directe n'ont pu confirmer ou infirmer un changement de volume de données dans cette session.** À relancer dès que l'accès DB (MCP ou env vars) est rétabli.

**Ce qui a été fait aujourd'hui, précisément :** le bug de `compute_alpha()` a été identifié, corrigé dans le code, couvert par un test de non-régression (`tests/test_math_engine.py::test_near_coinflip_edge_not_gated_on_tax_here`), et sa cause racine documentée. **Ce qui n'a PAS été fait :** déployer ce fix, laisser le pipeline tourner dessus, ni ré-exécuter l'audit empirique (Étapes 1 à 7) sur des données produites après le fix. Les prérequis listés en fin de rapport (§ Conclusion, points 1-2) restent entièrement valables et s'appliquent maintenant aussi au déploiement de ce fix lui-même, pas seulement aux migrations SQL.

---

## §-3 — Le plancher « appris par sport » (`min_edge` de `compute_alpha`) est-il basé sur `clv_final` ?

**Non, pas dans le code actuel.** Vérifié par lecture directe :

- `min_edge` transmis à `compute_alpha()` vient de `core.learning_layer.load_thresholds()` (`run_engine.py:1009,1268`), qui lit `SPORT_DEFAULTS` puis les overrides de la table `meta` écrits par `compute_and_save()`.
- `compute_and_save()` → `_sport_stats()` (`core/learning_layer.py:71-113`) calcule `hit_rate` **exclusivement** à partir de la colonne `outcome` (`WIN`/`LOSS` réel, posé par `core/settlement.py::settle_signal()` à partir du score de match effectivement récupéré — voir `settlement.py:138-141`) — **jamais** à partir de `clv_final`. Le docstring du module (`learning_layer.py:1-11`) et celui de `_sport_stats()` l'affirment explicitement, avec l'explication du piège : `clv_final` est une re-dérivation de l'edge d'entrée à partir des **mêmes prix de scan** déjà utilisés pour `edge_pct` — comme `MIN_EDGE` ne laisse passer que des edges positifs à l'origine, `clv_final` est quasi toujours ≥ 0 *indépendamment du résultat réel du match*.
- Ce piège exact a un nom dans le code : `tests/test_learning_layer.py` le documente comme le **« tautology bug »** — « compute_and_save() used to derive hit_rate from clv_final >= 0 [...] A batch of 100% real LOSS outcomes could still show ~100% "hit rate" under the old code » — avec des tests dédiés qui échouent sur l'ancien code et passent sur le nouveau.
- **Ce bug a déjà été corrigé séparément, avant le travail v9.5 d'aujourd'hui** : commit `d67ca84` (« fix: learning_layer lit ai_learning_ledger (pas signals.clv_pct) », 2026-07-03T11:13:54Z) — 5 jours avant l'incident documenté en §-1. Il est committé, pas dans le diff en cours, donc actif en production dès lors que `compute_and_save()` tourne (appelé depuis `core/audit_engine.py:305`, à chaque cycle d'audit).

**Verdict : la prémisse de la question ne s'applique plus au code actuel — ce n'est pas un bug résiduel à corriger séparément, il l'a déjà été (Tâche 1), avant même ce prompt.**

**Réserve non vérifiable dans cette session** (à vérifier dès que l'accès DB est rétabli, cf. §-2) : les valeurs actuellement en base dans `meta.threshold_*` reflètent le dernier appel de `compute_and_save()` ayant vu ≥30 échantillons décisifs (`_MIN_SAMPLES`) pour un sport donné. Avec `ai_learning_ledger` à **1 seule ligne permanente** (conséquence de l'incident du 07-07, voir §0.c), il est très probable qu'aucun recalcul n'a eu lieu depuis avant cet incident — donc soit ces seuils sont toujours à `SPORT_DEFAULTS` (jamais ajustés), soit ils datent d'un recalcul antérieur au 07-07. Comme le fix du tautology bug (`d67ca84`) précède le 07-07 de 4 jours, un recalcul dans cette fenêtre aurait déjà utilisé la bonne logique — mais je n'ai pas pu confirmer `updated_at` ni les valeurs elles-mêmes en direct (`Unauthorized` sur `mcp__supabase__execute_sql`, voir §-2). Requête à lancer dès que possible : `SELECT key, value, updated_at FROM meta WHERE key LIKE 'threshold_%';`

---

## §0 — Constats préalables (avant tout calcul)

### a. Biais de sélection confirmé
`run_engine.py::_emit()` ne fait `signals.append(signal)` que pour les candidats qui passent `compute_alpha()` (edge ≥ seuil). Toute jambe rejetée (`DISCARD`/`VOLATILE`) n'est journalisée que via `log.info`/`log.warning` (logs GitHub Actions éphémères, non interrogeables) — **jamais persistée dans une table**. La distribution réelle de l'edge (y compris les quasi-ratés sous le seuil) reste donc invisible avec l'architecture actuelle.

**Patch de logging recommandé** (prérequis pour un futur audit non biaisé) : journaliser dans une table dédiée (ex. `candidate_log`) **tous** les candidats scannés — sport, marché, match, edge calculé, statut (OK/DISCARD/VOLATILE/SUSPECT), horodatage — pas seulement ceux qui passent le filtre. Sans cela, toute analyse future de la distribution complète de l'edge restera tronquée par construction, quel que soit le volume de données accumulé.

### b. Migrations v9.5–v9.8 non appliquées
Vérifié directement en base (schéma réel `ai_learning_ledger`) : `kelly_pct`, `sharp_prob`, `closing_pinnacle_price`, `clv_pct_real` sont absents ; `signals` n'a pas non plus `correlation_group`. Conséquence pour cet audit : `true_prob` a dû être dérivé par repli (`sharp_prob` du signal si présent, sinon `1/pinnacle_price`, ou pour la table ledger `1/(odds/(1+edge/100))`) — mathématiquement cohérent avec ce que `compute_alpha()` supposait au moment du scan, mais moins précis qu'une vraie colonne `sharp_prob` persistée.

### c. Volume de données réel
Interrogation directe de la base (l'outil MCP Supabase standard renvoyait `Unauthorized` malgré un token valide — contournement via l'API Management directe, voir conversation) :

| Table | Lignes | Période couverte |
|---|---|---|
| `signals` | 11 | 2026-07-08 00:58–03:36 UTC (2h38, une seule session de scan) |
| `ai_learning_ledger` (registre permanent) | 1 | 2026-07-06 (1 seule sortie réelle réglée : LOSS, CLV -14.8%) |

Le registre permanent (`ai_learning_ledger`) ne contient qu'**une seule ligne** au total — conséquence directe de l'incident du 2026-07-07 documenté précédemment (17h+ de non-persistance, puis perte définitive des signaux à statut terminal purgés avant que l'audit ne les capture). Il n'y a, à ce jour, pratiquement aucun historique exploitable.

---

## §1–§2 — Distribution empirique de l'edge (12 signaux, 2 jours)

À titre indicatif uniquement — **échantillon bien trop petit pour être représentatif** :

| Sport:Marché | n | Moyenne | Écart-type | p10 | p50 | p90 |
|---|---|---|---|---|---|---|
| baseball:h2h | 6 | 2.19% | 0.89 | 1.18% | 1.70% | 2.84% |
| baseball:totals_over | 3 | 2.90% | 1.68 | 1.06% | 3.28% | 4.35% |
| baseball:totals_under | 1 | 10.71% | — | — | — | — |
| basketball:h2h | 1 | 3.05% | — | — | — | — |
| basketball:totals_under | 1 | 2.13% | — | — | — | — |

Observation qualitative (non concluante à n=12) : la distribution **n'est visiblement pas homogène** entre marchés (2.19% de moyenne en baseball h2h vs 10.71% pour l'unique point totals_under) — cohérent avec la mise en garde du prompt sur l'hypothèse simplificatrice de `tax_engine.py`, mais 1 à 6 points par groupe ne permet aucune inférence statistique.

`true_prob` observé sur ces 12 signaux : la majorité se situe entre 0.50 et 0.67 (proche de l'équilibre) — c'est précisément la zone où `min_edge_required()` exige le plus d'edge brut pour survivre à la taxe (voir l'audit précédent : ~11-14% requis à p≈0.55 pour un système à 1 jambe).

---

## §3 — Test de fréquence par k (Étape 4)

Résultat sur les 2 jours disponibles : **`valid_days = 0` pour tout k de 2 à 12.**

Deux causes cumulatives, toutes deux réelles et documentées, pas une anomalie de calcul :
1. **2 jours de données ne permettent pas de mesurer une fréquence.** Même si chaque jour avait qualifié, on obtiendrait au mieux "1/2" ou "2/2" — statistiquement vide de sens.
2. **Aucun jour n'a k≥2 jambes qualifiantes de groupes de corrélation distincts** au seuil `min_edge_required(k)*1.15` — les edges observés (1-4% pour la majorité, à des `true_prob` proches de 0.5-0.67) sont sous le seuil de rentabilité fiscale même pour k=2. Seul le point à 10.71% (baseball totals_under) serait individuellement viable, mais un seul point ne fait pas un système.

## §4 — Magnitude (Étape 5) et §5 — Validation post-hoc (Étape 6)

Directement dépendantes de §3 : aucun jour valide → `n_opportunities=0` et `expected_monthly_log_growth=0.0` pour tout k. Validation post-hoc : `0` combo entièrement réglé pour tout k, très en dessous du seuil de 30 échantillons — rapporté explicitement comme **non significatif**, pas comme "0% de réussite".

### Tableau explicite k / jours valides / log-growth mensuel attendu

Sortie ligne par ligne de `frequency_by_k()` + `magnitude_by_k()` (`scripts/edge_frequency_audit.py`) sur le jeu de données actuel (`total_days=2`, 12 signaux — **le même jeu de données pré-fix qu'en §1-§2, voir §-2** : aucune donnée post-fix n'existe encore pour relancer ce calcul avec un résultat différent) :

| k | jours valides / total | ratio | systèmes estimés/mois | opportunités (magnitude) | log-growth moy./opportunité | opportunités/mois | **log-growth mensuel attendu** |
|---|---|---|---|---|---|---|---|
| 2  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 3  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 4  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 5  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 6  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 7  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 8  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 9  | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 10 | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 11 | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |
| 12 | 0/2 | 0.0 | 0.0 | 0 | 0.0 | 0.0 | **0.0** |

Uniforme à 0 sur toute la plage — pas une erreur de calcul, c'est la conséquence directe et déterministe de `valid_days=0` pour chaque k (§3) : `_best_qualifying_legs_per_group()` ne trouve jamais ≥k jambes qualifiantes le même jour depuis des groupes de corrélation distincts, donc `magnitude_by_k()` n'a aucune journée candidate à passer à `system_expected_value()`, quel que soit k. **Ce tableau ne dit rien sur la viabilité réelle du système** (§ Conclusion) — il dit seulement qu'avec 2 jours et 12 signaux, aucun k n'a pu être ni confirmé ni infirmé, dans un sens comme dans l'autre.

---

## Conclusion et recommandations

**Ce que cet audit établit avec certitude :**
- L'infrastructure de mesure (biais de sélection, corrélation, fréquence, magnitude, validation post-hoc) est maintenant construite, testée (21 tests unitaires + exécution réelle réussie), et prête à être ré-exécutée.
- Le registre de données actuel est **structurellement insuffisant** pour répondre à la question posée — pas à cause d'un bug, mais à cause de l'historique perdu (incident du 07-07) et du fait que le rebuild du pipeline (v9.5) vient tout juste d'être déployé aujourd'hui.

**Ce que cet audit n'établit PAS (et ne peut pas établir aujourd'hui) :**
- Que l'edge 1xBet-vs-Pinnacle a — ou n'a pas — une fréquence suffisante pour un k donné. La question reste ouverte.

**Prérequis avant de pouvoir re-trancher :**
0. **Committer et déployer le fix `compute_alpha()` (§-1/§-2)** — tant qu'il reste dans l'arbre de travail non commité, aucune donnée post-fix ne peut exister, donc aucun ré-audit n'a de sens. C'est le blocage immédiat, avant même les migrations.
1. Appliquer les migrations `sql/migrate_v9_5` à `v9_8` (colonnes `kelly_pct`/`sharp_prob`/`closing_pinnacle_price`/`correlation_group`) — sans elles, même un futur ré-audit reposera sur les mêmes approximations de repli.
2. Laisser tourner le pipeline au moins **3-4 semaines** en continu (couvrant idéalement 20-30 jours de scan effectif) avant de ré-exécuter `python scripts/edge_frequency_audit.py` — c'est le minimum pour que §3/§4 produisent un ratio jours-valides/total-jours qui ne soit pas dominé par le bruit d'échantillonnage.
3. Idéalement, implémenter le patch de logging des candidats rejetés (§0.a) en parallèle, pour qu'un audit futur puisse aussi répondre à la question du biais de sélection, pas seulement à celle de la fréquence des signaux déjà qualifiants.
4. Viser ≥30 combos k-jambes entièrement réglés (WIN/LOSS sur chaque jambe) avant de faire confiance à la validation post-hoc de l'Étape 6 pour un k donné — cohérent avec le seuil `_MIN_SAMPLES=30` déjà utilisé dans `core/learning_layer.py`.
5. Dès que l'accès Supabase (MCP ou `SUPABASE_URL`/`SUPABASE_KEY`) est rétabli, exécuter `SELECT key, value, updated_at FROM meta WHERE key LIKE 'threshold_%';` pour lever la réserve de §-3 sur la fraîcheur des seuils appris par sport.

**Recommandation opérationnelle immédiate :** ne pas prendre de décision d'architecture (abandonner ou renforcer le pari sur 1xBet-vs-Pinnacle) sur la base des données actuelles — ni dans un sens, ni dans l'autre. Re-questionner dans 3-4 semaines avec `scripts/edge_frequency_audit.py`.
