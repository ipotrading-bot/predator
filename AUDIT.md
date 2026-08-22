# AUDIT — PREDATOR

> **Document de référence.** Il enregistre ce qui a été vérifié, ce qui a été
> corrigé, et surtout **quel test garde quel invariant**. Quand un doute
> revient sur l'équilibre du système, c'est ici qu'on regarde avant de
> rouvrir le code.
>
> Règle de tenue : on n'écrit ici que ce qui a été **mesuré**. Une hypothèse
> non vérifiée est marquée comme telle. Un document d'audit qui affirme sans
> preuve fait exactement le dégât qu'il prétend éviter.

Dernière passe : **2026-08-22**. État à la clôture : **1033 tests, 0 échec**,
pyflakes propre, les 6 pages du dashboard rendent (smoke test local).

---

## 1. La classe de bug qui domine ce dépôt

Trois des cinq défauts sérieux trouvés le 2026-08-22 sont **la même panne** :
une liste tenue à la main qui a divergé de sa source, sans qu'aucune erreur
ne soit levée.

| Liste | A divergé de | Conséquence mesurée |
|---|---|---|
| Clés IA dans `wiz.yml` | `core/ai_router.py` | Tout le repli de Wiz partait sur Groq — le workflow contournait la réserve settlement qu'il prétendait protéger |
| Clés IA dans les 7 workflows | `PRODUCTION_SAFE` | OVH et SiliconFlow inatteignables : 2 fournisseurs sur 9 morts en production |
| `_AI_SECRETS` dans `scripts/ops.py` | `REGISTRY` | `secrets-push` sautait `OVH_AI_API_KEY` en silence |
| Tables sport→emoji dans `index.html` | `api/index.py` | 3 sports actifs affichés « 🎯 rugbyleague » |
| 6 pieds de page + `/api/health` | (aucune source) | 6 numéros de version différents sur 6 onglets |

**Pourquoi c'est coûteux ici en particulier.** `core/ai_router.py` ignore
silencieusement un fournisseur sans clé — et c'est sa propriété *désirable* :
un palier gratuit qui ferme ne doit pas casser un run. La contrepartie, c'est
qu'une capacité peut rester morte des mois sans un log, sans un test rouge.
La suite de tests ne pouvait rien voir : **le code était correct**, c'est le
câblage qui manquait.

**La parade retenue** — ne plus jamais tenir ces listes à la main. Soit elles
sont dérivées de leur source (`_AI_SECRETS`, tables sport injectées dans les
templates), soit un test compare la copie à la source à chaque exécution.

---

## 2. Invariants et leur gardien

C'est le tableau à consulter avant de toucher à quoi que ce soit.

