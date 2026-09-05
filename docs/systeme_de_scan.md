# Le système de scan, en clair (opérateur)

Depuis le 3 septembre 2026, Predator n'a plus que **deux modes de scan**.
Tout le reste (golden, deep, guerrilla) a été supprimé — voir en bas.

## Ce qui tourne

| Mode | Ce qu'il fait | Coût |
|---|---|---|
| **standard** | Le VRAI scan : va chercher les cotes (OddsAPI payant + sources gratuites), calcule les edges sur 24 h, émet les signaux, les persiste, envoie le combiné Telegram, puis capte la ligne de clôture. | Crédits OddsAPI, bornés par le rythme mensuel (`core/scan_windows.py`) |
| **reprice** | Le tick horaire GRATUIT : reprend les cotes photographiées par le dernier standard (cache de 4 h), les recompare aux prix Matchbook/Betfair du moment, capte la clôture d'exchange, et se tait s'il n'y a rien de neuf. Aucune clé payante dans son environnement, c'est mécanique. | Rien |

## Quand (UTC)

| Quoi | Heures | Par jour |
|---|---|---|
| Scan **standard** | 06:03 · 09:03 · 11:03 · 13:03 · 16:03 · 19:03 · 21:03 · 23:03 | 8 |
| Tick **reprice** | toutes les heures à H+25 | 24 |
| Ligne de clôture (`closing_line.yml`) | H+14 · H+34 · H+54 | 72 |
| Audit : règlement des paris, apprentissage (`audit.yml`) | 00 · 06 · 12 · 18 | 4 |
| Digest Telegram (`reports.yml`) | toutes les 2 h à H+35 | 12 |
| Rapport hebdomadaire | lundi 07:00 | 1/semaine |

Chaque heure de scan standard est placée **2 h 30 à 6 h avant** un bloc de
coups d'envoi (06 → Asie/Australie ; 09 et 11 → Big 5 du week-end ; 13 et
16 → soirée européenne 16:45–19:30 ; 19 → Amérique du Sud et MLB ; 21 et 23
→ NFL/NBA/NHL et fin de soirée US). Un scan à moins de 2 h d'un coup d'envoi
ne produit que des fantômes : c'est ce que faisaient 02:03, 17:03, 19:03 et
21:03 sur leurs ligues avant le recalage du 3 septembre. Le pré-vol OddsAPI
(gratuit) saute en plus toute ligue dont plus aucun match n'est à 2 h 30 —
un crédit n'est jamais dépensé pour un fantôme, quelle que soit l'heure.

Trois ticks reprice par jour sont muets par construction, à **03:25**,
**04:25** et **05:25** : le cache du dernier standard (23:03) a plus de 4 h.
C'est normal, ils rafraîchissent le heartbeat et s'arrêtent.

Les heures ci-dessus sont les heures PLANIFIÉES. GitHub ne les livre pas
toutes (mesuré : parfois la moitié). Un chien de garde Cloudflare vérifie
toutes les 10 minutes et rattrape :

- un tick **reprice** si plus rien n'a tourné depuis 75 min ;
- un scan **standard** si son créneau est passé depuis plus de 5 min sans
  qu'un standard ait démarré — donc à H+7 au plus tard (le tick du Worker
  suivant l'heure due). Depuis le 5 septembre 2026 c'est le Worker qui sert
  le créneau en premier ; le cron GitHub, livré de +65 à +115 min ce jour-là,
  n'est plus que la roue de secours. Un scan à T-2h30 parti deux heures en
  retard n'émettait plus que des fantômes (< T-2h) : 137 lignes sur 198
  depuis le 27 août.

Jusqu'au 4 septembre 2026 un standard manqué était perdu, et pire : il était
INVISIBLE. Le chien de garde regardait l'âge du dernier run de `scan.yml`
tous modes confondus, et les ticks reprice horaires le maintenaient toujours
« frais ». Mesuré ce jour-là : sur 4 créneaux dus, 19:03 livré avec 40 min de
retard, 21:03 jamais livré, 23:03 livré avec 1 h 47 de retard, 06:03 jamais
livré — pendant que le chien de garde dispatchait une dizaine de reprice tous
déclarés sains. Le mode figure désormais dans le NOM du run (`Scan standard` /
`Scan reprice`), visible dans `gh run list` comme dans l'API.

