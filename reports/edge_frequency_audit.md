# Audit empirique : amplitude × fréquence de l'edge réelle
**Date de l'audit : 2026-07-08**
**Outil : `scripts/edge_frequency_audit.py` (testé, 21 tests unitaires, exécuté contre les données réelles de production)**

## Réponse directe

**Aucun k (2 à 12) ne peut être validé aujourd'hui avec les données réelles disponibles — ni positivement, ni négativement.** Ce n'est pas parce que l'edge ne fonctionne pas : c'est parce qu'il n'existe actuellement que **2 jours de données réelles** (12 signaux au total) en base, ce qui est très en dessous du minimum nécessaire pour répondre à la question posée. Aucune conclusion sur la viabilité commerciale du système ne peut être tirée de cet échantillon — voir §3 pour ce qu'il faudrait pour trancher.

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

---

## Conclusion et recommandations

**Ce que cet audit établit avec certitude :**
- L'infrastructure de mesure (biais de sélection, corrélation, fréquence, magnitude, validation post-hoc) est maintenant construite, testée (21 tests unitaires + exécution réelle réussie), et prête à être ré-exécutée.
- Le registre de données actuel est **structurellement insuffisant** pour répondre à la question posée — pas à cause d'un bug, mais à cause de l'historique perdu (incident du 07-07) et du fait que le rebuild du pipeline (v9.5) vient tout juste d'être déployé aujourd'hui.

**Ce que cet audit n'établit PAS (et ne peut pas établir aujourd'hui) :**
- Que l'edge 1xBet-vs-Pinnacle a — ou n'a pas — une fréquence suffisante pour un k donné. La question reste ouverte.

**Prérequis avant de pouvoir re-trancher :**
1. Appliquer les migrations `sql/migrate_v9_5` à `v9_8` (colonnes `kelly_pct`/`sharp_prob`/`closing_pinnacle_price`/`correlation_group`) — sans elles, même un futur ré-audit reposera sur les mêmes approximations de repli.
2. Laisser tourner le pipeline au moins **3-4 semaines** en continu (couvrant idéalement 20-30 jours de scan effectif) avant de ré-exécuter `python scripts/edge_frequency_audit.py` — c'est le minimum pour que §3/§4 produisent un ratio jours-valides/total-jours qui ne soit pas dominé par le bruit d'échantillonnage.
3. Idéalement, implémenter le patch de logging des candidats rejetés (§0.a) en parallèle, pour qu'un audit futur puisse aussi répondre à la question du biais de sélection, pas seulement à celle de la fréquence des signaux déjà qualifiants.
4. Viser ≥30 combos k-jambes entièrement réglés (WIN/LOSS sur chaque jambe) avant de faire confiance à la validation post-hoc de l'Étape 6 pour un k donné — cohérent avec le seuil `_MIN_SAMPLES=30` déjà utilisé dans `core/learning_layer.py`.

**Recommandation opérationnelle immédiate :** ne pas prendre de décision d'architecture (abandonner ou renforcer le pari sur 1xBet-vs-Pinnacle) sur la base des données actuelles — ni dans un sens, ni dans l'autre. Re-questionner dans 3-4 semaines avec `scripts/edge_frequency_audit.py`.