| Invariant | Gardien |
|---|---|
| Tout fournisseur `PRODUCTION_SAFE` est câblé dans **tout** workflow qui fait de l'IA | `tests/test_workflow_secrets.py::test_tout_fournisseur_de_production_est_cable` |
| Les workflows IA ne divergent pas entre eux | `…::test_les_workflows_ia_ne_divergent_pas_entre_eux` |
| `CLOUDFLARE_API_TOKEN` ne va jamais sans `CLOUDFLARE_ACCOUNT_ID` | `…::test_cloudflare_a_son_identifiant_de_compte` |
| Aucune clé d'API inconnue du registre (attrape la faute de frappe) | `…::test_aucune_cle_ia_inconnue_du_registre` |
| `ops.py::_AI_SECRETS` couvre tout le registre | `…::test_secrets_push_couvre_tout_le_registre` |
| `secrets-push` n'emporte **pas** les clés d'opérateur vers les runners | `…::test_secrets_push_nemporte_pas_les_cles_operateur` |
| `.env.example` documente tout credential réellement lu | `…::test_env_example_documente_les_credentials_reellement_lus` |
| Tout fournisseur du registre est documenté dans `.env.example` | `…::test_tout_fournisseur_du_registre_est_documente…` |
| Chaque job GitHub a une borne de durée | `…::test_chaque_job_a_une_borne_de_duree` |
| Une seule version de Python dans tout le dépôt | `…::test_une_seule_version_de_python` |
| Tout sport actif a emoji + libellé + libellé court + ordre | `tests/test_dashboard_sports.py::test_tout_sport_actif_est_couvert` |
| Les sports retirés gardent leur emoji (lignes historiques) | `…::test_les_sports_retires_gardent_leur_emoji` |
| `index.html` ne redéfinit pas les tables sport en dur | `…::TestPasDeTableDupliquee` |
| Tout sport scanné a un seuil d'edge par défaut | `…::test_les_seuils_appris_couvrent_le_meme_perimetre` |
| `/api/audit/run` échoue **fermé** sans jeton | `tests/test_api_admin_auth.py::test_sans_jeton_configure_la_route_refuse` |
| Le jeton ne se devine pas préfixe par préfixe | `…::test_bon_prefixe_mais_jeton_tronque_refuse` |
| Les refus sont indiscernables entre eux | `…::test_la_reponse_ne_dit_pas_pourquoi` |
| Aucune exception brute ne part dans une réponse HTTP | `…::TestPasDeFuiteDansLesReponses` |
| `/api/health` répond base injoignable et ne publie aucun secret | `…::TestSondeDeSante` |
| Un verdict Wiz `INDISPONIBLE` est rejoué, un verdict réel non | `tests/test_wiz_retry.py` |
| Aucun mois antérieur à l'époque (août 2026) ne remonte sur /performance | `tests/test_mission2_dashboard_quota.py::test_une_fenetre_elargie_ne_rouvre_pas_juillet` |
| La fenêtre reste glissante AU-DESSUS de l'époque (pas figée) | `…::test_la_fenetre_reste_glissante_au_dessus_de_lepoque` |
| L'archivage ne touche jamais un signal `active` | `…::test_le_script_darchivage_de_juillet_existe_et_narchive_pas_a_laveugle` |
| Les sources porteuses de faits survivent à la troncature de Wiz | `tests/test_wiz_sources.py::TestLesFaitsDabord` |
| Les deux sources gratuites de Wiz sont fusionnées, pas mises en concurrence | `…::test_les_deux_sources_gratuites_sont_fusionnees` |
| `.python-version` reste sur la version de **Vercel** (3.12), jamais « alignée » sur les workflows | `tests/test_workflow_secrets.py::test_python_version_appartient_a_vercel` |
| `vercel.json` et `.python-version` annoncent la même version | `…::test_vercel_json_annonce_la_meme_version_que_python_version` |
| Les workflows sont d'accord entre eux sur Python 3.11 | `…::test_les_workflows_partagent_une_seule_version_de_python` |

L'**invariant des sport-keys** (4 fichiers : `core/odds_api.py`,
`core/learning_layer.py`, `api/index.py`, `core/wiz_engine.py`) est décrit
dans `CLAUDE.md` ; son maillon d'affichage est tenu par
`tests/test_dashboard_sports.py`. Vérifié propre sur les 4 fichiers le
2026-08-22.

---

## 3. Corrections du 2026-08-22 (avec la preuve)

### 3.1 `/api/audit/run` était ouverte à tout Internet — `28afaa4`

`POST /api/audit/run` déclenchait `audit.yml` : 45 min de runner, le
settlement, et la consommation de la réserve IA gardée en négatif exprès.
**Aucune authentification, aucun cooldown, aucune limite de débit**, sur une
URL Vercel publique. Aucune interface du dépôt ne l'appelle — elle n'était
connue que de la table du README. Une boucle `curl` anonyme épuisait le quota.

Le coût est documenté : incident du 10→20 août 2026, dix jours sans signal.

Corrigé par un jeton `DASHBOARD_ADMIN_TOKEN` en **échec fermé**. C'est le
point qui compte : la forme du bug d'origine était « pas de PAT → 503 », donc
« PAT présent → ouvert à tous ». Comparaison par `hmac.compare_digest`, refus
indiscernables.

> ⚠️ **Changement de comportement assumé** : la route ne répond plus tant que
> le jeton n'est pas posé dans les variables d'environnement Vercel.

### 3.2 Deux fournisseurs IA inatteignables + trois listes divergentes — `37f4de0`