Depuis le 5 septembre 2026, **un créneau ne vaut qu'un seul scan payant**.
Le rattrapage arrivait alors à H+28 ; le cron GitHub, lui, peut arriver deux heures
plus tard — et il repayait les mêmes ligues. Mesuré ce jour-là : le créneau
09:03, servi à 09:30 (16 crédits), a été repayé à 10:08 (27 crédits, 23 % de
l'allocation du jour) ; le plafond du jour est tombé à 13:30 et les QUATRE
créneaux suivants — 13:43, 16:30, 17:58, 19:30 — sont repartis sans acheter
une seule cote, ceux qui portent le Big 5 du soir et la NFL/NBA.

Le second run d'un même créneau se **dégrade en `reprice`** : il ne paie rien,
mais re-tarife quand même le slate en cache et capte la clôture. C'est écrit
dans le résumé du run (« DOUBLON dégradé »). Le créneau n'est marqué servi
qu'APRÈS un scan réussi : un scan tombé reste à rattraper.

Le bouton « Scanner » du dashboard reste le rattrapage manuel immédiat — il
n'est JAMAIS dégradé. Pour relancer un scan payant sur un créneau déjà servi,
lancer `scan.yml` à la main en cochant **« Payer même si le créneau a déjà été
servi »**.

## Ce qui n'entre PAS (depuis le 3 septembre 2026, ta décision)

- Un match dont le prix sharp n'est qu'une copie d'une source soft, sans
  marché d'exchange derrière : **marché mort**, écarté.
- Un match qu'aucun de nos outils de règlement ne connaît (MLB statsapi,
  ESPN) : **non réglable**, écarté. Boxe et tennis n'ont plus de source de
  scores et ne sortent plus ; le MMA se règle via ESPN.
- Le log de chaque scan le dit en une ligne : `PÉRIMÈTRE | n matchs → vivants → réglables`.

## Ce que tu vois

- **Dashboard, « Dernier scan »** : rafraîchi par les deux modes. Le nombre de
  matchs et de signaux vient du dernier scan complet.
- **Dashboard, liste des paris** : uniquement les signaux RECOMMANDÉS. Un
  signal à moins de 2 h du coup d'envoi est un « fantôme » : mesuré, réglé,
  appris, mais jamais recommandé ni affiché (colonne `is_shadow`).
- **Telegram**, trois messages et pas un de plus :
  - le scan **standard** parle à chaque passage : « N pari(s) recommandé(s) »
    puis CHAQUE pari en simple (plus de combiné depuis le 3 septembre —
    ta décision), ou « Aucun pari recommandé · 13 matchs analysés ·
    1 écarté (< 2 h) » — le compte des écartés est le bilan honnête du run ;
  - le **reprice** ne parle que s'il a un pari NEUF (un pari est annoncé
    une seule fois par le scan, en 24 h) ;
  - le **digest** toutes les 2 h liste les paris recommandés encore jouables :
    🆕 ceux nés depuis le digest précédent, ⏳ ceux déjà annoncés. Rien à
    lister et moteur vivant : il se tait. Moteur muet depuis plus de 2 h :
    il alerte.
- **Bouton « Scanner »** : ramassé au tick suivant (≤ 1 h). Si c'est un tick
  reprice, il devient un scan standard complet. Il n'y a pas de poller dédié,
  et il ne faut pas en ajouter (incident du 2026-07-07 : 288 déclenchements
  par jour étouffaient tous les crons).

## Vérifier que ça tourne

```bash
gh run list --workflow scan.yml -L 12          # un run :03 « standard », un run :25 « reprice »
python scripts/ops.py status                   # dernier scan, pool OddsAPI
GITHUB_EVENT_NAME=schedule SCHEDULE="25 * * * *" python scripts/ci_scan_mode.py --dry-run
```

## Ce qui a été supprimé le 2026-09-03, et pourquoi

- **golden** (T-2h, 24 runs/jour) : 100 % fantôme par construction depuis le
  2026-08-04 (39 % de réussite mesurés sur cette tranche, pour 54,5 % requis),
  et il dépensait des crédits OddsAPI depuis le 2026-09-01. C'est lui qui a
  créé les fantômes affichés par erreur sur le dashboard.
- **deep** (2 runs/jour) : un standard avec 100 matchs au lieu de 50, jamais
  mesuré, tué deux fois par timeout.
- **guerrilla** (2 runs/jour) : horizon 48 h, alors que les paris à plus de
  24 h perdent (−62,8 % mesurés) et sortent de la zone jouable.

Le détail, les chiffres et les tests gardiens : INCIDENTS.md, « Cinq modes de
scan, deux qui servent ».
