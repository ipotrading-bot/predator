# INCIDENTS — ce qui a déjà cassé, et pourquoi

Ce fichier est la MÉMOIRE des pannes de PREDATOR PAIM. Il a été extrait de
`CLAUDE.md` le 2026-08-27 (phase D5) : ce dernier était devenu un fichier de
38 Ko chargé dans chaque session, dont 33 Ko de récit d'incidents. Un fichier
de consignes qu'on ne lit plus en entier ne donne plus de consignes.

Rien n'a été résumé ni raccourci dans le déplacement : ces textes sont chers,
chacun a été payé par une panne. Ils sont seulement regroupés par thème.

**À LIRE AVANT DE DIAGNOSTIQUER** quoi que ce soit dans ce dépôt, et avant de
toucher aux sources de cotes, à la couche IA, aux workflows ou aux seuils. Les
règles qui ne se discutent jamais sont rappelées en tête de `CLAUDE.md` ; leur
JUSTIFICATION est ici, et une règle dont on ignore la raison finit contournée.

La carte des invariants et de leurs tests gardiens vit dans `AUDIT.md`.

## Sommaire

- [Le moteur : prix, edge, seuils](#le-moteur-prix-edge-seuils)
- [Sources de cotes](#sources-de-cotes)
- [Règlement, CLV, apprentissage](#reglement-clv-apprentissage)
- [Couche IA](#couche-ia)
- [CI, secrets, déploiement](#ci-secrets-deploiement)
- [Dashboard et base](#dashboard-et-base)
- [La règle transverse](#la-regle-transverse)


## Le moteur : prix, edge, seuils

### TAX_RATE remis à 0.20 contre instruction opérateur → émission fermée (2026-09-01)

Chronologie :
- 2026-07-08 : l'opérateur fixe `TAX_RATE = 0.0` (« les 20 %, on s'en soucie
  plus »). Période à 61 % de réussite.
- 2026-08-22 : `_emit` (run_engine.py) refuse en dur tout signal dont
  `tax_engine.optimal_stake_fraction` rend une mise Kelly nulle.
- 2026-08-27 : une session remet `TAX_RATE = 0.20` CONTRE l'instruction
  opérateur et écrit `tests/test_taxe_reelle.py` pour le verrouiller.
- 2026-08-27 → 2026-09-01 : le moteur n'émet quasi plus rien — « Aucun pari
  de valeur · 17-50 matchs analysés » sur tous les scans du 2026-09-01.

Mécanique : la taxe entre dans le b de Kelly. À proba 0,60 un signal exige
+10 % d'EV brut (= le plafond `SUSPECT_EDGE`), à 0,70 → +7,6 %, à 0,80 →
+5 %. Les edges sharp-vs-soft réels font 1,5-4 %. Fenêtre d'émission fermée.
Vérifiable : `optimal_stake_fraction(0.62, 1.68, tax_rate=0.20,
kelly_multiplier=0.12)` → 0.0 ; à `tax_rate=0.0` → ~0.0073.

Correction du 2026-09-01 : `TAX_RATE = 0.0` (core/constants.py), aucun garde
réintroduit à la place. Les tests qui encodaient le 0.20 passent le taux
explicitement quand ils testent une propriété de la formule à taux non nul.

⛔ **Règle : `TAX_RATE` est une décision opérateur. Une session ne la change
pas, même « pour le réel ».** Il en va de même de `SHADOW_SPORTS` et du
périmètre sportif (CLAUDE.md, règle 11). Gardien :
`tests/test_taxe_reelle.py::TestUnSeulTaux::test_le_taux_est_celui_decide_par_l_operateur`.

Les corrections les plus coûteuses de ce dépôt sont ici. Toutes ont la même
forme : le moteur mesurait quelque chose de VRAI sur un objet FAUX.

### La ligne comparée doit être la MÊME ligne — et A6 est tranchée (2026-08-27)

ET A6 EST TRANCHÉE (2026-08-27).
A1 avait corrigé le PRIX du h2h. Restait l'anomalie : pourquoi le football
spreads/totals sortait-il encore des edges à +12 % quand le h2h s'effondrait
à −4,7 % ? Réponse : sur ces marchés, ce n'était pas le prix qui était faux,
c'était le PARI. La garde anti-lignes-divergentes existait dans
`_process_totals` ET `_process_spreads` — deux copies, avec des défauts
différents. Celle des spreads en portait TROIS :
  1. `if xs_line and ps_line` — **0.0 est faux en Python**. Une ligne AH 0.0
     d'un côté DÉSACTIVAIT la garde entièrement. C'est le cas le plus
     fréquent du football.
  2. Une tolérance de 0,5 — AH −1,0 contre AH −1,5 passait. Sur un handicap,
     une demi-unité change le pari.
  3. `abs(abs(x) - abs(p))` — le double `abs` DÉTRUIT LE SIGNE : −0,5 contre
     +0,5 passait. On comparait le prix du FAVORI chez un book à celui de
     l'OUTSIDER chez l'autre. L'écart est énorme et ressemble toujours à un
     edge.
Symptôme qui aurait dû alerter : les 7 refus `LINESKIP` du premier run du
moteur corrigé portaient TOUS sur des totals, pas un seul sur un spread —
alors que les deux seuls signaux émis étaient des spreads (« SOC PS -0.0 »
et « SOC PS -1.0 », +12,00 % et +12,28 %). Une garde qui ne refuse jamais
rien n'est pas une garde.
MESURÉ sur le slate réel le 2026-08-27 : **21 paires de spreads sur 24**
avaient des lignes différentes, **dont 20 passaient l'ancienne garde** ;
19 totals sur 24, dont 9 passaient. Effet sur la distribution des edges :

    AVANT  n=74  p50 −5,86  p90 +9,18  max **+13,88**  →  29 lignes ≥ +1,5 %
    APRÈS  n=16  p50 −5,43  p90 −3,31  max **−2,30**   →   0 ligne  ≥ +1,5 %

Toute la queue positive du football spreads/totals était l'écart de prix
entre deux paris différents. La règle est maintenant l'ÉGALITÉ EXACTE, signe
compris, par une garde UNIQUE (`run_engine._meme_ligne`) partagée par les
deux marchés — deux copies d'une même règle finissent toujours par diverger,
c'est exactement ce qui s'est produit ici. Une ligne ABSENTE fait REFUSER :
on ne peut pas vérifier qu'on compare le même pari sans la voir (même
contrat que le football sans prix de nul).
⚠️ CONSÉQUENCE SUR A6, ET C'EST LA RÉPONSE À LA QUESTION DES SEUILS : après
cette correction, le football n'a plus **aucune** ligne positive, ni en h2h
(max +0,00 %) ni en spreads/totals (max −2,30 %). L'émission football est
nulle SANS TOUCHER À UN SEUL SEUIL. Les 14,5 / 12,5 proposés plus haut sont
donc SANS OBJET : ils n'ajouteraient rien à zéro, et ils poseraient un
plancher d'émission AU-DESSUS du plafond de suspicion — une fenêtre vide par
construction, 19 tests par terre dont 7 non réécrivables honnêtement.
**DÉCISION : aucun seuil numérique n'est modifié.** MIN_EDGE, EV_EDGE_FLOOR,
SUSPECT_EDGE et `_EDGE_CEILINGS` restent où ils sont. Ce qui a supprimé
l'émission, c'est la correction du PRIX et du PARI, pas un durcissement de
garde — et c'est la bonne façon : un seuil relevé masque un mécanisme faux,
il ne le répare pas.
⚠️ Et la recalibration reste À FAIRE, pas faite : le ledger ne contient que
des lignes de l'ANCIEN moteur. Un p99 calculé dessus décrirait une
distribution que le moteur ne produit plus — exactement l'erreur qu'A6
interdisait (« il se re-mesure, il ne se convertit pas »). Il faut des
réglés post-correction, et le piège de la sortie tient toujours : à volume
nul, aucune bande n'atteindra jamais n ≥ 30.
Gardien : `tests/test_prix_executable.py::TestMemeLigne` (les trois défauts,
nommés un par un) et `::TestUnSpreadNemetPlusSurUneLigneDifferente`.

### Deux carnets tronqués, ce n'est pas un désaccord de marché (2026-08-27)

A6 a posé la bonne règle — on ne compare que la MÊME ligne, signe compris — et
l'émission totals/spreads est tombée à zéro derrière. On a d'abord lu ça comme
le résultat sain de la correction. C'était vrai à moitié : la moitié de la
divergence que `_meme_ligne` refusait était FABRIQUÉE trois fichiers plus haut.

Chaque source cote une douzaine de lignes. `core/odds_api_io.py` et
`core/matchbook.py` n'en gardaient qu'UNE — « la plus équilibrée en prix » —
chacune calculée sur SON carnet, sans rien savoir de l'autre. Rien n'oblige un
book soft à équilibrer sa cote sur le handicap où l'exchange équilibre la
sienne : les deux choix tombaient à côté l'un de l'autre, et la paire était
refusée alors que la ligne du sharp était cotée chez le soft aussi. On venait
juste de la jeter.

MESURÉ le 2026-08-27 sur les matchs communs à odds-api.io et Matchbook —
échantillon mince, parce que le recouvrement des deux slates l'est (voir
« odds-api.io : le plan autorise DEUX books ») :

    AVANT  (19:5x, 11 matchs soft, 2 appariés)  totals 1 sur 2   spreads 0 sur 2
    APRÈS  (20:05,  6 matchs soft, 2 appariés)  totals 2 sur 2   spreads 2 sur 2

Et en amont de la garde, ce qui ATTEINT le moteur passe de 0 total et
0 spread — pas un seul, tout le run du 19:20 — à 2 et 2 sur ce même
échantillon. Le premier chiffre est le vrai sujet : la garde n'était pas
franchie, elle n'était pas atteinte.

Les deux sources gardent désormais leur échelle entière (`ladder`), et
`run_engine._aligner_sur_meme_ligne` y cherche la ligne réellement commune
avant que `_meme_ligne` ne tranche. **Aucune garde n'est relâchée** : la règle
reste l'égalité exacte, `_meme_ligne` reste seul juge, et sans ligne commune le
refus a lieu comme avant. Aucun seuil n'est touché.
⛔ Ce qui est choisi parmi les lignes communes : celle du sharp, sinon la plus
proche. JAMAIS la mieux payée — parcourir une échelle en retenant la ligne au
plus gros edge, c'est retenir la plus grosse erreur de cote, exactement la
queue positive qu'A6 a identifiée comme un artefact. Le sharp désigne la
ligne, le soft ne fournit que son prix dessus.
⚠️ Et `core/exchange_match._retourner_spread` s'applique à CHAQUE barreau : un
seul barreau laissé dans l'ancien sens ferait croire à l'alignement qu'il a
trouvé la ligne commune, sur le handicap OPPOSÉ — le défaut n° 3 d'A6 rentrant
par la porte de derrière.

### Le second book soft ne servait qu'au 1X2 (2026-08-27)

Troisième défaut du même jour, dans `core/odds_api_io._to_match` : le line
shopping entre books soft ne portait que sur `("1","X","2")`. Les handicaps et
totaux du second book étaient jetés, et si le PREMIER n'en cotait aucun, le
match repartait sans spread ni total du tout. Le plan gratuit autorise deux
books (voir la section sur les sources) : la moitié de la couverture soft
disponible se perdait là.
`_line_shopping` compare désormais les trois marchés — mais À LIGNE ÉGALE,
barreau par barreau. Retenir le meilleur prix toutes lignes confondues
reviendrait à choisir un AUTRE pari parce qu'il est mieux payé : l'artefact
exact qu'A6 a supprimé.

### La contre-expertise jetait les totals/spreads du sharp (2026-08-27)

`_enrich_from_exchange` a deux rôles. En BOUCHE-TROU (match sans prix sharp) il
posait `totals_pinnacle`/`spreads_pinnacle` depuis l'exchange. En
CONTRE-EXPERTISE (match qui a déjà un h2h sharp) il faisait `continue` juste
avant — donc dès qu'un match avait un Pinnacle h2h, ses totals et handicaps
repartaient SANS référence sharp, et `_process_totals`/`_process_spreads`
n'étaient jamais appelés faute des deux côtés.
Symptôme qui aurait dû alerter : sur le scan du 2026-08-27 19:20, **zéro**
ligne `LINESKIP` dans tout le log — pas un refus, parce que pas une seule
évaluation. Le même run avait chargé 18 marchés Matchbook, puis 105 au reprice
suivant dont 64 totals et 51 handicaps : tous jetés.
Matchbook est la SEULE source de totals/spreads sharp du stack (titan007 ne
sert que du h2h, odds-api.io ne sert aucun book sharp sur le plan gratuit).
Les deux rôles passent maintenant par `_poser_lignes_sharp`, et le compte est
loggé — un enrichissement qu'on ne voit pas dans les logs est un
enrichissement qu'on croira acquis le jour où il retombera à zéro.

### A6 — la calibration n'a pas pu avoir lieu, et c'est le résultat (2026-08-27)

LA CALIBRATION N'A PAS PU AVOIR LIEU, ET C'EST LE RÉSULTAT (2026-08-27).
Méthode imposée : bucketiser les lignes RÉGLÉES par EV recalculée au prix
exécutable, retenir la bande la plus basse où le ROI réalisé NET DE TAXE est
positif ET dont la borne basse de Wilson dépasse le point mort, avec n ≥ 30.
**AUCUNE BANDE NE QUALIFIE.** Sur 114 réglés (WIN/LOSS), une seule bande
atteint n ≥ 30 — la plus basse (EV < −7,5 %, n=37) — et elle est PROUVÉE
PERDANTE : 40,5 % de réussite pour 70,9 % requis, ROI net −37,3 %. Les huit
autres bandes ont n entre 1 et 20 : ni prouvées rentables, ni prouvées
perdantes. On ne conclut pas, et on n'invente pas un seuil pour que le
moteur émette.
Le football au prix exécutable, mesuré : h2h **p95 = −4,70 %, max = +0,00 %**
— pas une seule ligne n'atteint le plancher actuel de 1,5 %, donc l'émission
h2h est DÉJÀ nulle depuis A1, sans toucher à un seul seuil. Ce qui émet
encore, c'est le football totals/spreads : 54 lignes sur 54 passent 1,5 %
(leur cote soft est brute, A1 ne les touche pas), pour un ROI net mesuré de
**−11,2 %** sur les 19 réglés. ⚠️ CETTE DERNIÈRE PHRASE A ÉTÉ EXPLIQUÉE LE
MÊME JOUR, ET C'ÉTAIT UN BUG — voir le point suivant : ces edges comparaient
DEUX PARIS DIFFÉRENTS.
⛔ SEUILS PROPOSÉS, NON APPLIQUÉS — ils demandent un arbitrage. MIN_EDGE et
EV_EDGE_FLOOR à 14,5 % (au-dessus du max football observé, +14,22 %) et
SUSPECT_EDGE à 12,5 % (p99 de la nouvelle distribution, exprimé en
percentile pour qu'un futur changement d'unité ne le laisse pas sur place).
Leur conséquence est ARITHMÉTIQUE et il faut la voir avant de les poser : un
plancher à 14,5 au-dessus d'un plafond de suspicion à 12,5 rend la fenêtre
d'émission h2h VIDE par construction, pour tout sport majeur. Il n'existe
aucune valeur d'edge qui passe les deux gardes. Essayé le 2026-08-27 : 19
tests tombent, et 7 d'entre eux ne peuvent pas être réécrits honnêtement —
il faudrait un edge que le moteur signale lui-même comme erreur de données.
C'est la vraie conclusion de la phase A : après correction du prix, de la
taxe et du point mort, **il ne reste aucun écart entre « assez rentable pour
parier » et « si élevé que c'est probablement une erreur de données »**.
⚠️ Et le piège de la sortie : suspendre l'émission suspend aussi la collecte.
Une seule bande atteint n ≥ 30 aujourd'hui ; à volume nul, aucune n'y
arrivera jamais. Décider entre « on s'arrête » et « on paie une perte connue
pour acquérir la mesure » est une décision d'opérateur, pas de calibration.
`_EDGE_CEILINGS` : rien à poser. Aucune bande n'est prouvée perdante
AU-DESSUS de zéro, par sport. Vérifié en base le même jour : `meta` ne porte
aucune clé de plafond, ils étaient déjà vides en production. Le constat
« soccer au-dessus de 6 % perd » appartient à l'ANCIENNE unité et ne se
convertit pas — il se re-mesure.
Outil : `python scripts/replay_ledger_executable.py` (lecture seule) rend la
table des bandes, le seuil proposé, le p99 et les plafonds par sport.
Gardien de la méthode : `tests/test_replay_ledger_executable.py::TestCalibrationA6`.

### /performance est volontairement DÉPOUILLÉE (2026-08-22)

plus de seuils
appris, ni de cycle d'apprentissage, ni de calibration Brier, ni de
découpage mensuel — rouages internes, pas résultats. (Le découpage mensuel
est REVENU le 2026-09-03, demande opérateur, une fois qu'il y a eu deux mois
à montrer : cartes PAR MOIS avec Wilson + point mort, historique limité au
mois choisi dans un menu déroulant, tableau PAR LIGUE « perdantes d'abord »
— `core/perf_view.monthly_summary/league_breakdown/pick_month`, gardien
`tests/test_perf_mois_ligues.py`. Le menu ne propose que `shown_months()` :
il ne rouvre pas juillet. Même jour, second lot : le BANDEAU ne compte plus
que les paris RECOMMANDÉS — `perf_view.recommended_rows`, zone T-2h…T-24h
plus l'inconnu, comme `playable_rows` — et les fantômes golden hour sont
rendus À PART. Mesure qui l'a imposé : septembre affichait 52 % pour 4–0 sur
les recommandés et 8–11 sur des fantômes que personne n'avait joués ; août
110–68 de fantômes contre 75–44 recommandés. Un tableau PAR MARCHÉ s'ajoute,
parce que les pertes se concentraient par marché — spreads extérieur et
unders basket — plus que par ligue.) Mais la règle « jamais
un taux de réussite nu » TIENT TOUJOURS : Wilson + seuil rentable après
taxe sont désormais rendus en une phrase française (« il faut 57 % pour
être rentable, et 85 paris ne suffisent pas à le prouver »). Ne pas
supprimer cette ligne en croyant simplifier : c'est une garde de sûreté.

### Époque zéro = août 2026 ; juillet est ARCHIVÉ, pas perdu

`core/perf_view.PERF_START_MONTH` masque tout mois
antérieur sur /performance, et juillet 2026 a été ARCHIVÉ en base
(`sql/migrate_v10_5_archive_pre_august.sql` : 206 lignes vers
`ai_learning_ledger_archive`, 7 signaux vers `signals_archive`). Décision
opérateur : « predator n'était pas au point en juillet ». Ne pas
« réparer » l'absence de juillet ; pour le rouvrir il faut restaurer
l'archive ET abaisser PERF_START_MONTH — les deux, sinon rien ne s'affiche.
Archiver, JAMAIS supprimer sèchement : ces lignes sont la seule trace
empirique et tout backtest qui les ignorerait aurait un biais de survie.


## Sources de cotes

Une source qui « répond » ne porte pas forcément un prix, et une source qui
échoue ne le fait presque jamais bruyamment.

### Sources de cotes : lesquelles portent RÉELLEMENT un signal

la règle est « authentifié par clé = joignable, sinon filtré
par IP depuis les runners » (LineFeed/ESPN/SofaScore sont morts ; 1xbet rend
203, 22bet 404 — le harvest soft direct ne ramène plus rien). `ops.py sources`
les sonde. Depuis l'obsolescence d'OddsAPI, les seules sources qui portent
RÉELLEMENT des signaux sont api-sports (foot : ~36 matchs, 100 % avec sharp
Pinnacle) et titan007 (~21-35 matchs, ~19-31 sharp) ; odds-api.io fournit du
SOFT PUR (100-150 matchs, ZÉRO sharp — de la donnée, pas de l'edge) ;
Matchbook fournit du SHARP PUR gratuit et illimité (141-202 marchés) et
produit pourtant 0 signal. ⚠️ CE N'EST NI UN PROBLÈME DE COUVERTURE NI DE
NOMS — l'hypothèse « seuls ~6 % s'apparient » était FAUSSE, vérifiée en
direct le 2026-08-26 : Matchbook cote bien les ligues du slate (7 matchs
d'OBOS-ligaen, 3 d'Argentine B ce jour-là), et les 4 signaux du run de
09:47 (Moss, Stabæk, Acassuso, Liniers) avaient TOUS leur marché
Matchbook ; `strict_team_match` les apparie correctement, diacritiques
norvégiennes comprises. La vraie cause est `_enrich_from_exchange`
(`run_engine.py`) : `if pin["1"] > 1.01 and pin["2"] > 1.01: continue` —
Matchbook n'est consulté que sur les matchs SANS prix sharp. api-sports
livrant Pinnacle sur 100 % de ses matchs foot, il est écarté précisément
sur ceux qui portent les signaux. Il est câblé en BOUCHE-TROU, pas en
CONTRE-EXPERTISE. Le gisement est là : un 2e avis sharp indépendant
détecterait le Pinnacle PÉRIMÉ, qui est la fabrique à faux edge — et sa
couverture du slate est sans commune mesure avec celle de
Kalshi/Polymarket (3 fixtures exploitables sur 70). Mesuré sur
10 runs du 2026-08-23 au 26. Corollaire : 100 % des signaux sont du FOOTBALL.

### Le Tier 2 entier sautait dès qu'OddsAPI rendait UN event (2026-09-02)

Effondrement du volume émis : ~26 signaux/jour avant le rallumage OddsAPI du
2026-09-01, **4/jour** après — alors que le rallumage devait AJOUTER une
source, il en a débranché cinq. Repéré par l'audit complet du 2026-09-02, en
croisant trois angles : le ledger montrait une perte EN AMONT des seuils
(bande d'edge [1.2;1.66) vide, aucun `threshold_*` en meta — donc pas un
resserrement appris), et le pipeline en vif a trouvé l'organe.
Cause mécanique : dans `run_engine.py`, le bloc Tier 2 (harvest, api-sports,
odds-api.io, titan007, sevenm) était gardé par `if not tier1_ok and not
REPRICE:` — il ne tournait que si OddsAPI n'avait RIEN rendu. Or pendant
l'US Open + la trêve internationale, le Tier 1 rendait du tennis sans edge :
`tier1_ok=True`, et le foot hors-Europe — qui portait TOUT le volume —
n'était plus jamais scanné.
MESURÉ le 2026-09-02 : run 33551932260 du 01/09 19:51 — « Tier 1 OK — 50/78
events | sports: tennis », 66 candidats TOUS à EV négative, 0 signal, zéro
ligne Tier 2 au log. odds-api.io : 289 requêtes le 01/09 contre 368 la
veille. Les seuls ticks productifs du 01-02/09 étaient ceux où le Tier 1
était revenu vide.
⚠️ MÊME CLASSE DE BUG, TROISIÈME OCCURRENCE. La garde a d'abord porté sur
`matches` (run 30768093911 : UN combat UFC suffisait à masquer le foot),
puis a été « corrigée » en la déplaçant sur `tier1_ok` — qui a reproduit le
piège à l'échelle du tier entier. La leçon générale : conditionner un étage
de sources au succès d'un autre finit TOUJOURS par affamer celui qui
produit. Chaque source a son budget journalier propre, la fusion déduplique
par nom de match en aval — il n'y a RIEN à économiser en les couplant.
Correctif : le Tier 2 tourne à CHAQUE tick, seul REPRICE saute le bloc
(`if not REPRICE:` — REPRICE est le mode « zéro source payante », c'est son
contrat, pas une économie déguisée). Étiquette `sharp_source` honnête :
« OddsAPI+Tier2 » quand les deux tiers contribuent. Dans la même passe
d'audit, quatre silences ont été levés : les gates `sharp_prob < prob_min`
des totals/spreads loggent désormais LOWPROB comme le h2h (ils jetaient en
silence — coût immesurable jusqu'ici), les jambes sans prix sortent en
`log.debug` NOPRICE, la purge logge ses destructions en `info` avec compte
(elles n'étaient visibles qu'en DEBUG_MODE), et la règle de purge morte
`status=pending` est retirée (rien n'écrit jamais ce statut). Enfin
`was_clv_positive` est aligné strict (`clv > 0`) entre `backfill_ledger.py`
(qui faisait `>=`) et `core/db.py`.
Ce qui n'a PAS été fait : aucun budget n'a été touché. Le retour du Tier 2
sur tous les ticks re-expose au problème connu des budgets
odds-api.io/titan007 brûlés avant le soir (runs stériles des 29-31/08) — à
REMESURER maintenant que les deux tiers cohabitent, avant de retoucher quoi
que ce soit au rythme.
Gardien : `tests/test_tier2_toujours.py` — 6 tests sur le source (style
`test_api_admin_auth`) : aucune garde `not tier1_ok`, bloc Tier 2 borné par
REPRICE seul, LOWPROB présent dans `_process_totals`/`_process_spreads`,
pas de retour de `pending`, PURGE loggée en `info`.

### Matchbook : quatre marchés « total » par match, un seul est le match entier (2026-08-28)

REPRICE 15:58 : « Al-Riyadh SC vs Neom SC | SOC Under 2.5 — EV 80.84 % »
(et 58 %, 70 %, 78 % sur d'autres totals du run), juste après « ALIGNE …
soft +2.75 / sharp +2.50 : les deux cotent +2.50 ». Refusé par `MAX_EDGE`,
donc sans dégât — mais un edge à 80 % sur une ligne alignée est un signe, pas
du bruit.
Relevé sur l'API le jour même : un match de football porte QUATRE marchés de
`market-type: total` — **« Total », « 1st Half Total », « Home Team Total
Goals », « Away Team Total Goals »** — et les runners s'appellent tous
« OVER 2.5 » / « UNDER 2.5 ». `_totals_odds` les fusionnait dans une seule
échelle : le point 2.5 y figurait deux fois (under 2.68 et under 1.21).
`run_engine._aligner_sur_meme_ligne` indexe l'échelle par point, le dernier
gagne : le moteur comparait le Under 2.5 du match entier chez le soft au
Under 2.5 de la PREMIÈRE MI-TEMPS chez l'exchange. Deux paris différents
comparés — l'artefact d'A6, revenu par le nom du marché au lieu du signe de
la ligne. Le handicap a la même porte (« 1st Half Handicap »).
Correction : `matchbook._est_sous_marche` — un marché dont le nom porte un
qualificatif de sous-marché (half, 1st/2nd, team, quarter, period, corner,
card, booking) est écarté des totals ET des handicaps. Un marché sans nom
reste accepté : perdre la seule source de totals sharp du stack sur une clé
absente coûterait plus qu'un doublon.
⚠️ Ces edges refusés n'ont jamais atteint le ledger ; mais tout signal
totals/spreads « Matchbook » émis AVANT cette date est suspect de la même
confusion si sa ligne existait en mi-temps. À garder en tête pour la
recalibration A6.
Gardien : `tests/test_matchbook.py::TestLesSousMarchesNeSontPasLeMatchEntier`.

### Étaler le SETTLEMENT était une faute — corrigé le 2026-08-28

Le rythme de dépense posé la veille avait été appliqué sans distinction. Il
n'a de sens que pour les SCANS : un scan tardif vaut mieux qu'un scan
matinal, parce que le slate européen entre dans la zone jouable le soir. Un
AUDIT, lui, règle des signaux dont le match est DÉJÀ joué — le reporter ne le
règle pas mieux, il le laisse sortir en `expired`.
Mesuré le 2026-08-28 à 01:15 : `search_credits_left()` rendait **6** (le
plancher du rythme) contre `CLV_CREDIT_RESERVE` = 12, et l'audit sautait —
« CLV SKIP | … 6 crédits restants réservés au settlement ». La réserve
existe pour empêcher exactement cette famine, et le rythme la recréait par
en dessous.
⛔ `search_exhausted()` et `search_credits_left()` n'ont qu'UN consommateur,
`core/audit_engine`, et il s'en sert pour décider d'écrire un état TERMINAL.
Elles répondent donc désormais « le settlement peut-il encore chercher ? » —
budget entier du jour, **sans rythme, sans dépendance à l'heure**.
⚠️ ET LE DRAPEAU EST LEVÉ DANS `audit_engine.run()`, PAS dans `run_audit.py`.
L'audit a plusieurs points d'entrée (cron, `/api/audit/run`) ; un seul qui le
pose est un oubli qui attend son tour.
⚠️ Symptôme à reconnaître : une suite VERTE le soir et ROUGE le matin. Six
tests sont tombés au passage de minuit — un test qui dépend de l'heure réelle
est pire qu'un test absent, il donne une confiance fausse la moitié du temps.
Gardien : `tests/test_tavily_budget.py::TestVueSettlementSansRythme` — dont
`test_et_ils_depassent_la_reserve_CLV_des_le_matin`, qui compare au vrai
`CLV_CREDIT_RESERVE` plutôt qu'à un nombre recopié.

### QUATRE sources, la même panne : le budget du soir était mangé le matin (2026-08-27)

Découverte quatre fois de suite dans la même soirée, en cherchant pourquoi
« pauvreté de signaux » alors que le moteur tournait et qu'aucun seuil
n'avait bougé. Chaque source avait un compteur qui l'empêchait de DÉPASSER
son plan. Aucune n'avait de quoi dire QUAND le dépenser.

    api-sports    budget de SCAN 64/64 dès 19:20  → slate sharp Tier 2 42 → 25
    odds-api.io   « budget journalier atteint (400/400) » à 20:05, 408/400 le soir
    Tavily        plan MENSUEL épuisé — compteur JAMAIS partagé entre les runs
    Groq          TPD des deux organisations à sec dès 18:10

Le cas Tavily est le pire et mérite d'être nommé : `_tavily_used` est un
global de MODULE, donc remis à zéro à chaque processus, avec un budget de 25
par run. Le plan gratuit fait 1 000 crédits par MOIS et ce dépôt lance ~40
runs par jour : jusqu'à 1 000 crédits par JOUR contre un plan MENSUEL de
1 000. « Tavily: plafond de PLAN atteint (HTTP 432) » n'était pas un pic
d'usage, c'était l'état permanent. `core/daily_quota.py` existe exactement
pour ça et son docstring cite api-sports et odds-api.io — pas Tavily, qui n'y
avait jamais été câblé.
⚠️ CE QUE ÇA COÛTAIT, ET C'EST LE VRAI SUJET : Tavily est l'ÉTAGE 2 de la
recherche de prix Pinnacle, groq/compound-mini l'ÉTAGE 1. Les deux mouraient
ENSEMBLE, chaque soir, faute d'être rationnés l'un comme l'autre — d'où
« Pinnacle/Search: 0/25 prices received ». Sur 5 runs : **141 refus « Échec
prix Sharp »** (dont 81 en tennis, structurellement sans source sharp),
57 `LINESKIP`, 19 `CONFLIT SHARP`, et **zéro** `PLAFOND`, **zéro** `SUSPECT`.
Aucune garde d'edge ne refusait quoi que ce soit : elles n'étaient pas
atteintes, faute de candidats. Baisser un seuil n'aurait rien changé.
Remède unique, posé UNE fois : `daily_quota.paced_allowance(budget, floor)`
n'ouvre à chaque heure que la fraction du budget correspondant à la journée
UTC écoulée, avec un plancher d'un cycle pour que le premier run du jour
parte toujours. Aucun budget n'est augmenté — api-sports a déjà coûté une
SUSPENSION de compte le 2026-08-20.
⛔ AUCUNE COPIE. La formule vit dans `core/daily_quota.py` et nulle part
ailleurs : quatre sources l'appellent, et `tests/test_rythme_des_sources.py`
::`test_le_rythme_vit_a_UN_seul_endroit` échoue si quelqu'un la recopie
(règle dure n°6 — la panne la plus fréquente de ce dépôt).
⚠️ Réserve du settlement partout, tenue EN NÉGATIF : `prioriser_settlement()`
n'est levé que par `run_audit.py`, jamais par un scan. C'est un drapeau de
PROCESS et non une variable d'env — l'env est posé par les workflows, et un
scan qui en hériterait mangerait la réserve en silence. Un signal dont le
score n'a pas pu être cherché sort en `expired` et n'apprend plus rien.
⚠️ Budget épuisé ≠ abandon : l'étage 1 rationné SAUTE vers Tavily. C'est tout
l'intérêt d'avoir deux étages, et c'est ce qui ne se produisait jamais.
Gardien : `tests/test_rythme_des_sources.py` — il DÉRIVE la liste des sources
budgétées et exige que chacune consulte le rythme ; une cinquième source
ajoutée sans lui fait échouer la suite. Les exemptions (odds500 filtrée par
IP, 7M qui ne cote rien, Kalshi/Polymarket qui n'émettent jamais) y sont
nommées avec leur motif.

### titan007 coupait à 40 par ORDRE D'HEURE — les grosses affiches n'entraient jamais (2026-08-28)

Vendredi soir, scans golden 17:00 et 17:38 : 13 matchs avec prix sharp, tous
de divisions mineures (Eerste Divisie, Challenge League, Pologne D4, Chili),
0 signal, EV −3 à −9 % partout. Pourtant ce soir : Palace–City, Bayern–
Stuttgart, Milan–Venezia, Lille–PSG, Alavés–Villarreal, Rio Ave–Sporting.
Le moteur ne les voyait QUE côté odds500 (mode ombre, 5/100 mesurés — pas un
levier avant des semaines) sous « alias : budget IA atteint ».
Mesuré sur le calendrier titan007 à 24 h : 238 matchs ; `upcoming.sort(
key=kickoff)` puis `[:40]`. Les positions 0-57 sont TOUTES des coups d'envoi
17:45-18:00 (U21 anglais/belges, Welsh PR, Pologne D3, INT CF…) ;
Bayern–Stuttgart (18:30) est en **position 62**. odds-api.io a la même
signature (`limit=60`, les 60 premiers par heure : 51 U21/U20 et divisions
inférieures). api-sports, lui, était éteint par le rythme de dépense (54/64,
réouverture ~20:37 — après les coups d'envoi). Matchbook AVAIT Bayern,
Milan, Al-Hilal, Boca dans ses 100 marchés — mais `_enrich_from_exchange`
n'enrichit que le slate soft : un sharp sans soft en face n'entre jamais.
Ce n'est ni un seuil ni un alias : c'est l'INGESTION qui servait la masse,
et la masse d'un vendredi 18:00 est du football de comté.
Correction : `source_adapter.LEAGUE_PRIORITY` (rang par liquidité du sharp)
et `league_rank(label)` ; titan007 trie par `(rang, heure)` avant le cap.
Rejoué sur le calendrier réel du soir : les 13 premiers du nouveau tri sont
les 13 affiches ci-dessus, toutes hors du cap auparavant. **Zéro requête de
plus, aucun seuil touché** (règle 10). Les libellés titan (« GER D1»,
« ENG PR »…) rejoignent `LEAGUE_MAP` — une seule table de libellés, et un
test refuse toute clé de priorité qu'aucun libellé ne produit (règle 6).
⚠️ Non fait, faute de pouvoir le vérifier hors CI (clé odds-api.io absente
du `.env`) : le même tri chez odds-api.io demanderait `limit` > 60 sur
`/events` — 1 requête quelle que soit la limite d'après le coût mesuré, mais
rien ne dit que le serveur honore 240. À sonder depuis un run.
Gardiens : `tests/test_titan007.py::test_les_ligues_majeures_passent_avant_le_cap`,
`tests/test_source_adapter.py::TestPrioriteDeLigue`.

### Le budget api-sports partait PREMIER ARRIVÉ, PREMIER SERVI (2026-08-27)

Symptôme opérateur : « pauvreté de signaux » le soir, alors que le moteur
tournait, que la suite était verte et qu'aucun seuil n'avait bougé. La
tentation était de baisser un plancher d'émission. C'était le mauvais organe :
ce n'est pas la garde qui refusait, c'est le SLATE SHARP qui s'effondrait.

Mesuré sur les scans du 2026-08-27 — la colonne de droite est le nombre de
matchs portant un prix sharp à la sortie du Tier 2 :

    14:25  api-sports 14 matchs (14 sharp)      →  39
    16:39  api-sports 13 matchs (11 sharp)      →  58
    18:00  api-sports 26 matchs (22 sharp)      →  42
    19:20  ⛔ budget de SCAN 64/64, cycle ignoré →  28
    20:00  ⛔ budget de SCAN 71/64, cycle ignoré →  25

api-sports est la seule source qui porte un prix Pinnacle sur ~100 % de ses
matchs. Un cycle foot coûte 8 requêtes, `SCAN_BUDGET` en autorise 64 : huit
cycles, pas un de plus. Rien ne disait QUAND les dépenser, donc les premiers
crons livrés raflaient tout — et la source s'éteignait précisément quand le
slate européen entre dans la zone jouable 2-24 h. À la cadence horaire du
cron `golden` (:25), le budget entier partait AVANT 07:00 UTC et le soir
n'avait pas un seul cycle.
⚠️ Le motif dominant de rejet n'était PAS un seuil : sur 5 runs, 141 refus
« Échec prix Sharp » (dont 81 en tennis, structurellement sans source sharp
depuis l'obsolescence d'OddsAPI), 57 `LINESKIP`, 19 `CONFLIT SHARP` — et
**zéro** `PLAFOND`, **zéro** `SUSPECT`. Aucune garde d'edge ne refusait quoi
que ce soit : elles n'étaient pas atteintes, faute de candidats.
Le budget n'est PAS augmenté — le compte a été SUSPENDU pour dépassement le
2026-08-20 et la marge de sûreté ne se mange pas. Il est ÉTALÉ :
`api_sports.scan_allowance()` n'ouvre à chaque heure que la fraction du
budget correspondant à la journée UTC écoulée, avec un plancher d'un cycle
pour que le premier scan du jour parte toujours. Aucun horaire n'est codé en
dur (une fenêtre qui bouge dans `core/scan_windows.py` n'a rien à
re-déclarer), et `fetch_results` n'est pas concerné : la réserve du
settlement reste intacte.
Gardien : `tests/test_api_sports.py::TestRythmeDeDepense` — en particulier
`test_le_matin_ne_peut_pas_bruler_le_budget_du_soir`.

### OddsAPI est OBSOLÈTE (décision opérateur 2026-08-26)

(décision opérateur 2026-08-26) : `ODDS_API_ENABLED`
(`run_engine.py`) vaut 0 par défaut, le Tier 1 ne s'exécute plus, aucune
alerte de pool ne part (un pool mort est l'état NOMINAL, pas une panne).
Réactivation : `ODDS_API=1`. Le module `core/odds_api.py` RESTE — il n'est
pas qu'une source, ses `SPORT_KEYS` sont le vocabulaire écrit dans
`signals.sport` et relu par `api/index.py` (invariant des sport-keys,
AUDIT.md §2). Ce que l'obsolescence tue, en clair : tennis, hockey,
MMA/boxe, NFL/NCAAF, LdC/UEL, Euroleague (aucune source gratuite ne les
price), et la capture closing-line « en stop » sur le payload payant —
seul `run_closing_line.py` la fait encore, donc le CLV réel se raréfie
alors que `learning_layer` en fait un critère de premier rang. La garde
`[ -z "$ODDS_API_KEY" ] && exit 1` a été retirée des scans (fusionnés dans
`scan.yml` le 2026-08-26) : elle échouait FERMÉ et aurait tué tous les scans
le jour où le secret est retiré. La sortie anticipée de GOLDEN_HOUR supposait
un Tier 1 vivant : sans elle, le tick golden était un no-op horaire permanent.
Gardien : `tests/test_oddsapi_obsolete.py`.

**RALLUMÉ le 2026-09-01** (décision opérateur, nouvelle clé posée par
`rotate_odds_key.py --add` dans `app_secrets.ODDS_API_KEYS`). Ce qui a été
fait, et pourquoi ainsi :

- le flag `ODDS_API=1` est posé par `scripts/ci_scan_mode.py::TIER1_ENV` pour
  `standard`, `golden` et `deep` — pas dans `scan.yml`, pas par défaut dans
  `run_engine.py` (le défaut 0 reste verrouillé : un run local ou un futur
  workflow ne doit jamais dépenser un crédit sans l'avoir demandé). Golden
  l'a perdu une heure (50c127e : crainte d'une clé vidée en 3-5 jours) et
  l'a RETROUVÉ le même jour, décision opérateur, avec le rythme mensuel
  ci-dessous — c'est l'allocation du jour qui borne la dépense, pas le
  nombre de ticks. Le bouton « Scanner » du dashboard promeut désormais le
  tick golden en scan STANDARD (était guerrilla : sans Tier 1, foot seul) ;
- la sortie anticipée GOLDEN_HOUR (« 0 event OddsAPI dans T-2h → exit ») a
  été **retirée**, et non ré-armée : elle datait d'un Tier 2 fait de recherche
  web ; aujourd'hui le Tier 2 porte tout le volume (api-sports, odds-api.io,
  titan007, Matchbook) et le tick golden tire 24 fois par jour. La garder
  aurait rendu ces sources muettes à chaque tick où le pool est vide, hors
  fenêtre ou sans match à 2 h. Gardien :
  `…::test_golden_hour_tier_1_allume_mais_vide_descend_au_tier_2` ;
- **le budget est le vrai risque.** Une clé = 500 crédits/mois ; un scan
  paie ~3 crédits par ligue peuplée (24 h ≈ 4 ligues ≈ 9-12 crédits, plus
  en saison). Mesuré le 2026-09-01 : 24 crédits sur le tick standard de
  09:03, soit ~240/jour à 10 scans — le pool de 5 comptes (2 500) serait
  parti en dix jours. D'où le **rythme mensuel** (même jour, décision
  opérateur « 1 mois seulement, maximum d'utilisation, suffisant pour tenir
  30 jours ») : `core/scan_windows` calcule à chaque scan l'allocation du
  jour = crédits restants du POOL ENTIER (5 sondes gratuites) ÷ jours
  restants du cycle de 30 j (`meta.oddsapi_cycle_start`, redémarre seul) ;
  un plafond intra-journée linéaire (15 % à 02:00 UTC, 100 % à 22:00) garde
  du budget pour la soirée Big 5 ; closing line imminente jusqu'à 110 % de
  l'allocation, fenêtre favorable / golden T-2h jusqu'à 100 %, fond jusqu'à
  50 % ; dans un scan les ligues les plus peuplées passent d'abord. L'engagé
  du jour vit dans `meta.oddsapi_spent_day`. Ce n'est PAS le gouverneur
  retiré le 2026-08-01 (« ne pas rationner » : il étalait un budget que
  l'opérateur voulait brûler) — celui-ci vise 100 % du pool, à la bonne
  vitesse, et l'inutilisé d'un jour creux est reporté. `ODDS_API_PACING=0`
  le coupe. Gardiens : `tests/test_scan_windows.py::TestRythme` et suivants.
  Un pool mort n'est PAS une panne
  (les alertes de pool Telegram sont de nouveau actives et le disent) :
  la réponse est `rotate_odds_key.py --add`, jamais un rationnement muet.

### odds-api.io : le plan autorise DEUX books, un seul est sélectionné (2026-08-27)

Le côté SOFT est le goulot de tout le pipeline, et il tenait ce jour-là sur
un unique book. Relevé en interrogeant l'API :

- `/v3/odds/multi` **exige** le paramètre `bookmakers` (HTTP 400 sans lui) ;
- avec `bookmakers=1xbet`, **4 événements sur 10** reviennent sans aucune
  cote — 1xbet ne price ni le NCAA ni les équipes réserves ;
- demander un book de plus : `403 — « You're allowed max 2 bookmakers.
  Allowed: 1xbet »`. **Le plan en autorise deux. Un seul est pris.**
- demander un exchange : `403 — « sharp or exchange books are only available
  on our paid plans »`. Aucun prix sharp ne viendra donc jamais de cette
  source sur le plan gratuit ; c'est Matchbook qui le porte, et lui seul.

Conséquence mesurable : le slate soft d'odds-api.io est peuplé de rencontres
que Matchbook ne cote pas (réserves sud-américaines, NCAA, championnats
islandais) tandis que Matchbook cote Bayern–Stuttgart, Lille–PSG,
Crystal Palace–City — sans prix soft en face. **2 matchs appariés sur 11.**
Le moteur ne peut pas calculer un edge sur un match dont il n'a qu'un côté.

Le second slot est donc le levier le plus lourd du pipeline, et il est
GRATUIT. Le code n'a rien à changer pour en profiter : `selected_bookmakers()`
lit `/v3/bookmakers/selected` à l'exécution (`ODDS_API_IO_BOOKMAKERS` force la
liste si besoin). Ce qui a été corrigé côté code, c'est que le second book
serve à TOUS les marchés et non au seul 1X2 — voir `_line_shopping`.

⚠️ Le choix du second book n'est pas neutre : MelBet est de la même famille
que 1xbet (mêmes lignes, aucune diversification). Un book à large couverture
et à lignes indépendantes ajoute des matchs ET un vrai line shopping — un
meilleur prix exécutable sur le MÊME pari est un edge honnête, pas un
artefact. Réinitialisation : `PUT /bookmakers/selected/clear`.

### odds-api.io : un POOL de comptes, budget par compte (2026-08-28)

Le second slot posé (Bet365 + 1xbet, vérifié le 2026-08-28 : Bet365 cote
5/5 des matchs sondés, 1xbet 4/5), le goulot suivant était le QUOTA : 221/400
à 13:00 UTC, tout pour le foot, les cinq autres sports en « rythme de
dépense » à chaque tick. Le plan se compte PAR COMPTE (500 req, 2 books) :
`ODDS_API_IO_KEYS` (CSV, app_secrets d'abord) ajoute des comptes, sur le
contrat de `core/odds_api.candidate_keys` — ordonné, dédupliqué, compte
refusé (401/403/429) écarté pour le processus et MÊME requête rejouée sur le
suivant. Trois règles à ne pas défaire :
- le budget est tenu PAR COMPTE (`_bucket(clé)` = empreinte, jamais la clé :
  le nom finit dans `meta`) — un compte à 400 ne coupe pas les autres ;
- le rythme de dépense porte sur le TOTAL et reste `daily_quota.paced_
  allowance` — aucune copie (`test_le_rythme_vit_a_UN_seul_endroit`) ;
- les books sont lus PAR COMPTE et la garde « aucun bookmaker » passe AVANT
  la requête calendrier : une réponse est servie par un seul compte, donc
  chaque compte doit porter ses deux books lui-même
  (`scripts/odds_api_io_books.py --compte N`).
⚠️ Multi-comptes chez un fournisseur gratuit = risque de conditions
d'utilisation ; api-sports a suspendu le compte le 2026-08-20. DAILY_BUDGET
reste 400/500 par compte. Décision opérateur du 2026-08-28.
Gardiens : `tests/test_odds_api_io.py` (bloc « Pool de comptes »).

### Périmètre sports (2026-08-22)

eSports/tennis de table/volley/handball RETIRÉS
(`RETIRED_SPORTS`, garde dans `_emit`, données historiques conservées) ; MMA/boxe/NFL/
LdC/UEL/Euroleague sur flux OddsAPI réel (pré-vol 0 crédit, `SEASON_OPENS` pour la NFL).
Plus aucun sport pricé par recherche web. Détail : `reports/refonte_scope_2026-08.md`.

### Sources gratuites Asie

(mission 3, 2026-08-22) : cadre commun `core/source_adapter.py`
(appariement par temps+ligue+STRUCTURE de cotes, jamais par nom ; divergence en
POINTS de probabilité, pas en % relatif — un seuil relatif crie au loup sur tout
outsider) ; `core/odds500.py` (odds.500.com, 30 books dont Pinnacle `cid=1055`,
books identifiés par marge+pays car les noms sont masqués), `core/sevenm.py`
(7M = source de NOMS anglais, pas de cotes — aucun endpoint de cotes gratuit),
`core/prediction_markets.py` (Kalshi/Polymarket, rôle consensus).
Nowgoal/win007 = MORTE depuis les runners (DNS), ne pas réessayer.
Dictionnaire `team_aliases` (`sql/migrate_v10_3_team_aliases.sql`) :
clé = identifiant numérique de la source, pas le libellé ; un nom résolu ne
repasse jamais par l'IA. Migration APPLIQUÉE le 2026-08-22 — vérifié en base
le même jour (table présente, 12 lignes). L'ancienne mention « À APPLIQUER »
contredisait la ligne suivante ; une consigne qui se contredit fait rejouer
une migration déjà passée.
Câblage : `core/free_sources.py` (appelé EN DERNIER par harvester.fetch_matches,
car il se mesure contre les sources déjà collectées). odds500 démarre en MODE
OMBRE → rend [] tant qu'il n'a pas 100 matchs appariés à <2 pts de divergence :
zéro signal au premier déploiement, c'est voulu. Coupe-circuit `FREE_SOURCES=0`.
Un match dont une équipe ne se résout pas est ÉCARTÉ, jamais émis en chinois.
Curseur `meta.sevenm_sitemap_cursor` OBLIGATOIRE : le sitemap 7M (435 ids
au 2026-08-26, pas 936) commence par des coupes mineures sans recoupement —
sans curseur, 0 alias appris à chaque run et branchement inerte en silence.
Le curseur NE SUFFIT PAS : mesuré le 2026-08-26 sur 30 ids de tête, 0 échec
de requête mais **27 matchs DÉJÀ JOUÉS** et 3 utiles seulement (le sitemap
n'est pas trié par coup d'envoi et traîne plusieurs jours de passé). D'où
`meta.sevenm_past_gids` : un match joué ne redevient jamais à venir, on ne
le repaie donc plus jamais. Rendement mesuré 10 % → 30 % dès le 2e run, et
croissant. La mémoire est refermée sur le sitemap courant à chaque écriture,
sinon elle gonfle sans fin.

### Le pont d'alias avait DEUX chemins morts en silence (2026-08-28)

Mesuré au scan de 14:19 : odds500 rend **27 matchs, 27 avec prix sharp réel**
— et **26 sont écartés « faute d'alias fiable »**, 7M à court de budget
(90/80). L'estimation « ~11 jours pour converger » (ci-dessous) reposait sur
le seul chemin 7M ; deux autres existaient et n'étaient branchés nulle part :
1. **Le slate de confiance du run** (api-sports/Matchbook/titan007, noms
   anglais). `measure_against` apparie DÉJÀ ces fixtures à celles de 500.com
   par temps + ligue + structure — mais seulement APRÈS `resolve_names`, qui
   venait de les jeter. Les matchs mesurables étaient exactement ceux qu'on
   écartait. `learn_from_trusted` fait le même appariement AVANT, et nourrit
   `apply_pairing(canonical_source="trusted")` : gratuit, zéro requête,
   confiance 0,7 comme 7M (même nature de preuve, aucun nom).
2. **`team_aliases.resolve_with_ai`** (lane `translate_cjk`, 40 appels/jour
   sur les modèles chinois du registre) — écrite le 2026-08-22, **jamais
   appelée** : 12 alias en base, tous `sevenm`, zéro `ai`, aucune clé
   `quota_alias_ai_*` jamais écrite. Capacité morte en silence, règle 6.
   Branchée dans `resolve_names` pour les noms encore inconnus.
⛔ L'IA PROPOSE, ELLE NE DÉCIDE PAS. Un alias IA part à 0,4 sous le seuil de
0,6 : le match reste écarté tant que deux appariements indépendants (7M ou
slate de confiance) ne l'ont pas confirmé. `resolve_names` ignore la valeur
rendue par l'IA et ne lit que `canonical()`. Un nom déjà proposé ne repasse
pas par l'IA (c'est un dictionnaire, pas un traducteur), et une panne IA
n'écarte rien de plus.
Gardiens : `tests/test_free_sources.py::TestLesDeuxChemainsQuiManquaient`,
`tests/test_team_aliases.py::TestSeuilDeConfiance`.

### Le slate de confiance a appris QUATRE alias faux sur cinq (2026-08-28)

Premier run du chemin `learn_from_trusted` (ci-dessus), 15:48 UTC :
« appariement odds500↔trusted : 5 paires sur 28×104 ». Deux justes (波鸿 →
VfL Bochum, 奥斯纳 → Osnabrück) et **quatre fausses** : 拜仁/斯图加特
(Bayern/Stuttgart, 德甲) appris comme **UCD / Finn Harps** (Irlande D2),
蒙彼利埃/布洛涅 (Ligue 2) comme Farsta / Nacka Iliria (Suède, divisions
inférieures), 雷克斯/伯明翰 (Championship) comme Kerry / Treaty United,
卡斯鲁厄/沃夫斯堡 (2. Bundesliga) comme **AIK W / Kristianstad W** (football
féminin). Huit lignes `team_aliases` (id 15-22), `resolved_by='trusted'`,
confiance **0,7 ≥ MIN_CONFIDENCE 0,6** — utilisables dès l'écriture. Le
`SUSPECT_DATA odds500 vs trusted 11.77 pts` loggé juste après, c'était la
comparaison de Bayern-Stuttgart avec UCD-Finn Harps.
Cause : `pair_fixtures` ne refusait une ligue que si elle était connue des
DEUX côtés (`la and lb and la != lb`). Le libellé api-sports (« Ireland -
First Division ») n'est pas dans `LEAGUE_MAP` → `lb` vide → la garde ne
peut rien dire, et il reste le temps (même minute) et la structure — deux
gros favoris se ressemblent à moins de 12 pts. Conçu pour le chemin 7M, dont
les libellés ont été recopiés dans `LEAGUE_MAP` ; jamais mesuré sur le slate
de confiance. Et le paragraphe « l'IA propose, elle ne décide pas » ne
protégeait que la voie IA (0,4) : la voie `trusted` décide en un passage.
Correction : `pair_fixtures(require_league=True)` sur ce chemin — une ligue
inconnue d'un côté n'est plus « pas de désaccord », c'est « pas de preuve ».
Ça coûte les deux paires justes de 德乙 (pas dans `LEAGUE_MAP` non plus) :
un match sauté contre un alias faux à vie, l'arbitrage est le même que pour
l'ambiguïté. Les huit lignes ont été INVALIDÉES en base (confiance 0,
contradiction +1 — le geste d'`invalidate()`, pas un DELETE : la ligne
garde la trace de ce que la source a affirmé).
⚠️ Impact contenu parce qu'odds500 était en MODE OMBRE (0 émis). Le jour où
elle en sort, un alias faux = un signal émis ET réglé sur le mauvais match.
Gardiens : `tests/test_source_adapter.py::TestLaLigueEstExigeeSurLeCheminDeConfiance`,
`tests/test_free_sources.py::TestLeSlateDeConfianceExigeLaLigue`.

### 500.com sert son mur anti-bot en HTTP 200 — odds500 meurt sans un WARNING (2026-09-02)

DIAGNOSTIQUÉ, PAS CORRIGÉ — le statu quo est le choix par défaut, la
décision finale appartient à l'opérateur.

Symptôme : odds500 rend « 0 matchs au calendrier » sur TOUS les runs depuis
le 2026-09-01 ~11:40 UTC. Dernier run vivant : 33489971035 à 09:00
(16 matchs) ; premier mort : 33503697780 à 11:40. Aucun WARNING nulle part :
le run contract ne voit rien, car `[]` est un retour « propre » — `_get` n'a
pas échoué, le parseur n'a juste rien trouvé.

Cause établie (MESURÉ le 2026-09-02, 2 requêtes de diagnostic) : 500.com a
déployé un challenge anti-bot Tencent EdgeOne servi en **HTTP 200** — une
page de **987 octets** de JS obfusqué (cookies `EO_Bot_Ssid`/`__tst_status`)
au lieu des ~288 Ko du calendrier ; rejouer la requête avec les cookies
calculés escalade vers une page « Security Verification » de **1 978
octets**. Le parseur (`_ROW_RE` sur `<tr data-fid=…>`) rend légitimement 0.
Le mur couvre l'IP datacenter ET la sortie Webshare UK. Ce que ce N'EST PAS,
mesuré aussi : pas un re-filtrage de l'IP du proxy, pas le proxy gratuit qui
rate 1/3 des requêtes (cet aléa est TRANSITOIRE et loggue un WARNING après
3 tentatives ; ici, 200 persistant sur tous les runs), et pas le mode ombre
(`meta.source_scorecard_odds500` : matched=244, shadow=false, promue à
100 matchs / 0.00 pt de divergence médiane, errors=0 — la source était
PROMUE quand le mur est tombé). C'est exactement la « panne INDISCERNABLE
d'un blocage réel » que l'entrée du proxy (ci-dessous) redoutait — sauf
qu'ici le blocage est réel.

Ce qui a été fait : le diagnostic ci-dessus, rien d'autre. Ce qui n'a PAS
été fait, et pourquoi : exécuter le challenge JS demanderait un navigateur
headless ou un service de déblocage — coût et fragilité, décision opérateur ;
l'alternative est l'abandon de fait. Le code reste en place : il rend `[]`
sans nuire (~1 requête gaspillée par run) et le scorecard rétrogradera la
source de lui-même si le mur persiste.

⛔ NE PAS couper `FREE_SOURCES=0` pour « nettoyer » : ce coupe-circuit
tuerait aussi 7M et les marchés de prédiction, qui vont bien.

Gardien : aucun nouveau — le comportement « 200 avec 0 ligne = silence » est
celui, documenté, de `core/odds500.py` (`_get` lignes 188-210,
`fetch_fixtures` lignes 245-290) : une RÉPONSE du serveur n'est pas un échec
de transport, et le parseur qui ne trouve rien rend `[]`.

### Le proxy gratuit rate une requête sur trois — et ça coûtait la source (2026-08-28)

Mesuré au lendemain du déblocage, trois GET identiques sur 500.com à travers
le proxy Webshare : **un timeout de handshake TLS à 40 s, deux réponses en
~1 s**. Un proxy gratuit et partagé est instable par construction.
Sans reprise, cette unique requête ratée rendait `_get` None, le calendrier
repartait vide, et odds500 loggait « 0 match dans les 24h » — INDISCERNABLE
d'un blocage réel. On venait de payer un proxy pour lever un blocage ; le
perdre un run sur trois sur un aléa réseau n'a pas de sens.
`net.open_with_retry()` reprend DEUX fois au plus, et seulement sur les
échecs de TRANSPORT (timeout, connexion refusée, coupure TLS). Un `HTTPError` — 403,
404, 429 — est une RÉPONSE du serveur : la rejouer ne changerait rien et ne
ferait que marteler la source, ce que `robots.txt` et le budget journalier
existent pour éviter. L'échec final remonte tel quel : l'appelant garde son
`except` et son message, seul le nombre d'essais change.
⚠️ Le helper vit dans `core/net.py` et NON dans chaque source : odds500 et 7M
passent par le même proxy, donc par la même instabilité. Une seule des deux
protégée serait une liste qui diverge (règle dure n°6), et c'est exactement
ce que garde `test_les_deux_sources_du_proxy_l_utilisent`.
⚠️ UNE reprise ne suffisait pas, et c'est mesuré : depuis un runner le
2026-08-28, les DEUX tentatives ont échoué sur le même scan (« handshake
timed out » puis « Remote end closed connection ») alors que le même proxy
rendait **6/6** depuis un poste de dev à la même minute. Le chemin
runner → proxy est plus fragile, et les échecs se GROUPENT. D'où 3 tentatives
(`FREE_SOURCES_TENTATIVES`) : à ~1 échec sur 3, le risque de perdre la source
sur un run passe de 11 % à 4 %, pour une requête de plus en cas d'échec
seulement.
Gardien : `tests/test_free_sources_wiring.py::TestRepriseSurEchecPassager`.

### Le pont d'alias converge, mais sa mise en route coûte ~11 jours (2026-08-28)

Mesuré en direct, odds500 et 7M interrogés côte à côte depuis un poste :

    odds500        14 matchs à venir, 14 avec prix sharp réel
    7M sitemap     854 identifiants
    balayage       60 identifiants interrogés → **1 seul match à venir**,
                   42 déjà joués (donc mémorisés), 17 inexploitables

Le sitemap 7M n'est pas trié par coup d'envoi et traîne plusieurs jours de
passé : le rendement en matchs À VENIR est de ~1,7 % au premier passage. Avec
`SEVENM_DAILY_BUDGET` = 80, il faut ~11 jours pour balayer les 854 entrées.
⚠️ CE N'EST PAS UNE PANNE, ET LE COÛT EST NON RÉCURRENT : les 42 joués de ce
balayage sont entrés dans `meta.sevenm_past_gids` et ne seront plus jamais
repayés. Le rendement monte donc à chaque run. Ce qu'il faut surveiller n'est
pas la lenteur mais l'ARRÊT : si `team_aliases` (12 lignes, inchangé depuis
le 2026-08-22) n'a pas bougé sous deux jours, le pont ne fonctionne pas et il
faut le diagnostiquer, pas l'attendre.

    python scripts/ops.py supabase sql "select count(*) from team_aliases"

⚠️ Et rappel de l'échelle : la sortie du MODE OMBRE demande 100 matchs
appariés à ≤ 2 points de divergence. Tant que le dictionnaire est vide,
14 des 15 prix sharp d'odds500 sont écartés à chaque run.

### odds500 était FILTRÉE PAR IP — ✅ LEVÉ le 2026-08-27 par un proxy UK

HTTP 200
et 15 fixtures depuis un poste de dev, `Connection refused` depuis les
runners GitHub. Le code, le parseur et le User-Agent vont bien — aucune
correction de code ne lève un blocage d'IP. Seule issue : `core/net.py`
(`FREE_SOURCES_PROXY`, ou `ODDS500_PROXY`/`SEVENM_PROXY` par source). Sans
variable, le module est INERTE et rien ne change. Plomberie COMPLÈTE au
2026-08-26 : lecture par `secret_store` (donc `app_secrets` AVANT l'env —
URL rotative sans redéploiement), les 3 variables sont transmises par
engine/golden_hour/deep_scan/guerrilla, documentées dans `.env.example`,
et `ops.py sources` affiche `[via proxy]`. ⚠️ `proxy_for` MÉMORISE sa
résolution pour tout le processus : `get_secret` ne met pas les valeurs
ABSENTES en cache, et l'absence de proxy étant le cas nominal, chaque
requête HTTP d'odds500 aurait relu Supabase. `net.reset()` pour les tests.
Chemin proxy VÉRIFIÉ de bout en bout : proxy vivant → 99 420 caractères
et 15 fixtures ; proxy mort → échec (donc rien ne le contourne) ; sans
proxy → succès direct. ⚠️ Un test depuis un poste de dev ne prouve RIEN
sur les runners, où ça marche déjà sans proxy — seul un run GitHub tranche.
VOIE RETENUE (décision opérateur 2026-08-26) : RELAIS Cloudflare Worker,
`scripts/cloudflare_relay_worker.js`. Un Worker ne parle pas CONNECT — ce
sont donc DEUX mécanismes distincts dans `net.py`, pas deux réglages du
même : `prepare()` réécrit l'URL (relais), `opener_for()` tunnelise
(proxy). Le relais gagne si les deux sont posés. Variables :
`FREE_SOURCES_RELAY` + `FREE_SOURCES_RELAY_TOKEN` (et `ODDS500_RELAY` /
`SEVENM_RELAY`), câblées dans les 4 workflows de scan.
DEUX GARDES NON NÉGOCIABLES côté Worker, sans quoi c'est un PROXY OUVERT
que le premier venu utilisera sur ton quota : jeton partagé (comparé en
temps constant) ET liste blanche d'hôtes. Gardées par
`tests/test_free_sources_wiring.py::TestModeRelais`.
Le Worker doit rendre `upstream.body` (octets bruts) et JAMAIS `.text()` :
500.com sert du GB18030, un passage par le texte rendrait tous les noms
chinois en mojibake — panne silencieuse ressemblant à un parseur cassé.
Chemin relais VÉRIFIÉ de bout en bout contre un serveur local conforme :
15 fixtures, `大田市民 vs 蔚山现代` intact ; jeton faux → 403 ; hôte hors
liste avec jeton valide → 403 ; 7M → 435 ids.
EN PRODUCTION DEPUIS LE 2026-08-26 : Worker `predator-relay` déployé sur le
compte, sous-domaine `predator-relay.ipotradingbot.workers.dev`, et les deux
secrets GitHub posés. ⚠️ LE PIÈGE QUI A COÛTÉ LE PLUS DE TEMPS : le Worker
était uploadé ET son binding `RELAY_TOKEN` présent, mais le sous-domaine
`workers.dev` était DÉSACTIVÉ — le script n'avait donc aucune URL publique
et rendait 404 sur tout. Un `workers/scripts` qui liste le Worker ne prouve
PAS qu'il est joignable : vérifier `GET workers/scripts/<nom>/subdomain`
(`enabled: true`). La valeur d'un `RELAY_TOKEN` déjà posé étant ILLISIBLE,
la seule façon de faire correspondre les deux côtés est de le faire tourner.
✅ LEVÉ — que 500.com accepte les IP de sortie de Cloudflare : mesuré le
2026-08-26, 200 et 58 807 octets à travers le relais, soit exactement la
taille obtenue en direct. Pas de 502, donc pas de proxy à IP dédiée à
chercher. Encodage vérifié à travers le relais : 518 noms chinois, ZÉRO
mojibake. 7M a été joint pour la PREMIÈRE fois (435 ids) — sa joignabilité
n'est plus inconnue. `ops.py sources` affiche `[via relais Cloudflare]` sur
les deux. ⛔ TRANCHÉ le 2026-08-26 (run engine 32994959190, 17:34) : depuis
un runner, odds500 rend « 403 de l'AMONT via le relais (colo Cloudflare
IAD) ». Le Worker s'exécute au colo le plus proche de l'APPELANT — Londres
(LHR) depuis le poste de dev, où 500.com répond 200 ; Washington (IAD)
depuis les runners GitHub, où 500.com REFUSE l'IP de sortie. Ce n'est ni
le jeton (tourné des deux côtés, même résultat), ni le code, ni la liste
blanche. `net.describe_failure` le dit en clair : un 403 SANS `X-Relay-By`
serait le Worker (jeton/hôte) ; AVEC, c'est l'amont, et le colo est nommé.
Conséquence : le relais Cloudflare tel quel NE SUFFIT PAS depuis GitHub
Actions. Il faut une sortie hors des colos US — relais épinglé en Europe
(Fly.io/Render région EU), proxy à IP dédiée, ou runner auto-hébergé en
Europe. ✅ RÉSOLU LE 2026-08-27 — proxy Webshare à sortie LONDRES (plan gratuit,
10 proxys, 1 Go/mois), posé en secret GitHub `FREE_SOURCES_PROXY`. Premier
run depuis un runner (33120263411) :

    odds500: 15 matchs (15 avec prix sharp réel) / 15 à venir | 16 req
    sevenm: 854 identifiants au sitemap   ← 7M atteint EN PRODUCTION
    free_sources: 14 match(s) avec un nom inconnu — interrogation 7M

Le 403 a disparu. odds500 apporte du prix SHARP RÉEL, ce qu'aucune autre
source gratuite ne fait à ce volume. Vérifié avant de poser le secret :
`curl --proxy … https://odds.500.com/fenxi/ouzhi-1.shtml` → 200 et
**58 807 octets**, exactement la taille documentée d'une réponse valide.
⚠️ Le Smart Placement du relais a été essayé le même soir et NE SUFFIT PAS
(colo déplacé IAD → SEA, toujours américain, toujours refusé). C'est le
proxy qui a réglé le blocage, pas lui.
⛔ ET SANS L'INVERSION DE PRÉCÉDENCE, LE PROXY N'AURAIT RIEN CHANGÉ : le
relais captait l'URL même quand un proxy était posé. Le log le dit désormais
en clair : « net[odds500]: proxy configuré — le relais est ignoré ».
⚠️ CE QUI N'EST PAS ENCORE GAGNÉ, et il ne faut pas le lire comme une panne :
14 des 15 matchs sont ÉCARTÉS faute d'alias fiable, et odds500 reste en MODE
OMBRE (1 match mesuré, 0 émis). Les deux se résorbent run après run — le
dictionnaire `team_aliases` se remplit à chaque interrogation de 7M, et la
sortie du mode ombre demande 100 matchs appariés à ≤ 2 points. Compter en
JOURS, pas en runs.

### Un proxy posé était CAPTÉ par le relais, en silence (2026-08-27)

« J'avais installé un proxy » — et rien n'avait changé. Deux causes,
cumulées, et aucune ne produisait le moindre message d'erreur.

1. AUCUN PROXY N'ÉTAIT POSÉ NULLE PART. Vérifié le 2026-08-27 : ni dans
   `app_secrets` (qui ne porte que les 3 clés de cotes), ni dans les secrets
   GitHub Actions (seuls `FREE_SOURCES_RELAY` et `..._TOKEN` existent), ni
   dans `.env`. Le bloc `env:` des workflows affichait bien
   `FREE_SOURCES_PROXY:` et `ODDS500_PROXY:` — VIDES. La plomberie est
   complète depuis le 2026-08-26 (`scripts/ci_env.py::RELAYS`) ; il ne
   manquait que la VALEUR. Un proxy acheté chez un fournisseur n'entre pas
   tout seul dans le pipeline : il faut poser le secret.
2. ET MÊME POSÉ, IL AURAIT ÉTÉ IGNORÉ. La règle « le relais gagne si les
   deux sont posés » datait du 2026-08-26, quand le relais était le seul
   mécanisme et qu'on ignorait encore d'où il sortirait. On le sait depuis :
   un Worker s'exécute au colo le plus proche de l'APPELANT — IAD depuis les
   runners — et 500.com refuse cette IP. Le relais est donc PROUVÉ inopérant
   là où le pipeline tourne, et le proxy est le remède documenté.
   ⛔ PRÉCÉDENCE INVERSÉE : `net.prepare()` laisse l'URL intacte dès qu'un
   proxy est configuré pour la source, et le dit dans les logs. Poser un
   proxy est un geste EXPLICITE qui n'a qu'une raison d'être — contourner ce
   blocage précis. L'ancienne règle menait au pire scénario : capacité payée,
   jamais empruntée, invisible.
   Le relais reste le chemin par défaut quand aucun proxy n'est posé.
Gardien : `tests/test_free_sources_wiring.py::TestModeRelais`
::`test_un_proxy_pose_l_emporte_sur_le_relais` et
::`test_sans_proxy_le_relais_reprend_la_main`.
⚠️ Ce qui tranche reste un run DEPUIS UN RUNNER GitHub : `describe_failure`
nomme le colo, et un 403 SANS `X-Relay-By` n'a pas la même cause qu'avec.

### odds500 : le Smart Placement a été essayé — et il NE SUFFIT PAS (2026-08-27)

Le blocage est établi et ne change pas : un Worker s'exécute au colo le plus
proche de l'APPELANT, donc IAD depuis les runners GitHub, et 500.com refuse
cette IP de sortie. Ce qui n'avait jamais été vérifié, c'est le réglage qui
INVERSE cette règle. Relevé en base le 2026-08-27 : l'endpoint
`workers/scripts/predator-relay/settings` rend `placement: {}` — le Smart
Placement, qui exécute le Worker près de l'ORIGINE et non de l'appelant, n'a
jamais été activé.
Outil : `scripts/relay_smart_placement.py` (lecture seule sans `--oui`,
réversible par `--annuler`).

⛔ TRANCHÉ LE MÊME JOUR — ACTIVÉ, MESURÉ, INSUFFISANT. `placement: smart`
posé à 21:41, puis run de scan 33119345516 : le colo est passé de **IAD**
(Washington) à **SEA** (Seattle). Le Smart Placement DÉPLACE donc bien
l'exécution — ce n'est pas un réglage inerte — mais Cloudflare choisit par
LATENCE, et depuis les runners GitHub le plus proche de l'origine reste un
colo américain. 500.com refuse toujours : « 403 de l'AMONT via le relais
(colo Cloudflare SEA) ».
Le réglage est LAISSÉ EN PLACE : il ne nuit pas, et un proxy le contourne de
toute façon depuis l'inversion de précédence. Mais il ne faut plus le
compter comme une piste — elle est fermée, avec sa mesure.
CE QUI RESTE, ET IL N'Y A PLUS D'HYPOTHÈSE GRATUITE : une sortie hors des
colos US. Proxy à sortie européenne (`FREE_SOURCES_PROXY`, le chemin le plus
court — la plomberie est déjà là, il ne manque que la valeur du secret),
relais épinglé en Europe (Fly.io/Render région EU), ou runner auto-hébergé
en Europe.
⚠️ CE N'EST PAS UNE SOLUTION ANNONCÉE, c'est une hypothèse gratuite qu'on
ferme avant d'en payer une autre. Cloudflare optimise la LATENCE et choisit
lui-même le colo : rien ne garantit qu'il en retienne un dont 500.com accepte
l'IP. Si le 403 persiste en nommant un colo américain, la conclusion tient
sans changement — il faut une sortie hors des colos US (relais épinglé en
Europe, proxy à IP dédiée, ou runner auto-hébergé).
⛔ ET LE PIÈGE EST LE MÊME QUE CELUI DU SOUS-DOMAINE workers.dev : un
`placement.mode = smart` POSÉ ne prouve rien sur le RÉSULTAT. Seul un run
depuis un runner GitHub tranche, et `net.describe_failure` nomme le colo.
⚠️ Le PATCH renvoie les réglages EXISTANTS tels quels. N'envoyer que
`placement` effacerait les bindings du Worker, dont `RELAY_TOKEN` — dont la
valeur est ILLISIBLE une fois posée, ce qui obligerait à faire tourner le
jeton des deux côtés. Et l'endpoint n'accepte QUE du multipart/form-data :
un PATCH JSON rend 415.
Note sur le jeton : il avait été trouvé en lecture seule le 2026-08-27 sur
`workers/scripts`. Sur `settings` il rend 415 et non 403 — donc il écrit
peut-être ici. Un 403 ne veut pas dire « jeton expiré ».

### Kalshi/Polymarket

BRANCHÉS le 2026-08-26 (`free_sources.measure_slate_consensus`,
appelé par `harvester._fetch_multi_book`). Ils étaient importés NULLE PART
hors de leurs tests depuis le 2026-08-22 — capacité morte en silence. Rôle
`consensus` : ils MESURENT et n'émettent jamais, ne repricent rien ; ils
crient quand un prix du slate diverge d'un marché qui ne recopie aucun
bookmaker (un « edge » qui est en fait un prix périmé). Couverture honnête :
EPL/UCL/NFL/NBA seulement, et sur 70 fixtures EPL vivantes, **3** portent
des cotes exploitables. Le recoupement avec un slate fait de ligues mineures
est donc structurellement faible — c'est un garde-fou, pas un gisement.

### `strict_team_match` NE NORMALISE PAS la ligature « æ »

« Stabaek » et
« Stabæk » ne s'apparient que par le RATIO de similarité (0,857 ≥ 0,60), pas
par `_normalize_team`. Le ratio tombe à 0,476 dès qu'un côté porte un suffixe
de club (« Stabæk Fotball ») et l'appariement échoue — mesuré le 2026-08-26.
Le contrat est le REFUS silencieux, pas un prix posé au hasard. Élargir la
normalisation toucherait l'appariement de TOUT le pipeline (edge compris) et
ne se décide pas au détour d'un correctif.

### Pièges qui tuent une source en silence

User-Agent avec un accent → urllib encode
en latin-1 → 403 Cloudflare (Polymarket) ; Kalshi rend `yes_bid`/`volume` à `null`
et met les prix dans les champs `*_dollars` en chaîne ; un 1X2 amputé d'une patte
devient indiscernable d'un moneyline et s'apparie avec lui ; la `<description>`
d'un item Google News RSS est le TITRE recopié, pas un extrait — une source qui
« répond » peut ne porter aucun fait (mesuré sur /wiz avant sa suppression,
2026-08-22 — la leçon vaut pour toute source d'actualité).

### robots.txt

sur odds.500.com la QUERY STRING est la frontière (`/fenxi/ouzhi-*.shtml`
autorisé nu, interdit avec `?ctype=`/`?order=`/`?cids=`). Ne jamais paramétrer un
endpoint — gardé par `tests/test_odds500.py::TestRobotsTxt`.


## Règlement, CLV, apprentissage

Un règlement manqué ne retarde pas l'apprentissage : il DÉTRUIT
l'échantillon, parce qu'un signal non réglé finit purgé en `expired`.

### Settlement : le score vient d'un CHAMP, plus d'un LLM (2026-08-26)

LE SCORE VIENT D'UN CHAMP, PLUS D'UN LLM (2026-08-26).
`core/settlement.result_from_api_sports` interroge `core/api_sports.fetch_results`
(`/fixtures?date=`) AVANT toute recherche web : déterministe, gratuit, UNE
requête par journée quel que soit le nombre de matchs (cache de run). La
recherche web (Groq `compound-mini` + Tavily) reste en DERNIER RECOURS.
Pourquoi : mesuré le 2026-08-26, le taux de résolution réelle du ledger est
tombé de 65 % (23 août) à 11 % (24-26) parce que les DEUX quotas gratuits ont
lâché ensemble — Tavily au plafond de plan (HTTP 432) et Groq en limite par
minute. Un audit a rendu « 0 settled | 52 skipped », EN VERT. Or la réponse
qui porte les scores était DÉJÀ téléchargée à chaque scan par `fetch_sport`,
qui jette les matchs commencés (`if when < now: continue`).
MESURÉ EN RÉEL le 2026-08-26 contre l'API : 1 requête = 201 fixtures dont
197 terminées avec leur score. Rejoué sur les 17 matchs que l'audit venait
d'échouer à régler : **11 appariés (65 %), 1 refusé pour ambiguïté, 5 non
trouvés** — et c'est un PLANCHER, le test n'interrogeait que 2 journées là
où le code en interroge 3 autour du coup d'envoi. À comparer aux 11 % que
la recherche web obtenait.
⚠️ Appariement par `strict_team_match` sur les DEUX équipes, candidat UNIQUE
exigé : deux prétendants → REFUS. Régler le mauvais match écrirait un
WIN/LOSS faux et DÉFINITIF dans le ledger. On cherche aussi la veille et le
lendemain UTC (un coup d'envoi à 23h30 bascule de journée).
⚠️ Les clés `API_*` doivent être dans le pool `settlement` de `ci_env.py`,
sinon le chemin est INERTE sans erreur. Gardien :
`tests/test_ci_env.py::test_le_settlement_porte_les_cles_de_resultats`.
⚠️ RÉSERVE DE BUDGET TENUE EN NÉGATIF (`SCAN_BUDGET = DAILY_BUDGET −
RESULTS_RESERVE`, 64/16 sur 80). Au premier essai, l'audit s'est heurté à
« budget journalier atteint (80/80) » : les scans avaient tout consommé.
Les SCANS sont donc amputés, jamais la réserve — un scan de plus vaut
moins qu'un résultat de moins. Le total reste sous 80 : le plan fait 100
mais le compte a déjà été SUSPENDU pour dépassement le 2026-08-20, on ne
mange pas la marge. Un 429 pendant un scan cale le compteur à SCAN_BUDGET
et non au plafond, sinon il emporterait la réserve.

### Un audit stérile ALERTE (2026-08-26)

« 0 settled » sortait en `log.info`,
run vert, aucune alerte : la régression du 24 août a vécu deux jours sans
être vue. `_signaler_audit_sterile` envoie un Telegram ET pose
`meta.settlement_starved_at` ; tant que ce marqueur est frais (< 24 h),
`_purge_old_signals` porte sa fenêtre de 48 h à 96 h. Sans ce filet, une
panne de recherche ne retarde pas l'apprentissage, elle DÉTRUIT
l'échantillon : un signal purgé part en `expired`, ligne que
`learning_layer._clv_stats` exclut. Borné à 96 h — au-delà le score n'est
plus retrouvable et laisser gonfler la table créerait une seconde panne pour
en éviter une première. Gardien : `tests/test_settlement_deterministe.py`.

### `expired` n'est plus un état terminal (2026-08-27)

Une ligne passait en `expired` le plus souvent parce qu'on n'avait pas **PU**
chercher — Tavily au plafond de plan, Groq en limite par minute, api-sports
qui ferme l'historique au plan gratuit (« Free plans do not have access to
this date »). Or `fetch_pending` ne sélectionne que `status='active'` : la
ligne ne repassait donc **plus jamais** devant un moteur de recherche. Le
commentaire d'`audit_one` le disait déjà : « a transient rate limit
permanently cost those signals their real WIN/LOSS ».
MESURÉ le 2026-08-27 : **255 lignes** dans cet état (199 au ledger dont le
signal était purgé, 56 signaux), soit **57 % du portefeuille** absent de
/performance — biais de survie pur, puisque `learning_layer._clv_stats`
exclut les expirés. Après recherche manuelle de 137 affiches sur 175, la
résolution est passée de 43 % à 90 % (138 → 351 paris réglés) et le taux de
réussite s'est stabilisé à 59,4 % — il affichait 63,9 % après la première
vague, écart dû aux affiches les plus faciles à trouver, pas au portefeuille.
Outils : `scripts/backfill_expired_results.py` (réparation one-shot depuis
`reports/backfill_scores_2026-08.json`) et surtout `core/relance_expires.py`,
appelé à la fin de CHAQUE audit, qui reprend un lot de lignes expirées et
refait la recherche web.
⚠️ TROIS GARDES À NE PAS DÉFAIRE. (1) La relance passe **après** le settlement
frais : la réserve IA est tenue en négatif depuis le 2026-08-02, un signal du
jour vaut plus qu'un match d'il y a deux semaines. (2) **Curseur tournant**
(`meta.relance_expires_cursor`) : sans lui, 12 lignes/run repasseraient
éternellement sur les 12 mêmes introuvables pendant que les autres ne
seraient jamais retentées. (3) Elle ne **devine** rien : score introuvable ou
marché indécidable → la ligne RESTE expirée et repassera. Un WIN/LOSS faux au
ledger est DÉFINITIF, l'attente ne l'est pas.
⛔ NE PAS SUPPRIMER les lignes expirées résiduelles pour « nettoyer »
/performance : c'est la seule action qui rendrait leur résultat introuvable
pour toujours, et elle fabriquerait exactement le biais de survie qu'on vient
de retirer (règle dure n°9). Elles se résorbent d'elles-mêmes à chaque audit.
Gardien : `tests/test_relance_expires.py`.

### Un plafond appris sur l'ANCIEN moteur était réappliqué au nouveau (2026-08-27)

A6 avait tranché le matin même : « `_EDGE_CEILINGS` : rien à poser », et
« vérifié en base : `meta` ne porte aucune clé de plafond ». Le soir, l'audit
de 19:26 a reposé `edge_ceiling_soccer = 6.0` (écrit à 19:28). Rien dans le
code ne l'en empêchait : `compute_and_save` apprend sur les 120 dernières
lignes du ledger, toutes ANTÉRIEURES à la correction du prix et du pari. Le
constat « soccer au-dessus de 6 % perd » date du 2026-08-02 et d'une unité
que la refonte EV du 2026-08-22 a changée.
Il ne coupait rien ce jour-là — zéro refus `PLAFOND` sur 5 runs, faute de
candidats — mais il aurait mordu dès le retour du flux sharp : une panne
posée d'avance, invisible tant que la source d'amont était éteinte.
Ce que dit la règle 10 est appliqué DANS le code, pas seulement dans la
doc : `CALIBRATION_EPOCH` (défaut 2026-08-27) + `post_correction_rows()`.
Seules les deux sorties que le moteur fait RESPECTER (`edge_ceiling_*` et
`odds_ceiling_*`, lues par `run_engine._EDGE_CEILINGS`/`_ODDS_CEILINGS`) y
sont soumises ; tout le reste de la couche continue de lire le ledger entier,
puisqu'il est loggé et non appliqué.
⚠️ DEUX MOITIÉS, ET LA SECONDE EST CELLE QU'ON OUBLIE : gater les écritures
FUTURES ne retire pas la clé DÉJÀ posée, et cette couche ne fait que des
upserts — le 6,0 serait resté appliqué pour toujours. `_drop_stale_ceiling`
la retire, et ne fait aucun appel quand rien n'est posé (le cas nominal).
⚠️ `post_correction_rows` ÉCARTE une ligne sans date, là où `playable_rows`
CONSERVE l'inconnu. Les deux contrats sont inverses et c'est voulu : l'une
filtre ce qu'on OBSERVE (jeter l'inconnu viderait l'historique), l'autre ce
qu'on IMPOSE au moteur (garder l'inconnu ferait passer l'ancien moteur pour
une preuve).
La borne se lève d'elle-même quand le moteur corrigé aura produit assez de
réglés. Elle ne modifie aucun seuil : elle interdit d'en fabriquer un sur une
mesure périmée.
Gardien : `tests/test_learning_layer.py::TestEdgeCeiling` — les quatre tests
d'époque, dont `test_un_plafond_perime_est_RETIRE_pas_seulement_plus_reecrit`.

### Le même piège côté PLANCHERS : « loggé, jamais appliqué » était faux (2026-08-28)

L'incident du plafond (ci-dessus) s'est rejoué le lendemain, côté seuils —
et il avait survécu à la correction précisément à cause d'un commentaire
faux. f30b317 disait : « les deux plafonds sont les seules sorties de cette
couche que le moteur fait respecter ; le reste est loggé ». CLAUDE.md
répétait « seuils … loggés, jamais appliqués ». Or `run_engine` passe
`threshold_<sport>`/`threshold_seg_*` à `_segment_min_edge`, qui en fait le
`min_edge` de CHAQUE scan. Résultat : `threshold_soccer = 5.6` (appris le
2026-08-24 sur les edges de l'ANCIEN moteur, avant la correction du prix et
du pari) gatait l'émission du moteur corrigé, pendant que le plancher
committé (SPORT_DEFAULTS 1.2 + EV_EDGE_FLOOR 1.5) aurait laissé passer dès
1,5 % d'EV. `threshold_basketball` a même été RE-calculé le 2026-08-28 à
09:25 sur une fenêtre dominée par des lignes pré-époque.
Contexte opérateur, à retenir : la demande était « baisse les seuils, la
perf est à plus de 60 % ». La re-mesure du jour (`replay_ledger_executable`,
390 lignes, 90 % résolues) répond : ROI net de taxe **−12,1 %** au prix
exécutable, bandes à 55-72 % de réussite pour des points morts à 74-78 %
(cotes moyennes 1,34-1,43), aucune bande d'EV ne qualifie. **Un taux de
réussite nu au-dessus de 60 % peut perdre de l'argent** (règle 7) —
MIN_EDGE / EV_EDGE_FLOOR / SUSPECT_EDGE n'ont donc PAS bougé (règle 10).
Ce qui a été fait est l'inverse d'une baisse au doigt mouillé : appliquer
aux planchers la règle d'époque des plafonds. `compute_and_save` ne décide
un seuil que sur `post_correction_rows` ; sous `_MIN_SAMPLES` post-époque,
un seuil POSÉ est RETIRÉ (retour à SPORT_DEFAULTS) — même mécanique de
retrait actif, la couche ne faisant que des upserts. La borne se lève
d'elle-même quand le moteur corrigé a assez de réglés.
Gardien : `tests/test_learning_layer.py::TestSeuilEpoqueRegle10`.

### Le même match réel pesait DOUBLE : deux sources, deux match_id, deux lignes de ledger (2026-09-02)

L'opérateur voyait sur /performance des lignes réglées jamais vues ailleurs ;
en tirant le fil, le diagnostic a trouvé des matchs comptés DEUX FOIS dans
l'historique et dans le n de la couche d'apprentissage. Mécanique : le même
match réel arrivant par deux sources porte deux match_id (uuid OddsAPI d'un
côté, id dérivé des noms d'équipes de l'autre) → deux signaux jumeaux
coexistent, chacun est réglé, chacun écrit sa ligne. Toutes les gardes
d'unicité raisonnaient par signal_id (`ledger_signal_id_uniq`, v10_8,
`_ledger_deja_ecrit`) ou par match_id (index partiel v10_7) — AUCUNE par
match RÉEL.
MESURÉ le 2026-09-02, en base : **47 paires exactes** (même
match/selection/market_type, < 6 jours d'écart) + **7 paires floues**
vérifiées une à une = 54 lignes sur ~540 vivantes (~10 % du n). Aucune paire
WIN contre LOSS — les jumeaux concordent, le biais est un POIDS double, pas
un résultat faux — sauf une paire PUSH contre expired (Hellas Syrou).
3 lignes avaient en outre été réinsérées par l'audit manuel du 2026-09-01
(run 33493462880).
Correctif, deux moitiés :
1. Le FLUX — `core/db.py::_ledger_jumeau_reel`, appelée par `log_to_ledger` :
   clé EXACTE (match, selection, market_type) sur 6 jours ; même règle que
   `_ledger_deja_ecrit`, « le décisif gagne » — entrant décisif sur stocké
   non décisif → PROMOTION de la ligne stockée au lieu d'une seconde ligne ;
   fail-open sur panne de lecture (perdre un résultat réel serait définitif,
   un doublon se rattrape par archivage).
2. Le STOCK — `sql/migrate_v10_10_ledger_dedup.sql` : ARCHIVAGE (règle n°9,
   modèle v10_5), règle générique recalculée pour les exacts + liste d'ids
   MORTE pour les 7 flous, RLS refermée sur l'archive au passage. Le cluster
   Pachuca n'est traité que PARTIELLEMENT : impossible de trancher sur
   pièces si « CF Pachuca vs CD Guadalajara » désigne le féminin ou le
   masculin — on ne devine pas, la paire ambiguë reste. APPLIQUÉE le
   2026-09-02 (ops.py, sur instruction opérateur) : 66 lignes archivées —
   59 par la règle générique (les 47 inspectées + les 3 réinsérées par
   l'audit manuel du 01/09 + 9 ré-insertions de la vague de backfill du
   27/08, vérifiées sur pièces avant application : cote/edge/ttm/issue
   identiques à une ligne du 9-11/08, donc jumeaux, pas des revanches) +
   7 épinglées. Témoins passés : 0 doublon restant, 410 lignes vivantes,
   archive fermée à `anon`.
⛔ L'APPARIEMENT FLOU A ÉTÉ ESSAYÉ ET REJETÉ le même jour, ne pas le
remettre : `strict_team_match` sur les deux noms rendait des faux positifs —
Green Gully U23 apparié aux seniors, Kocaelispor U19 aux seniors, et
« Atletico Junior Barranquilla vs Deportiva Once Caldas » apparié à
« Atletico Nacional vs Deportivo Cali », matchs DIFFÉRENTS aux issues
différentes. D'où la décision : comparaison EXACTE seulement ; les libellés
divergents relèvent du pont d'alias, pas de cette garde.
⚠️ LIMITE ASSUMÉE, deux faces. Les jumeaux à libellés différents
continueront d'entrer tant que le pont d'alias ne converge pas — la garde
n'attrape que la forme majoritaire (47/54). Et les signaux jumeaux restent
ÉMIS en amont (double exposition Kelly le jour du pari) : la dédup à
l'émission a été écartée SCIEMMENT — un faux appariement y supprimerait un
vrai signal, et les doubleheaders MLB rendent la clé (équipes, jour)
ambiguë.
Invariant ajouté à AUDIT.md §2 : un match réel = UNE ligne de ledger.
Gardien : `tests/test_ledger_jumeaux.py` (10 tests).

### Closing line : `capture_from_scan` est morte avec OddsAPI (2026-08-26)

`capture_from_scan` (payload OddsAPI) est MORTE avec OddsAPI —
elle vit dans une branche que `ODDS_API_ENABLED=0` ne franchit plus.
`capture_from_exchange` (2026-08-26) la remplace sur les prix Matchbook que
chaque scan charge déjà, REPRICE compris → `closing_source='exchange'`.
⚠️ ELLE PREND LE DICT DE PRIX BRUT, PAS LES MATCHS ENRICHIS, et c'est tout
l'enjeu : `_enrich_from_exchange` n'écrase `odds_pinnacle` que sur les matchs
SANS prix sharp, or api-sports sert Pinnacle sur 100 % de ses matchs foot et
100 % des signaux sont du foot. Lire le match enrichi stockerait le prix
d'ENTRÉE comme prix de clôture : CLV nul partout, exécution verte, aucune
trace. Football sans prix de nul = REFUS (jamais de repli sur le moneyline :
comparer une entrée DNB à une clôture ML donne un CLV faux et silencieux).
Gardien : `tests/test_closing_line_exchange.py`.


## Couche IA

Le paysage des paliers gratuits change tous les mois. Rien de ce qui suit
n'est stable, et c'est le fait le plus important de cette section.

### Couche IA : registre, lanes, disjoncteur (mission 4, 2026-08-22)

(mission 4, 2026-08-22) : `core/ai_router.py` = registre +
lanes (FILTER/ANALYZE/TRANSLATE_CJK/SEARCH_READ/SETTLEMENT) + disjoncteur
(3 échecs → 30 min) + découverte des catalogues au démarrage du run.
NE JAMAIS coder un nom de modèle en dur hors du registre : le paysage gratuit
churne chaque mois. SEUL mort prouvé : GitHub Models (410, corps nommant le
retrait) ; aussi mort : `meta-llama/llama-3.3-70b-instruct:free` (retiré du
catalogue :free OpenRouter — le repli était mort en silence) ; et depuis le
2026-08-26 `llama-3.3-70b-versatile` + `llama-3.1-8b-instant`, DISPARUS du
catalogue Groq (14 modèles, plus aucun llama de génération). Groq tourne
désormais sur `qwen/qwen3.8-27b` — le SEUL instruct du nouveau catalogue :
les `openai/gpt-oss-*` sont des modèles de RAISONNEMENT qui rendent un
contenu VIDE sous les plafonds serrés du pipeline (max_tokens=80 pour un
alias), ils restent en repli pour les appels à 2048. Corollaire : le palier
`tier` de `ai_complete` ne réordonne PLUS les modèles (il ne choisit que la
lane du repli) — inverser remettrait un modèle de raisonnement en tête.
⚠️ Cette panne-là a coûté cher parce que `ai_search.py` portait TROIS copies
à la main des modèles Groq et appelait Groq EN DIRECT : le routeur écartait
Groq proprement pendant que le vrai chemin d'appel tapait un modèle mort en
404, avec backoff, jusqu'au timeout global de 540 s qui tuait le Deep Scan
du matin. Les listes sont maintenant DÉRIVÉES (`ai_search._groq_models`) et
gardées par `tests/test_ai_router.py::TestAucunModeleEnDurHorsDuRegistre`,
qui refuse tout littéral de modèle du registre ailleurs que dans
`ai_router.py` (vérifié sur l'AST : un commentaire a le droit de nommer un
modèle mort pour raconter pourquoi il l'est).
⚠️ JAMAIS DORMIR DANS LE MOTEUR SUR UN 429-MINUTE GROQ (2026-08-26, run
Guerrilla 32990495899) : une 4e org neuve (`GROQ_API_KEY_5`) répondait à la
limite par minute, l'ancien backoff dormait 20 s puis 40 s à chaque
recherche Oracle, jusqu'au timeout global de 540 s — exit 1, ZÉRO signal,
là où trois clés MORTES en émettaient 12. Une clé vivante mais bridée
faisait pire qu'une clé morte. Désormais `_groq_cooldown_until` : la clé
passe en cooldown (délai lu dans la réponse, borné 5-60 s) et la main est
rendue tout de suite ; gardé par
`tests/test_settlement.py::test_un_429_minute_groq_ne_dort_plus_dans_le_moteur`.
⚠️ Cerebras avait été retiré À TORT sur un 403 SANS CLÉ : un 401/403 sans clé
ne prouve JAMAIS qu'un palier a fermé, il faut une clé INVALIDE pour trancher
(Cerebras rend alors 401 wrong_api_key). Rétabli au registre.
`ai_search.py` délègue au routeur ; Mistral y est ENTRÉ le 2026-08-26.
Réserve settlement gardée EN NÉGATIF (les autres lanes sont amputées, elles
n'y accèdent jamais) — leçon du 2026-08-02. Un compte par fournisseur ;
`terms_flag` (non_commercial/evaluation) = exclu de la production par défaut.
Zéro fournisseur configuré n'alerte PAS (sinon spam Telegram en mode REPRICE).
RÉPARTITION 24h : `lane_providers(balanced=True)` trie par budget RESTANT, pas
par ordre du registre — sinon le 1er fournisseur est drainé pendant que les
autres restent intacts (mesuré : 240 appels tous sur Groq → 42 après). Ordre du
registre = départage à égalité seulement. `ai_complete` interroge le ROUTEUR
AVANT Groq (Groq = seul à porter compound-mini/recherche web, quota irremplaçable) ;
`ai_search_complete` garde l'ordre inverse. Budget Groq = 160 req/j, dérivé de
son TPD 100k et non d'un nombre de requêtes inventé.
Un catalogue lisible ne prouve RIEN : Cerebras/SambaNova/Chutes rendent 200 sur
/models et 402 à l'inférence ; Scaleway rend 429 quota-zéro. `ops.py ai` fait le
vrai appel — c'est le seul diagnostic qui tranche.

### Wiz a été SUPPRIMÉ le 2026-08-26 — et Mistral est entré au registre IA

(page, moteur, workflow, tests, lane du
routeur) — décision opérateur : « la page wiz ne me sert pas ». Ne pas le
réintroduire par inadvertance en recréant `/wiz` ou `core/wiz_*`. La table
`wiz_analysis` a été SUPPRIMÉE de la base le 2026-08-26 (DROP, 748 lignes,
décision opérateur explicite après présentation de l'option d'archivage —
`sql/migrate_v10_6_drop_wiz.sql`, appliquée). Il n'existe AUCUNE archive :
ne pas chercher `wiz_analysis_archive`, elle n'a jamais existé.
Conséquence directe : MISTRAL EST ENTRÉ AU REGISTRE IA. Il en était exclu
parce qu'il était le fournisseur unique de Wiz (domaine de panne isolé) ;
son quota sert désormais la RECHERCHE DE SIGNAUX, lanes `filter`/`analyze`
seulement. PAS `settlement` (2 req/min, la réserve doit répondre vite) et
PAS `search_read` (son connecteur `web_search` avait son quota épuisé au
niveau du COMPTE — l'y enrôler promettrait une capacité inexistante).
⚠️ Ses modèles et ses budgets sont les SEULS du registre à n'avoir jamais
été validés par une inférence réelle : la clé n'est pas disponible en dev.
Premier geste après déploiement : `python scripts/ops.py ai`.
Gardien : `tests/test_ai_router.py::TestLanes`.


### Périmètre : marchés morts et ligues non réglables BANNIS (2026-09-03)

Décision opérateur, dans ses mots : « ligues sans sources fiables,
artefacts et marchés morts à bannir ; garder prix sharps réels avec
liquidité et ligues réglables avec nos outils ». La question posée était
« doit-on délaisser les ligues mineures maintenant que les grands
championnats commencent ? » — et la réponse mesurée était non : la ligue la
plus perdante du ledger est la Primera División argentine (10–12), pas une
division mineure ; en revanche 8 des 14 signaux en souffrance de règlement
ce jour-là venaient de divisions qu'aucune source gratuite ne couvre
(Azerbaïdjan 1. Liga, Kakkonen finlandaise, coupe de Géorgie, playoffs U20
NSW…). Un pari qu'on ne peut pas régler n'apprend rien, gonfle les expirés
et nourrit le biais de survie. La frontière utile n'est pas « mineur /
majeur », c'est « tarifable et réglable / ni l'un ni l'autre ».

Deux gardes DÉRIVÉES des données du run, aucune liste de ligues à la main
(règle n°6), dans `run_engine._filtrer_perimetre`, AVANT la photographie
du slate (un match banni ne revient pas par le cache du tick reprice) :

1. **MARCHÉ VIVANT** — un match du Tier 2 n'entre que si un exchange
   confirme son prix sharp (`odds_exchange` par la contre-expertise, ou
   `_exchange` par le bouche-trou de `_enrich_from_exchange`). L'exchange ne
   publie un marché qu'avec back ET lay serrés (`core/matchbook.py`,
   MAX_SPREAD_RATIO) : c'est la liquidité mesurée. Une copie « Pinnacle »
   d'api-sports/titan007 sans marché d'exchange derrière est un prix que
   personne ne prend — un marché mort, la fabrique d'edge d'artefact d'A6.
   Le Tier 1 OddsAPI (sans `_soft_source`) est un flux Pinnacle réel : il
   passe. Mesuré sur le scan standard de 14:38 : Matchbook confirmait 14
   matchs du Tier 2.
2. **RÉGLABLE** — api-sports règle ses propres fixtures dans la fenêtre de
   son plan, MLB statsapi règle le baseball, ESPN règle ce qu'il LISTE
   (`core/score_sources.fixtures_espn`, à venir compris, une requête par
   sport présent sur la fenêtre des coups d'envoi du run). Tout le reste est
   refusé, et ESPN muet sur un sport qu'il couvre (panne) vaut REFUS, pas
   laisser-passer : le tick suivant réessaie dans l'heure. Conséquence
   assumée : la BOXE et le TENNIS, sans source de scores structurée, ne
   sortent plus (le MMA, lui, se règle désormais par le drapeau `winner`
   des combats ESPN, à deux athlètes sans `homeAway`).

Chaque refus est loggé (`MARCHÉ MORT |`, `NON RÉGLABLE |`, bilan
`PÉRIMÈTRE | n → vivants → réglables`). Le volume de signaux va BAISSER —
c'est le but — et la première mesure honnête est le bilan `PÉRIMÈTRE` des
prochains scans, à relire avant de toucher quoi que ce soit.

PREMIÈRE MESURE, scan standard de 19:43 UTC le jour même : 40 matchs avec
prix sharp → 20 marchés vivants → 13 réglables, 1 signal émis. Et un FAUX
NÉGATIF dedans : « VfB Stuttgart vs 1. FC Köln » refusé comme « ligue non
couverte » — ESPN écrit « FC Cologne », et « Tepatitlán FC » pour
« Tepatitlan de Morelos ». Corrigé le soir même : le test de COUVERTURE
(`fixture_connue`) accepte UN nom apparié strictement, accents repliés
(`_fold`), dans la fenêtre ; le RÈGLEMENT ESPN (`_espn_paire`) exige
toujours les deux noms — un match couvert sous un autre libellé reste
réglable par api-sports dans sa fenêtre. Audit de 20:28 le même jour, premier
avec ESPN depuis les runners : 7 réglés (0 aux deux audits précédents),
aucun 403. Gardien : `tests/test_perimetre.py::TestCouvertureTolerante`.
Gardiens : `tests/test_perimetre.py`, `tests/test_score_sources.py::TestESPN`.

### ESPN, source de scores OUVERTE, avant TheSportsDB ; api-sports sauté hors de son plan (2026-09-03)

Deux audits stériles le même jour (10:38 et 15:45 UTC, run 33774472425 :
« 0 réglé sur 15 éligibles »). Le log dit tout : « api-sports[soccer]
résultats 2026-08-31 : Free plans do not have access to this date, try from
2026-09-02 to 2026-09-04 » (15 des 25 lookups du jour brûlés sur des refus
CERTAINS — le plan gratuit ne sert que J-1 … J+1) puis
« score_sources[tsdb_results]: budget journalier atteint (150) ». Décision
opérateur : « pour les résultats chercher sources open, pas TheSportsDB ».

MESURÉ avant d'écrire : le scoreboard public ESPN
(`site.api.espn.com/apis/site/v2/sports/<sport>/<ligue>/scoreboard?dates=A-B`)
est SANS CLÉ, sans compte, sans quota publié ; `soccer/all` rend TOUTES les
ligues de foot du monde en UNE requête par fenêtre de 3 jours (702
événements terminés du 29 au 31 août, de la Premier League à la J.League),
avec `status.type.completed`/`state`, camps `homeAway` et scores. Essai réel
sur les 14 signaux en souffrance du jour : 6 réglables du premier coup
(J.League, Premiership écossaise, Championship, Colombie D1, Paraguay — et
l'expiré « AD Pasto vs Deportivo Pereira » du 31 août, 1-2). Les 8 autres
sont des divisions que ni ESPN ni personne de gratuit ne couvre (1. Lig
turque, Vtora Liga, coupes roumaine/italienne C, Azerbaïdjan, Finlande D3,
Géorgie) : ils suivent le chemin normal actif → expired → relance.

⚠️ PIÈGE MESURÉ : ESPN filtre sur la FORME du User-Agent, pas sur l'adresse —
« PREDATOR/1.0 » et un UA de navigateur reçoivent 403, « curl/8.5.0 »,
« Python-urllib/3.11 » et « produit/version (+url) » reçoivent 200. C'est
la « leçon ESPN/SofaScore » de l'entrée suivante, résolue : ce n'était pas
Azure, c'était l'en-tête. `_ESPN_USER_AGENT` (surchargeable par
`ESPN_USER_AGENT`), requête routée par `core.net` (relais/proxy `ESPN_PROXY`
ou `FREE_SOURCES_PROXY` si la règle change).

Fait : `core/score_sources.result_from_espn` (même contrat : deux noms
appariés strictement, candidat unique, statut terminé, None sur panne ;
cache de run par (chemin, fenêtre) ; budget `espn_results` 200/j), chaîne
`fetch_score` = MLB (baseball) → ESPN → TheSportsDB ; table `_ESPN_PATHS`
par sport (soccer/all, nba, wnba, euroleague, nhl, mlb, nfl,
college-football, afl, NRL) — MMA/boxe/tennis absents (athlètes, pas
d'équipes) ; `core/settlement._hors_fenetre_api_sports` : hors
J-`API_SPORTS_FREE_WINDOW_DAYS` (1), api-sports n'est plus appelé et ses
lookups sont préservés pour les matchs de la veille.
Gardiens : `tests/test_score_sources.py::TestESPN`, `::TestChaineAvecESPN`,
`tests/test_settlement.py::…::test_api_sports_saute_hors_de_la_fenetre_du_plan_gratuit`.

### odds500 derrière un mur anti-bot EdgeOne depuis le 1er septembre (2026-09-03)

`ops.py sources` disait « odds500 KO — 0 matchs au calendrier » et le quota
journalier était tombé de 400 (30 août) à 14 (3 septembre) sans qu'aucun log
n'accuse quoi que ce soit. Lu à la main via le relais : le calendrier n'est
plus une page mais un script obfusqué de ~1 ko qui pose un cookie
`EO_Bot_Ssid` — le défi JavaScript de Tencent EdgeOne. Sans moteur JS, la
source est MUETTE, relais Cloudflare ou pas ; ce n'est plus un filtrage par
IP (celui-là, le relais le contournait), c'est une porte fermée à tout
client qui n'exécute pas de JavaScript.

Fait : `core/odds500.mur_anti_bot` reconnaît le défi et `_get` rend None en
loggant « MUR ANTI-BOT » en clair — plus jamais « 0 match » pour une panne.
Pas fait, et c'est une DÉCISION OPÉRATEUR : retirer la mission 3 odds500
(module, relais, scorecard, alias chinois) si le mur dure, ou la garder
dormante (coût : 1 à 2 requêtes de calendrier par scan). Gardien :
`tests/test_odds500.py::TestMurAntiBot`.

### Le LineFeed 1xbet/Melbet/22bet retiré du harvest (2026-09-03)

Même décision opérateur (« si une source est inutilisable, il faut la
dégager »), même jour. Le LineFeed direct des books soft était bloqué par IP
depuis les runners GitHub depuis août (documenté ici même, « LineFeed/ESPN/
SofaScore sont morts ») mais restait CÂBLÉ : mesuré sur le scan standard de
19:43, neuf requêtes en HTTP 203/404 pour le seul football, chacune
précédée d'un sommeil de 2 à 5 s — ~36 s de budget moteur perdues par sport,
quatre fois par scan, à ne rien rendre. Une source morte laissée en place
coûte du temps et fait croire à une capacité.

Retirés de `core/harvester.py` : les gabarits d'URL, `SOFT_BOOKS`,
`_fetch_from_book`, `_parse_xbet_json`, les en-têtes de navigateur. Les
books soft (1xbet, Bet365) arrivent par odds-api.io (authentifié, non filtré
par IP) ; `fetch_matches` itère l'union DÉRIVÉE des sports que servent
api-sports, odds-api.io et titan007. La clé `odds_1xbet` garde son nom
historique dans tout le pipeline (slate, ledger). `validator.py`, script
manuel qui sondait ce feed, suit. Gardien :
`tests/test_harvester.py::TestFetchMultiBook::test_le_linefeed_est_parti`.

### Six fournisseurs IA morts retirés du registre (2026-09-03)

Décision opérateur : « supprimer les IA non opérationnelles comme zhipu et
cloudflare ». Vérifié par appel RÉEL le jour même (`python scripts/ops.py ai`,
seul diagnostic qui tranche) : 4 fournisseurs configurés sur 12 répondaient
(gemini, ollama_cloud, cohere, upstage). Retirés du registre
(`core/ai_router.py`), donc des pools CI (`ci_env.py --write`) :
cerebras, chutes, sambanova (402 payment_required — carte obligatoire),
scaleway (429 INSUFFICIENT QUOTA — quota à zéro), cloudflare (401 clé
refusée), zhipu (401 token expired). Un fournisseur mort au registre coûte un
appel raté par run et une ligne de log qui fait croire à une capacité.
Gardés : openrouter et nvidia_nim (erreurs transitoires ce jour-là, vivants
auparavant), cohere et upstage (répondent, marqués hors production par leurs
CGU), et les cinq jamais configurés (nebius, ovh, modelscope, siliconflow,
mistral) — des options, pas des pannes. Le registre passe de 17 à 11, dont 8
sans clause restrictive ; le minimum de 2 fournisseurs sains par lane tient.
Geste opérateur restant : supprimer les secrets GitHub correspondants
(docs/actions_operateur.md §6). Gardiens : `tests/test_ai_router.py`
(fournisseurs morts absents), `tests/test_ci_env.py::CLES_SUPPRIMEES`.

### Groq et Tavily SUPPRIMÉS — le settlement est déterministe (2026-09-02)

Décision opérateur, dans ses mots : « j'en ai marre de groq et tavily
toujours épuisé, on va les supprimer et les remplacer par quelque chose de
plus efficace, open, gratuit ».

Le constat qui l'a déclenchée : DEUX famines de settlement en une semaine
(2026-08-26 puis 2026-09-01, « AUDIT STÉRILE — 0 réglé sur 3 éligibles »,
Tavily 29/29 du jour ET compound-mini KO), pour un besoin qui n'exigeait pas
d'IA. Un score final est une DONNÉE STRUCTURÉE publiée par des API gratuites ;
le demander à un LLM, c'était payer deux quotas, un prompt et un parseur de
JSON approximatif pour une information qui existe en champ.

CE QUI REMPLACE, mesuré le jour même avant d'écrire une ligne :
  - `core/score_sources.py` — MLB statsapi (officiel, SANS CLÉ, 1 requête
    par journée) et TheSportsDB (clé publique gratuite « 123 »,
    `THESPORTSDB_API_KEY` pour un compte Patreon). ⚠️ La voie « tous les
    matchs du jour » (eventsday) est PLAFONNÉE À 3 ÉVÉNEMENTS en gratuit —
    inutilisable. La voie qui marche est PAR ÉQUIPE (searchteams →
    eventslast) : elle a retrouvé du premier coup les deux signaux en
    souffrance depuis >26 h (Hapoel Akko 0-3 Bnei Yehuda, D2 israélienne, FT).
  - ⚠️ searchteams est FLOU : « AD Pasto » rend « Pastoreo » (équipe sans
    ligue), et `strict_team_match` l'accepte par containment. La recherche
    d'équipe n'est qu'un GÉNÉRATEUR DE CANDIDATS — ce qui règle, c'est
    l'événement complet : les DEUX noms appariés + la date (±1 jour),
    candidat UNIQUE, statut TERMINÉ seulement (TheSportsDB et statsapi
    publient les scores EN DIRECT — régler à la 70e minute écrirait un
    WIN/LOSS faux et définitif). Même contrat que `result_from_api_sports`.
  - Budgets journaliers partagés (`daily_quota`), SANS rythme horaire :
    étaler le settlement était une faute (2026-08-28), la leçon tient.

CE QUI EST PARTI AVEC EUX — parce que sans Tavily/compound-mini ces chemins
étaient morts, et qu'une capacité morte laissée en place est la panne n°6 :
  - `core/oracle.py` (SUPPRIMÉ) : « estimer la cote Pinnacle » par LLM était
    déjà à zéro par défaut (2026-08-27, « une génération plausible, pas une
    observation ») ; il servait encore la passe CLV de l'audit et le job
    closing line. Remplacé par ce qui existait déjà : la passe CLV lit la
    colonne `closing_pinnacle_price` capturée par les scans, et
    `run_closing_line.py` rejoue `capture_from_exchange` sur des prix
    Matchbook frais (gratuits, illimités, RÉELS).
  - `harvester.fetch_pinnacle_prices` (prix sharp groupés par LLM — le
    « chemin dominant » que l'en-tête d'oracle.py désignait),
    `fetch_estimated_prices` (Tier 3, cotes de mémoire d'entraînement) et
    `_fetch_from_gemini` (slates inventés avec « cotes 1XBet réalistes »).
    Tous fabriquaient des prix qu'aucun book n'a affichés — la fabrique à
    faux edge d'A6. Un match sans prix sharp RÉEL est écarté, point.
  - La lane `settlement` du routeur et sa réserve tenue en négatif
    (`AI_SETTLEMENT_RESERVE`), la lane `search_read`, le fournisseur `groq`
    du registre, les clés `GROQ_API_KEY(_2…_5)` et `TAVILY_API_KEY` de tous
    les pools CI (`ci_env.py --write` rejoué), `search_exhausted`/
    `search_credits_left`/`prioriser_settlement`, les gardes IA de
    `audit_one` et le `SystemExit` « GROQ_API_KEY absente » de l'audit.

CE QUI NE CHANGE PAS : l'ordre (api-sports reste l'étage 1), le refus
plutôt que la devinette, `EXPIRE_AFTER_H`, la relance des expirés (qui
profite même de la voie par équipe, utilisable SANS date : paire unique
exigée dans les 15 derniers résultats), l'alerte d'audit stérile et le
marqueur `settlement_starved_at`. La couche IA (alias CJK, analyse) tourne
sur les ~15 autres fournisseurs du routeur.

⚠️ CE QUI N'EST PAS ENCORE PROUVÉ : la joignabilité de statsapi.mlb.com et
thesportsdb.com DEPUIS LES RUNNERS GitHub (leçon ESPN/SofaScore : un test
depuis un poste de dev ne prouve rien). Vérifié depuis ce Codespace (Azure,
comme les runners) — encourageant, pas concluant. Premier audit à surveiller ;
en cas de 403, router par `FREE_SOURCES_PROXY` comme odds500.
⚠️ Sports sans source structurée (tennis surtout, revenus avec OddsAPI) : un
score introuvable suit le chemin normal actif → expired → relance. Si le
tennis émet en volume, il faudra lui trouver une source de scores.
Gardiens : `tests/test_score_sources.py`, `tests/test_settlement.py::
TestFetchMatchResult::test_no_ai_layer_involved`,
`tests/test_ci_env.py::test_aucun_pool_ne_transmet_groq_ou_tavily`,
`tests/test_ai_router.py::TestGroqEtLaneSettlementSupprimes`.


## CI, secrets, déploiement

Cinq workflows sur six ont tourné à vide pendant une journée sans produire
un seul log. C'est le mode de panne le plus coûteux du dépôt.

### Les blocs de secrets des workflows sont GÉNÉRÉS, jamais écrits à la main (2026-08-26)

(2026-08-26). `python scripts/ci_env.py --write` les régénère depuis les
pools de `scripts/ci_env.py`, eux-mêmes DÉRIVÉS de `core.ai_router.REGISTRY` ;
`tests/test_ci_env.py` compare chaque bloc à sa source à chaque exécution.
Les blocs sont posés par STEP, pas par job : c'est ce qui garantit que le
step REPRICE ne reçoit aucune clé payante — garantie lisible dans le YAML.
Le registre portait 18 fournisseurs quand les workflows n'en câblaient que
15 : la divergence était déjà là.
⛔ NE JAMAIS ÉCRIRE `${{ toJSON(secrets) }}` DANS UN WORKFLOW. La première
version de cette refonte exposait tout d'un coup et filtrait à l'exécution.
GitHub REFUSE de faire tourner un tel workflow : « GitHub detected that this
workflow file may be malicious. It will not run until someone with write
access approves it. » — conclusion `action_required`, ZÉRO job créé, aucun
log, aucune annotation, sur TOUT événement. CINQ des six workflows sont
restés muets ainsi (scan, closing line, audit, rapports, outils) ; seul
`ci.yml`, dépourvu de l'expression, tournait. Le message n'apparaît QUE sur
la page HTML du run — ni l'API des runs, ni les jobs, ni les check-runs ne le
disent. Et la détection a RAISON : ce dump était lisible par chaque step du
job, `actions/checkout` et `pip install` compris. Ne pas chercher à
contourner : ce serait évader un contrôle de sécurité pour rétablir une
pratique dangereuse.
⚠️ Ne jamais utiliser le contexte `inputs` nu dans un `if:` de job non plus
(`github.event.inputs.*`) : il n'existe qu'en workflow_dispatch.
Les 4 scans sont fusionnés dans `scan.yml`, le mode vient du cron qui a tiré
(`scripts/ci_scan_mode.py::CRON_MODES` — un cron ajouté sans sa ligne fait
échouer le run ET le test).
Gardiens : `tests/test_ci_env.py`, `tests/test_workflow_secrets.py`.

### Le scheduler GitHub ne livre qu'une fraction des crons (2026-08-27)

Mesuré sur les ~21 h suivant la refonte du 26/08 : `closing_line.yml` livré à
~5 % (3 ticks sur ~63 attendus), l'audit de 12:00 UTC jamais tiré, un trou de
9 h 19 sur `scan.yml` — alors que CHAQUE run livré était vert et de durée
normale. Ce n'est pas le dépôt : c'est GitHub qui ne tire pas, même à 124
déclenchements/jour (la réduction du 26/08 n'a pas suffi). Conséquence
mesurée : 72 signaux h2h passés sans prix de clôture, couverture CLV réduite
à 12 lignes sur 112 en 7 jours — pendant que `learning_layer` fait du CLV un
critère de premier rang.

Remède : le chien de garde `scripts/cloudflare_watchdog_worker.js` (Worker
Cloudflare, cron `*/10`, déployé par `scripts/deploy_watchdog_worker.py`).
Il ne dispatche un `workflow_dispatch` de rattrapage QUE si le dernier run du
workflow est démontrablement en retard (seuil > cadence nominale, gardé par
`tests/test_watchdog_worker.py`). Ce n'est PAS l'erreur du 2026-07-07 : on
n'ajoute aucun schedule GitHub, on vit HORS du scheduler défaillant. Deux
règles à ne pas défaire : `scan.yml` n'est rattrapé qu'en `reprice` depuis le 2026-09-03 (avant : `golden`, gratuit —
rattraper standard/deep/guerrilla doublerait la dépense des budgets des
sources gratuites), et les seuils restent au-dessus des cadences (le chien de
garde ne peut pas tirer plus vite que le schedule qu'il supplée). Le PAT est
un secret du Worker (`WATCHDOG_PAT`), jamais dans le JS.

### Le timeout du moteur se déclenchait — et le run continuait (2026-08-28)

Run golden 15:40 UTC (rattrapage watchdog, budget 600 s) : `15:50:56 ERROR
TIMEOUT: Engine exceeded 600 seconds — exiting gracefully`. Puis **sept
minutes de plus** — alias IA jusqu'à 15:54, Tier 2 jusqu'à 15:58. Le tick a
tenu 17 min 44 s sous le verrou `predator-signals-write`, dont
`cancel-in-progress: false` fait attendre tous les autres.
Cause : `_timeout_handler` levait `TimeoutError`, qui dérive d'`Exception`.
Le moteur est truffé d'`except Exception` « jamais bloquants » (sources, IA,
alias — chacun justifié séparément), et `core.net._TRANSIENT` retente sur
`TimeoutError` comme sur une coupure réseau. L'alarme est tombée pendant un
appel IA de la boucle d'alias : attrapée, loggée en debug, boucle suivante.
Le filet D3 était bien dimensionné et bien armé ; il n'était simplement pas
inarrêtable.
Correction : `run_engine.EngineTimeout(BaseException)`. Rien de ce qui
attrape `Exception` ne le voit passer, et `finally` s'exécute quand même. Le
comportement voulu — sortie, traceback, exit 1, run en ÉCHEC par le contrat —
est celui qu'on croyait avoir depuis le 2026-08-27.
Gardien : `tests/test_timeout_par_mode.py::TestLeTimeoutNestPasAvalable`.

### Cinq modes de scan, deux qui servent — golden, deep et guerrilla supprimés (2026-09-03)

Symptôme : l'opérateur ne comprend plus ce qui tourne (« trop de workflows
et de process, je ne comprends pas golden, guerrilla… »). Et il avait
raison de ne pas comprendre, parce que trois des cinq modes ne servaient à
rien de rentable :

- **golden** (T-2h, 24 runs/jour, cron `25 * * * *`) était 100 % FANTÔME PAR
  CONSTRUCTION depuis `SHADOW_GOLDEN_HOUR` (2026-08-04 : 39 % de réussite
  sur la tranche pour 54,5 % requis, p=0,007) — vingt-quatre runs par jour
  dont aucun ne pouvait recommander quoi que ce soit, et qui portaient le
  Tier 1 OddsAPI depuis le 2026-09-01 : des crédits dépensés sur une tranche
  que le système ne joue pas. C'est lui qui a créé les fantômes affichés par
  erreur (entrée précédente).
- **deep** (2 runs/jour) : un standard avec `MAX_MATCHES=100` et des quotas
  élargis, MÊME fenêtre de 24 h ; tué deux fois par timeout, jamais de mesure
  de rendement.
- **guerrilla** (2 runs/jour) : horizon 48 h sans Tier 1 — la tranche 24-48 h
  fait −62,8 % (p=0,0023) et sort de la zone jouable ; ses quatre variables
  `CACHE_*_TTL_H` n'avaient plus aucun lecteur.

Fait : `scripts/ci_scan_mode.py` n'a plus que DEUX lignes — `standard`
(scan complet, 8×/jour, fenêtres favorables, inchangé) et `reprice`
(horaire, gratuit, `REPRICE=1` posé par `MODE_ENV` et plus par le step
YAML) ; `scan.yml` deux crons, dispatch `[standard, reprice]`, un seul
`run_engine.py` par tick ; `run_engine.py` sans `DEEP_SCAN`/`GOLDEN_HOUR`/
`GUERRILLA`, sans `GOLDEN_SPORT_KEYS`, un seul `SPORT_QUOTA`,
`MAX_MATCHES = 50`, horizon 24 h unique ; `SCAN_TIMEOUTS` à deux entrées ;
`SpendPolicy` sans `imminent_mode` ; la règle fantôme T-2h vaut PAR SIGNAL
(`_shadow_reason`, deux raisons), plus aucune par mode ; le bouton Scanner
promeut reprice → standard ; le chien de garde rattrape en `reprice`.
32 invocations/jour au lieu de 60, 8 scans payants au lieu de 34, 0 fantôme
créé par mode. Doc opérateur : `docs/systeme_de_scan.md`.

Pas fait, et dit : le TTL du slate soft reste à 4 h (deux ticks reprice
muets/jour, 06:25 et 16:25 — `CACHE_SOFT_SLATE_TTL_H=5` se pose sans code
si on veut zéro tick muet, après mesure) ; `SHADOW_GOLDEN_HOUR` garde son
NOM (c'est celui de la tranche dans perf_view/learning_layer, pas du mode) ;
les lignes `shadow_reason='golden_hour'` restent en base (règle n°9) ; la
closing line sur payload payé à T-2h ne tourne plus qu'aux 8 ticks standard
— entre deux, exchange (h2h) et `closing_line.yml` ; perte non mesurée, à
lire dans la couverture CLV totals/spreads du rapport hebdo.

⚠️ Ordre de déploiement : le Worker Cloudflare (`scripts/deploy_watchdog_worker.py`,
geste opérateur) AVANT le merge — `reprice` existe des deux côtés, alors
qu'un Worker resté en `golden` ferait échouer chaque rattrapage
(`mode inconnu : 'golden'`) toutes les 75 min de retard GitHub.

Gardiens : `tests/test_ci_env.py::test_il_ny_a_que_deux_modes`,
`…::test_le_dispatch_de_scan_yml_noffre_que_les_modes_connus`,
`…::test_reprice_vient_de_mode_env_pas_du_yaml`, `tests/test_timeout_par_mode.py`
(`SCAN_TIMEOUTS == MODES`, anciens drapeaux absents), `tests/test_watchdog_worker.py`,
`tests/test_signaux_fantomes.py::TestRaisonDuFantome::test_aucune_raison_par_mode`.

### Le verrou `predator-signals-write` ne contient plus `closing_line.yml`

raison courante (« aucune ligne en commun ») est FAUSSE : `purge_rules`
(`_purge_old_signals`) supprime des lignes actives sur des critères de
qualité SANS filtre sur `match_time`, donc à coup d'envoi futur. Ce qui tient :
le settlement, lui, ne peut pas les toucher (`match_time` de part et d'autre
de `now`), et la purge ne peut que SUPPRIMER — Postgres sérialise déjà cette
course ligne par ligne. Au pire une écriture est perdue sur une ligne
condamnée. Le verrou n'achetait pas de correction, il achetait de l'attente
derrière un deep scan de 25 min.

### DEUX interpréteurs, subis et non choisis

`.python-version` + `vercel.json`
= **3.12** (l'image de build Vercel n'embarque PAS 3.11 — l'y « aligner »
casse le déploiement et laisse la prod sur le commit précédent, vécu le
2026-08-22) ; les 6 workflows, l'action `.github/actions/setup` (qui porte
désormais l'unique `setup-python` du dépôt) et le dev local = **3.11**.
Gardé par `tests/test_workflow_secrets.py`.

### Une suite verte ne prouve RIEN sur le déploiement

aucun test ne déploie.
Après toute retouche de `vercel.json`, `.python-version`, `requirements.txt`
ou `api/index.py` : `python scripts/ops.py vercel deployments | head -3`
(READY, pas ERROR) puis `curl .../api/health`.

### Secrets : `app_secrets` bat `os.environ`, même périmée

`core/secret_store.py` (table Supabase `app_secrets`) bat `os.environ` ;
une valeur périmée dans la table gagne quand même.


## Dashboard et base

### Le dashboard écrit DEUX fois

`/api/scan` (demande de scan dans `meta`,
cooldown 120 s) et `/api/audit/run` (déclenche `audit.yml`). Cette dernière
exige `DASHBOARD_ADMIN_TOKEN` et ÉCHOUE FERMÉ depuis le 2026-08-22 — elle
était ouverte à tout Internet. Ne pas « réparer » son 401 en retirant la
garde.

### Trois zéros à l'écran, deux étaient faux (2026-08-28)

Dashboard à 12:41 UTC : « 0 matchs », « Aucun signal haute valeur », « Prochain
scan automatique : — ». Un seul de ces trois constats était vrai (0 signal —
résultat de marché, sources saines, 41 matchs tous écartés à EV négative).
Les deux autres étaient des artefacts, et ils se cumulaient précisément sur la
page que l'opérateur regarde quand il n'y a rien :

1. **Le heartbeat du vrai scan était écrasé par le step REPRICE.** Le scan
   golden écrivait « 41 matchs » à `meta.last_scan` ; six secondes plus tard le
   step REPRICE du même tick, cache expiré → exit, réécrivait « 0 matchs ».
   Un tick qui n'a pas scanné rafraîchit désormais `at` (preuve de vie, le
   test REPRICE l'exige) mais CONSERVE les comptes du dernier scan réel —
   `_heartbeat(sb, now, None, None)` sur les trois sorties anticipées.
2. **L'état vide tuait tout le script du template.** La branche « aucun
   signal » ne rend ni `#sport-chips` ni `#signals-list`, mais l'init des
   filtres tournait inconditionnellement : TypeError, et tout le bloc script
   restant mourait — dont le compte à rebours « prochain scan », laissant les
   deux textes de repli figés (« — » en bas, « prochain <30min » en haut, qui
   n'étaient PAS une divergence de données mais deux victimes du même crash).
3. **Et ce compte à rebours mentait de toute façon** : il visait :00/:30 en
   dur alors que le tick golden est horaire depuis le 2026-07-23 — une liste
   recopiée qui avait divergé (règle n°6). Les instants de tir sont désormais
   DÉRIVÉS de `scripts/ci_scan_mode.py::CRON_MODES` par `api/index.py`
   (`_scan_cron_specs`) et injectés dans le template ; un cron d'une forme
   nouvelle lève à l'import, jamais en silence à l'écran. C'est l'heure
   PLANIFIÉE : le scheduler GitHub sous-livre, le chien de garde rattrape.

Gardiens : `tests/test_reprice_mode.py::test_reprice_empty_cache_preserves_last_scan_counts`,
`tests/test_dashboard_sports.py::TestCompteARebours`.

### Le digest Telegram annonçait un pari sur un match commencé (2026-08-28)

15:20 UTC, digest « APRÈS-MIDI » : « FK Akron Tolyatti vs CSKA Moscow ·
15:00 UTC → CSKA @ 1.39 · valeur +6.1% ». Coup d'envoi vingt minutes plus
tôt. Le signal était légitime (émis 14:27, T-33 min, CLV réel +5 %) — mais
`run_rapport` filtre `status='active'` et `created_at` dans les 2 h, jamais
`match_time` : `active` ne tombe qu'à l'audit, toutes les 6 h. À 17:03 le
dashboard disait « 0 sig » pour le même signal, parce que LUI filtre
`match_time > now` (`api/index.py::_is_playable`) — deux lecteurs, deux
règles, et l'opérateur qui demande « où sont mes signaux ? ».
Correction : `run_rapport._a_venir` — même règle que le dashboard. Un signal
sans coup d'envoi lisible est conservé (on ne sait pas), pas jeté.
Gardien : `tests/test_rapport_signaux_a_venir.py`.

### Les fantômes s'affichaient sur le dashboard comme des paris à poser (2026-09-03)

Depuis le 2026-08-04, les signaux de la golden hour (< T-2h) sont FANTÔMES :
mesurés, réglés, appris, mais retirés de Telegram (`SHADOW_GOLDEN_HOUR`,
`run_engine._shadow_partition`). Le fantôme n'était qu'un filtre sur la liste
envoyée à Telegram ; la ligne partait en base en `status='active'` SANS
MARQUEUR (le commentaire de `SHADOW_SPORTS` le disait : « rien ne les
distingue au niveau de la ligne »). Or `/`, `/api/signals` (donc `/system`) et
`/audit` listent `status='active'` : ils montraient les fantômes comme des
paris à poser. L'opérateur pariait dessus depuis le dashboard, et perdait sur
la tranche que le système avait mesurée perdante. Mesuré le 2026-09-03 sur
septembre : 17 des 27 actifs, 19 des 22 réglés étaient des fantômes (8–11).

Trois anomalies corrigées ensemble :

1. **Le fantôme devient une colonne** — `signals.is_shadow` + `shadow_reason`,
   `ai_learning_ledger.is_shadow`, mêmes colonnes sur les deux archives
   (`sql/migrate_v10_12_signals_shadow.sql`, APPLIQUÉE : 189 signaux et 276
   lignes de ledger marqués, aucune supprimée). La partition tourne AVANT la
   persistance et marque chaque signal ; le dashboard, l'API et `/audit`
   filtrent `is_shadow = false` ; `log_to_ledger` recopie le drapeau, et
   `/performance` le préfère à la zone horaire (`perf_view.is_phantom`).
2. **La règle T-2h vaut par SIGNAL, plus seulement par mode de run** — la
   mesure du 2026-08-04 (39 % de réussite) portait sur la TRANCHE horaire, mais
   le fantôme n'était appliqué qu'au mode `golden` : un scan standard tiré à
   13:54 pour un match de 15:00 émettait un signal T-66 min recommandé, envoyé,
   affiché. `_shadow_reason` : `shadow_sport` → `golden_hour` → `t_minus_2h`
   (la raison `golden_hour` est partie avec le mode le 2026-09-03 — deux
   raisons, toutes deux par signal)
   (borne IMPORTÉE de `learning_layer._PLAYABLE_MIN_MINUTES`, règle n°6).
3. **Le rafraîchissement écrasait la mesure** — un re-scan du même
   (match, marché) réécrit `scanned_at` ; 309 lignes l'avaient postérieur à
   `created_at`. Conséquences : (a) le drapeau est FIGÉ à la première insertion
   (`_FIGES_AU_RAFRAICHISSEMENT`) — une recommandation envoyée à T-6h revue à
   T-1h par le tick golden ne devient pas fantôme, sinon le dashboard cacherait
   un pari déjà posé ; (b) `time_to_match_minutes` se mesure depuis
   `created_at`, plus depuis `scanned_at` — les lignes anciennes du ledger
   portent encore l'ancienne mesure (le backfill le dit), toute analyse par zone
   sur elles surestime les fantômes.

Le heartbeat `meta.last_scan.signals` compte désormais les RECOMMANDÉS :
« 12 signaux » pour 0 visible faisait chercher une panne d'affichage.
Gardien : `tests/test_signaux_fantomes.py` (partition avant `_save`, drapeau
figé, ledger, filtres, migration sans DELETE).

### Une version, un seul endroit

`DASHBOARD_VERSION` (`api/index.py`), injectée
dans les 6 templates et rendue par `/api/health`. Ne jamais réécrire un
numéro de version dans un pied de page.


## La règle transverse

### Listes qui divergent — la panne la plus fréquente de ce dépôt

le 2026-08-22, toutes silencieuses). Un fournisseur IA sans clé est ignoré
SANS ERREUR — propriété désirable, mais elle laisse une capacité morte des
mois. Ne JAMAIS tenir à la main une liste qui existe déjà ailleurs : soit on
la dérive (`ops.py::_AI_SECRETS` ← `REGISTRY`, tables sport injectées dans
les templates), soit un test la compare à sa source. Gardiens :
`tests/test_workflow_secrets.py` (clés IA × workflows × `ops.py` ×
`.env.example`, bornes de durée, version de Python unique) et
`tests/test_dashboard_sports.py` (sport → emoji/libellé/ordre).
AVANT d'ajouter un fournisseur, un sport ou un workflow : lire AUDIT.md §2.