Voir §1. En corrigeant, le test neuf a immédiatement trouvé un défaut que la
lecture n'avait pas vu : **`.python-version` annonçait 3.12** contre 3.11
partout ailleurs (interpréteur local 3.11.15, `CLAUDE.md`, `vercel.json`, les
14 workflows). Vercel lit ce fichier pour choisir son runtime : le dashboard
pouvait être servi par un interpréteur sur lequel rien n'est testé.

> ⚠️ **Cette conclusion était FAUSSE, et elle a cassé la production.** Voir
> §3.8. `.python-version` appartient à Vercel : son image de build n'embarque
> pas 3.11. Le 3.12 n'était pas une dérive, c'était la contrainte de la
> plateforme.

`.env.example` — dont tout le propos est « copiez-moi en `.env` » — omettait
10 credentials réellement lus : les 5 variables Betfair et tout le bloc de
`scripts/ops.py`, alors que `CLAUDE.md` présente `ops.py` comme *la* façon de
piloter Supabase et Vercel. La commande documentée était inutilisable après
une copie propre.

### 3.3 Dashboard : nav amputée, version qui ment, tables dupliquées — `ec2cacf`

- **`/ledger` et `/audit` n'étaient atteignables sur mobile par aucun lien** —
  `.nav-pages` est masquée sous 640 px et la barre du bas ne portait que 4
  entrées sur 6. Deux pages entières injoignables au doigt.

  > **Suite, le même jour (`0866820`, décision opérateur)** : le menu a été
  > volontairement ramené à quatre entrées — *Accueil · Sys · Wiz · Perf* —
  > et `/ledger` et `/audit` en ont été **retirés**. Les deux routes
  > fonctionnent toujours (200 en production) mais ne sont plus atteignables
  > par aucun lien, ni mobile ni desktop : il faut saisir l'URL.
  > Ce n'est plus le bug corrigé ci-dessus (une nav amputée par accident,
  > incohérente entre desktop et mobile) mais un choix de produit assumé et
  > uniforme sur les 6 pages. Consigné ici pour que personne ne le
  > « re-corrige » en croyant retrouver le défaut d'origine. Les six entrées
  ont d'abord été alignées sur les deux menus.

  > **Suite, le même jour — décision opérateur.** `/ledger` et `/audit` ont
  > ensuite été **volontairement masqués des DEUX menus**, qui portent
  > désormais quatre entrées dans l'ordre **Accueil · Sys · Wiz · Perf**.
  > Les deux pages restent servies et rendent normalement : elles ne sont
  > simplement plus liées, et s'atteignent par URL directe. Ce n'est donc
  > plus un défaut à « réparer » en les remettant — la skill
  > `predator-dashboard-check` porte la même consigne, pour qu'un futur
  > contrôle de parité ne les réintroduise pas de bonne foi.
- Six pieds de page, six versions (`v8.5`, `v8.6`, `v8.8`, `v9.4`, `v10.0`,
  `v1.0`) + `« 8.8 »` en dur dans `/api/health`. Désormais `DASHBOARD_VERSION`,
  une seule définition, injectée par un `context_processor`.
- `/api/signals` ne filtrait pas les matchs commencés alors que les trois
  autres consommateurs le faisaient. **Mesuré en base le 2026-08-22 : 37
  signaux actifs, dont 23 déjà commencés — 62 % de lignes non jouables.**
  Règle extraite dans `_is_playable()`, partagée ; `?all=1` pour le
  diagnostic.
- `Cache-Control` : `no-store` gardé sur les **pages** (un signal périmé
  affiché comme actif est un faux pari), 10 min sur `/static` qui était
  re-téléchargé à chaque navigation.

### 3.4 Une cote ronde était lue comme absente — `328c0ec`

`core/oracle.py` exigeait `\d+\.\d+`, point décimal obligatoire. Or un modèle
sérialise très souvent une cote ronde sans décimale (`"draw": 3`,
`"price": 2`). Le motif ne matchait pas, la fonction rendait `(None, None)`,
et **le prix sharp était perdu en silence** sur le chemin du settlement —
pour la seule raison que la cote tombait juste.

### 3.5 MMA/boxe cherchaient des « compositions » — `f0b620c`

Depuis la refonte du périmètre, MMA et boxe sont sur flux OddsAPI réel, donc
émis, donc analysés par Wiz. `_SPORT_QUERY_A` n'avait pas d'entrée pour eux :
requête générique « team news lineup injuries ». Dans un sport de combat,
« composition » et « absences » n'existent pas — ce qui déplace la cote c'est
la pesée ratée, le remplaçant de dernière minute, le changement de catégorie.

### 3.6 README : un produit qui n'existe pas — `README.md`

Vérification poste par poste contre le code. **Sept éléments annoncés
n'existent nulle part dans le dépôt** : console de log « style Matrix »,
courbes Chart.js, intégration QuantStats, export PDF, ticker de news, ratios
Sortino/Calmar, monitoring BetterStack.

Le plus grave était chiffré : le README annonçait **« Kelly 25 % »** avec la
formule `Mise = Bankroll × (Edge / Odds) × 0.25`. Le code applique 0.08–0.15
selon le sport (`KELLY_FRACTION`). **Faux dans un rapport de 2 à 3.** Sur un
système de mise, un chiffre de documentation faux ne fait pas perdre du
temps : il fait perdre de l'argent.

Également retirés : « Max Drawdown 15 % (hard stop) » et « Stop Loss dynamique
selon volatilité » (aucune constante, aucun code), et le tableau de valeurs
cibles (Win Rate > 65 %, Sharpe > 2.0, Sortino > 2.5, Profit Factor > 2.0)
dont **rien n'était calculé** — `sharpe` n'apparaît que dans un commentaire.

### 3.7 Wiz : le correctif des sources ramenait des faits, le plafond les jetait

Trouvé en **mesurant les sources en réseau réel**, pas en lisant le code.

`core/wiz_sources.py::gather()` fusionne Google News et Bing News, déduplique,
puis tronque à `MAX_TOTAL = 12`. Or `FREE_SOURCES` commence par Google News —
qui couvre presque tous les matchs mais dont **tous** les items sont des
titres nus : sa `<description>` RSS recopie le titre, et `_echoes_title()` en
vide donc le contenu. `keep()` empilant dans l'ordre des sources, la
troncature finale **gardait en priorité ce qui ne porte aucun fait**.

Mesuré sur Espanyol–Real Madrid, deux requêtes, avant correction :

| | items rendus | dont porteurs de faits |
|---|---|---|
| avant | 12 | **5 (42 %)** |
| après | 12 | **10 (83 %)** |

Les 7 titres nus occupaient 58 % du prompt pour n'y annoncer que leur propre
titre, et la troncature faisait tomber **tous** les extraits de la seconde
requête. Un modèle à qui l'on sert majoritairement des titres répond
`INDISPONIBLE` — et c'est la bonne réponse de sa part : on ne lui avait rien
donné à lire.

Correction : tri **stable** « les faits d'abord » avant la troncature. Les
titres nus ne sont pas supprimés (un titre « X forfait » informe), ils passent
après. Gardé par `tests/test_wiz_sources.py::TestLesFaitsDabord`.

> Ceci n'annule pas §5.1 : la cause racine de l'`INDISPONIBLE` en production
> peut être ailleurs (Mistral, quota). Ce défaut-ci est **mesuré et corrigé** ;
> il reste à voir ce que le run de 16:15 UTC produit.

### 3.8 L'erreur de cet audit : « aligner » `.python-version` a cassé le déploiement

À consigner, parce que la leçon vaut plus que l'incident.

Le test neuf `test_une_seule_version_de_python` a signalé que
`.python-version` annonçait **3.12** quand `CLAUDE.md`, `vercel.json`,
l'interpréteur local et les 14 workflows disaient **3.11**. Un fichier contre
cinq : conclusion tirée, « dérive », aligné sur 3.11.

Le déploiement suivant a échoué :

```
Failed to run "uv sync --active --no-dev --link-mode hardlink --locked --no-editable"
error: No interpreter found for Python 3.11 in managed installations or search path
```

`.python-version` est **le seul fichier du dépôt que Vercel lit** pour choisir
son interpréteur, et son image de build n'embarque pas 3.11. Le 3.12 n'était
pas une dérive : c'était la contrainte de la plateforme de déploiement.
Conséquence concrète : la production est restée sur le commit précédent —
donc **sans le correctif de sécurité de `/api/audit/run`** — jusqu'à la
réparation.

**Ce que ça enseigne, au-delà du cas.** Une valeur isolée n'est pas une valeur
fausse. Avant d'aligner un fichier sur la majorité, il faut savoir **qui le
lit**. Ici la majorité (les runners GitHub) et le minoritaire (Vercel) ont
deux lecteurs distincts et deux contraintes distinctes ; les « accorder »
revenait à casser l'un pour faire plaisir à l'autre.

Et surtout : **un test qui encode la mauvaise règle est pire qu'aucun test**.
Il donne l'autorité d'une suite verte à une erreur. Le test a été remplacé par
trois tests qui disent la règle réelle :

- `test_python_version_appartient_a_vercel` — ce fichier reste à 3.12, avec
  le message d'erreur de Vercel cité dans le code ;
- `test_vercel_json_annonce_la_meme_version_que_python_version` — deux
  fichiers de config Vercel qui se contredisent, c'est la prochaine personne
  qui corrige le mauvais des deux ;
- `test_les_workflows_partagent_une_seule_version_de_python` — la règle
  réellement utile : les runners doivent rester d'accord **entre eux**.

État final assumé et testé :

| Lecteur | Version | Fichier |
|---|---|---|
| Runners GitHub + dev local | 3.11 | les 14 `.github/workflows/*.yml` |
| Build Vercel | 3.12 | `.python-version`, `vercel.json` |

Le code doit rester compatible avec les deux.

### 3.8 Le dépôt vit sur DEUX interpréteurs — et un test avait encodé le contraire

**Régression introduite par cet audit même, et la leçon la plus utile qu'il
ait produite.**

Le test neuf `test_une_seule_version_de_python` avait trouvé que
`.python-version` annonçait 3.12 contre 3.11 partout ailleurs. L'alignement
paraissait évident — il a été fait dans le mauvais sens. L'image de build
Vercel **n'embarque pas Python 3.11** :

```
Warning: Python version "3.11" detected in .python-version is not installed
         and will be ignored.
Using python version: 3.12
error: No interpreter found for Python 3.11 in managed installations
```

Le déploiement a échoué et la production est restée bloquée sur le commit
précédent — **donc sans le correctif de sécurité de §3.1**. Une correction de
sécurité non déployée ne protège rien.

La règle réelle, désormais écrite dans le test :

| Fichier | Interpréteur | Qui l'impose |
|---|---|---|
| `.python-version`, `vercel.json` | **3.12** | Vercel — son image de build n'a pas 3.11 |
| les 14 workflows, le dev local | **3.11** | choix du projet |

Ce n'est pas une incohérence à réparer, c'est une contrainte subie.

> **La leçon** : un test qui encode la mauvaise règle ne se contente pas
> d'être inutile — il donne l'autorité d'une suite verte à une erreur. Ici
> 1012 tests au vert accompagnaient un déploiement cassé, parce qu'aucun
> d'eux ne déployait quoi que ce soit. Après toute modification de
> `vercel.json`, `.python-version`, `requirements.txt` ou `api/index.py` :
> **vérifier le déploiement, pas seulement la suite.**
> ```bash
> python scripts/ops.py vercel deployments | head -3   # READY, pas ERROR
> curl -s https://predator-two.vercel.app/api/health
> ```

### 3.9 Époque zéro : le système commence en août 2026

Décision opérateur du 2026-08-22 : « predator n'était pas au point et avait
des bugs en juillet, on recommence tout en août ». Les lignes de juillet ne
mesurent donc pas le système actuel — les garder dans les agrégats revenait à
juger la version d'aujourd'hui sur les erreurs d'une version corrigée depuis.

**Ce qui a été fait, en deux temps qui se complètent.**

*En base* — `sql/migrate_v10_5_archive_pre_august.sql` a déplacé 206 lignes
vers `ai_learning_ledger_archive` : 194 de juillet, plus 12 de sports retirés
encore présents en août. Sept lignes de `signals` (esports, tabletennis,
toutes `settled`/`closed`) sont parties vers `signals_archive`. Il reste 126
lignes vivantes, août uniquement, quatre sports.

C'est un **déplacement, pas une destruction**, conformément à la politique
déjà écrite dans `sql/archive_retired_sports.sql` : ces lignes (cotes, edge
d'entrée, CLV, issue réelle) sont la seule trace empirique du comportement
passé, et « juillet était buggé » est une hypothèse qu'on peut vouloir
re-vérifier sur pièces. Un backtest futur qui ignorerait des paris réglés
souffrirait d'un biais de survie. Le bloc RESTAURATION du script donne le
chemin inverse. Une sauvegarde JSON des 206 lignes a été prise avant
exécution.

*Dans le code* — `PERF_START_MONTH` (`core/perf_view.py`, défaut `2026-08`)
empêche tout mois antérieur de remonter sur /performance, **même si des
lignes étaient réinsérées**. La condition est portée deux fois — dans
`shown_months()` et dans `filter_rows()` — parce que relever
`PERF_MONTHS_SHOWN` pour inspecter un historique ne doit pas ramener juillet
en douce dans les agrégats : il faut abaisser la borne explicitement.

Sans cette borne, la fenêtre glissante afficherait une carte « juillet
0 gagné / 0 perdu » — un mois vide qui ne dit pas « aucun pari » mais
« période exclue ». Afficher 0/0 pour une période volontairement écartée
trompe davantage que de ne rien afficher.

### 3.10 /performance : moins de littérature, même rigueur

Demande opérateur : « il y a trop de littérature et d'informations, mets
juste les infos essentielles ». Quatre sections ont quitté la page — seuils
d'edge appris, dernier cycle d'apprentissage, calibration de Brier par
tranche de confiance, découpage par mois. Ce sont des rouages internes, pas
des résultats ; ils restent mesurés et lisibles ailleurs (table
`brier_scores`, `meta.threshold_<sport>`, `scripts/weekly_report.py`).

**Ce qui n'a PAS été simplifié, et pourquoi.** Le code portait une règle
explicite : *ne jamais afficher un taux de réussite sans son intervalle de
Wilson et le seuil de rentabilité après taxe* — parce que « 3 gagnés sur 4 »
fait 75 % et ne prouve rien. Supprimer cette garde aurait été une régression
de sûreté sur un système de mise, pas une simplification.

Elle est donc **traduite au lieu d'être retirée**. Là où la page affichait
« IC 95% [41.2 – 66.8%] · seuil rentable net taxe 57.0% ✗ pas confirmé »,
elle affiche maintenant :

> **À confirmer** — il faut 57 % de réussite pour être rentable, et 85 paris
> ne suffisent pas encore à le prouver.

Même calcul, même prudence, une phrase que l'on lit sans dictionnaire. Le
tableau par sport suit la même logique : les colonnes « IC 95% » et « seuil
rentable » disparaissent, la colonne **Verdict** qu'elles alimentaient reste.

La table d'emojis codée en dur dans le template (deux copies, divergentes de
`api/index.py`) est remplacée par l'injection — même correctif que sur
`index.html`, même raison (§1). 386 → 286 lignes, 13 règles CSS mortes
retirées, quatre appels Supabase de moins par chargement.

---

## 4. Vérifié sain (ne pas re-diagnostiquer)

Mesuré le 2026-08-22, avec la méthode :

- **Invariant des sport-keys** — les 4 fichiers couvrent les 10 sports actifs,
  0 manquant.
- **Routes ↔ liens ↔ assets** — les 13 routes Flask répondent ; tous les
  `href`/`src` locaux des 6 pages résolvent en 200 ; aucun résidu Jinja ni
  trace d'exception dans le HTML rendu. Les 6 pages portent 6 entrées de nav
  avec le bon état actif.
- **Garde des sports retirés** — dernière émission `esports` le 2026-08-05 et
  `tabletennis` le 2026-08-02, toutes deux **antérieures** au retrait du
  2026-08-22. La garde de `_emit` tient.
- **Les 6 fournisseurs IA non câblés** portent tous un `terms_flag`
  (`payment_required`, `non_commercial`, `evaluation`, `quota_zero`) — ils
  sont exclus de `PRODUCTION_SAFE` **exprès**, ce n'est pas un oubli.
- **Les `terms_flag` du registre sont confirmés PAR L'INFÉRENCE RÉELLE**
  (`python scripts/ops.py ai`, 2026-08-22) — pas par lecture de catalogue,
  qui ne prouve rien. Chaque fournisseur marqué échoue bien comme annoncé :

  | Fournisseur | `terms_flag` | Réponse réelle |
  |---|---|---|
  | cerebras, chutes, sambanova | `payment_required` | **HTTP 402** |
  | scaleway | `quota_zero` | **HTTP 429** INSUFFICIENT QUOTA |
  | cohere, upstage | `non_commercial` / `evaluation` | OK — mais exclus de la production **exprès** |
  | gemini, cloudflare, openrouter, ollama_cloud | *(aucun)* | **OK** |

  Le registre dit donc la vérité sur le terrain. Seule nouveauté :
  `ZHIPU_API_KEY` rend **401 « token expired or incorrect »** — la clé est
  périmée. Impact nul (Zhipu est `non_commercial`, donc hors production), à
  renouveler ou à retirer du `.env` au choix de l'opérateur.
- **`team_aliases`** existe en base (12 lignes) : la migration `v10_3` est
  bien appliquée, contrairement à ce que `CLAUDE.md` laissait entendre.
- **Aucun TODO/FIXME/HACK** dans le code de production.
- **14 workflows, YAML valide**, tous bornés en durée, tous en Python 3.11
  (Vercel, lui, construit en 3.12 — divergence VOULUE, voir §3.8).
- Les 2 fichiers orphelins de la Phase 1 (`api/static/logo.jpg`,
  `.vercel-build-trigger`) ont bien été supprimés.
- **Vérifié sur la PRODUCTION** (`predator-two.vercel.app`, commit `3934fd5`)
  et non seulement en local : les 6 pages rendent en 200, `/api/health`
  annonce la version unique `10.4` et `db_configured: true`,
  `POST /api/audit/run` sans jeton renvoie bien **401**, et `/api/signals`
  rend **14** signaux jouables contre **37** en `?all=1` — le filtre mesuré
  en base (37 actifs, 23 déjà commencés) se retrouve exactement à l'écran.

---

## 5. Ouvert / non vérifié

Honnêteté du document : ce qui suit n'est **pas** réglé.

### 5.1 Wiz : requêtes refondues et mesurées — reste à confirmer en run réel

**État au 2026-08-22, fin de passe.** La cause racine du 100 %
d'INDISPONIBLE a été trouvée, corrigée et **mesurée**. Ce qui manque encore,
c'est la confirmation par un run réel : le quota Mistral n'est pas disponible
en local, donc la moitié « le modèle en tire-t-il un verdict ? » n'est pas
vérifiée.

**Le diagnostic.** Le modèle marchait, les sources arrivaient, elles ne
disaient rien — il écrivait lui-même « aucune information exploitable trouvée
dans les sources consultées ». Trois causes, toutes reproduites en local sur
les flux RSS publics (aucune clé requise) :

1. **La requête sélectionnait le bruit qu'elle devait éviter.** « team news
   lineup injuries » est mot pour mot le titre SEO des pages de preview, que
   `_PROMPT_GROUNDED_HEAD` ordonne justement au modèle d'ignorer. Chaîne
   cohérente de bout en bout, incapable de produire autre chose que du vide.
2. **Les termes étaient combinés en ET implicite.** Cinq mots font tomber le
   flux à zéro résultat. Remplacés par un groupe `(motA OR motB OR motC)`.
3. **Vocabulaire anglais pour tous les championnats** — vrai pour la MLS ou
   la NBA, faux pour le Brésil ou l'Argentine, où le fait sort dans la presse
   locale et n'est jamais traduit. Corollaire découvert en mesurant :
   traduire ne suffit pas, **l'édition US du flux n'indexe pas la presse
   hispanophone** (0 item en `en-US`, 5 en `es-419/AR`, requête identique).
   Vocabulaire et locale sont un seul levier.

Retiré aussi : la date ISO passée comme terme de recherche. Aucun article ne
contient « 2026-08-23 » ; et c'était un doublon de `when:Nd`, que
`google_news()` applique déjà côté moteur, là où c'est exact.

**Mesure avant/après** — 7 matchs réels, collecte complète (Google + Bing) :

| | avant | après |
|---|---|---|
| sources totales | 30 | **60** |
| porteuses de faits | 1 (3 %) | **14 (23 %)** |
| matchs sans aucune source | 1 | **0** |

Aucun match ne régresse. Le banc de mesure classe une source en « fait » sur
un vocabulaire de terrain multilingue (absence, suspension, composition,
retour) et en « bruit » sur le vocabulaire de preview (où voir, pronostic,
cote, diffusion).

**Ce qui reste à faire** : lire le prochain run Wiz. Le taux de sources
porteuses de faits est passé de 3 % à 23 %, mais c'est une mesure de la
matière première, pas du verdict. La requête à passer après le run suivant :

```bash
python scripts/ops.py supabase sql \
  "select verdict, count(*) from wiz_analysis \
   where analyzed_at > now() - interval '2 hours' group by 1"
```

Si le taux d'INDISPONIBLE reste à 100 % avec 23 % de sources porteuses de
faits, la cause suivante est dans le prompt ou la validation R4, plus dans la
collecte.

**Indépendant de tout ça** : Tavily rend `HTTP 432` (quota de plan) et le
connecteur `web_search` de Mistral est épuisé. Ni l'un ni l'autre n'était la
cause — la cascade RSS a fonctionné — mais Wiz est privé de ses deux replis.


### 5.2 Deux clés production-safe ne sont pas encore obtenues

`OVH_AI_API_KEY` et `SILICONFLOW_API_KEY` sont désormais **câblées** dans les
7 workflows, mais aucun secret n'existe côté GitHub ni dans le `.env` local.
Le câblage est inoffensif (clé absente = fournisseur ignoré) et deviendra
actif le jour où l'opérateur ouvre les comptes. Rien à faire si ce n'est
souhaité — c'est de la capacité de repli, pas un manque.

### 5.3 `DASHBOARD_ADMIN_TOKEN` doit être posé sur Vercel

Tant qu'il ne l'est pas, `/api/audit/run` refuse (c'est voulu). Aucune
interface ne l'appelle, donc rien d'autre n'est affecté.
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python scripts/ops.py vercel env set DASHBOARD_ADMIN_TOKEN <jeton> production
```

### 5.4 Non traité délibérément

- **Découpage des gros fichiers** (`run_engine.py`, `core/ai_router.py` 1086 l.,
  `core/learning_layer.py` 1085 l., `api/index.py` ~1050 l.) — couverts par
  les tests et stables. Découper un moteur qui gagne de l'argent pour un
  critère de taille : risque > gain.
- **`base.html` Jinja** pour les 6 templates — la duplication du `<head>` et
  de la nav est réelle, mais chaque page a des variations de style voulues.
  À faire seulement si on retouche le dashboard pour une autre raison. La nav
  du bas, elle, est désormais identique partout (vérifiée par smoke test).
- **Entrées mortes de `.env.example`** (`NEWS_API_KEY`, `PERPLEXITY_API_KEY`,
  `BETTERSTACK_*`, `PREDATOR_SECRET`…) — conservées **exprès**, chacune avec
  une mention `⚠️ UNUSED` datée. Elles servent de pierre tombale : sans
  elles, quelqu'un les réintroduit de bonne foi.

---

## 6. Comment refaire cet audit

```bash
python -m pytest tests/ -q                      # doit rester à 0 échec
python -m pyflakes $(git ls-files '*.py')       # doit rester vide
python scripts/ops.py doctor                    # credentials
python scripts/ops.py status                    # santé pipeline en un écran
python scripts/ops.py ai                        # INFÉRENCE réelle par fournisseur
python scripts/ops.py sources                   # chaque source de cotes
```

Le dashboard ne se vérifie **que** par rendu réel — la suite de tests ne rend
aucun template et n'appelle aucune route Flask. Utiliser la skill
`predator-dashboard-check`.

> `python scripts/ops.py ai` est le seul diagnostic qui tranche pour l'IA :
> un catalogue lisible ne prouve rien (Cerebras/SambaNova/Chutes rendent 200
> sur `/models` et 402 à l'inférence ; Scaleway rend 429 quota-zéro).
