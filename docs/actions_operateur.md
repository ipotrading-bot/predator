# Actions opérateur — ce que le code ne peut pas faire à ta place

> **État au 2026-08-27 23:00 UTC — les trois sont FAITS.**
> 1. ✅ Proxy Webshare (sortie Londres) posé → odds500 rend 15 matchs sharp.
> 2. ✅ Second book posé → `Slots posés (2/2) : Bet365, 1xbet`.
> 3. ⚠️ Smart Placement activé mais INSUFFISANT (colo IAD → SEA, toujours US).
>    C'est le proxy qui a levé le blocage, pas lui.
>
> Ce document reste la marche à suivre pour REFAIRE ces gestes (rotation de
> proxy, changement de book). Le suivi de ce qui reste ouvert est dans
> INCIDENTS.md.

Trois gestes touchent des comptes EXTERNES (odds-api.io, Cloudflare, GitHub).
Aucun agent ni workflow ne les fait : ils modifient tes comptes, pas ce dépôt.
Chacun est réversible et documenté ici avec sa commande exacte, ce qu'on
attend en retour, et comment vérifier que ça a marché.

Tous se lancent depuis la racine du dépôt, avec le `.env` en place.

---

## 1. odds500 — poser le proxy ✅ FAIT le 2026-08-27

### Le constat d'origine

Vérifié le 2026-08-27 : **aucun proxy n'était posé nulle part.** Ni dans
`app_secrets`, ni dans les secrets GitHub Actions, ni dans `.env`. Le bloc
`env:` des workflows porte bien `FREE_SOURCES_PROXY` et `ODDS500_PROXY` —
**vides**. La plomberie est complète depuis le 2026-08-26 ; il ne manque que
la valeur.

Un proxy souscrit chez un fournisseur n'entre pas tout seul dans le pipeline.

### Ce qui a été corrigé côté code

Avant, même posé, il aurait été **ignoré** : la règle était « le relais gagne
si les deux sont posés ». Le relais est prouvé inopérant depuis les runners
GitHub (le Worker sort au colo de l'appelant, IAD, et 500.com refuse cette
IP). La précédence est désormais inversée — un proxy posé l'emporte, et
`core/net.py` le dit dans les logs.

### La commande

Le proxy doit avoir une **sortie hors des États-Unis** — de préférence en
Europe. C'est tout l'objet de l'opération : les IP US sont refusées par
500.com.

```bash
# Format attendu : http://utilisateur:motdepasse@hote:port
gh secret set FREE_SOURCES_PROXY --body 'http://user:pass@hote-eu:8080'
```

`FREE_SOURCES_PROXY` couvre **toutes** les sources gratuites. Pour n'en cibler
qu'une :

```bash
gh secret set ODDS500_PROXY --body 'http://user:pass@hote-eu:8080'
gh secret set SEVENM_PROXY  --body 'http://user:pass@hote-eu:8080'
```

### Vérifier — la seule preuve qui compte

Un secret posé ne prouve rien. Ce qui tranche est un run **depuis un runner
GitHub**, parce que c'est là que l'IP est refusée. Un test depuis ce
Codespace ne prouve rien : ici, ça marche déjà sans proxy.

```bash
gh workflow run scan.yml -f mode=standard
gh run list --workflow=scan.yml --limit 1        # récupérer l'id
gh run view <id> --log | grep -i odds500
```

| ce que tu lis | ce que ça veut dire |
|---|---|
| `odds500: N matchs dans les 24h` avec N > 0 | ✅ débloqué |
| `403 de l'AMONT via le relais (colo …)` | le proxy n'est pas pris — vérifier le nom du secret |
| `403` sans mention de relais | le proxy est emprunté mais 500.com refuse AUSSI son IP → il faut une autre sortie |
| `Connection refused` / timeout | le proxy lui-même ne répond pas |

⚠️ Même débloquée, odds500 **n'émettra aucun signal tout de suite** : elle
démarre en mode ombre et rend `[]` tant qu'elle n'a pas 100 matchs appariés à
≤ 2 points de divergence. C'est voulu, ce n'est pas une panne.

---

## 2. odds-api.io — le second slot bookmaker ✅ FAIT le 2026-08-27

### Le constat

Le plan autorise **deux** bookmakers ; un seul est posé.

```
Slots posés (1/2) : 1xbet
odds-api.io[soccer]: 8 matchs (0 avec prix sharp) / 60 à venir
```

Huit matchs cotés sur soixante. Le moteur ne peut pas calculer d'edge sur un
match dont il n'a qu'un côté : c'est le goulot du côté soft.

### La commande

```bash
python scripts/odds_api_io_books.py set Bet365 --oui
```

Mesuré le 2026-08-27 sur les 10 prochains matchs : **Bet365 1/10**, et zéro
pour Betano, 888Sport, Marathonbet, Betsson, Betcris, Bodog, Sportingbet BR.
Le slate d'odds-api.io est fait de rencontres que presque aucun book
récréatif ne price — Bet365 est le meilleur candidat mesuré, et le seul à
lignes vraiment indépendantes de 1xbet.

⛔ Ne pas prendre MelBet, 1xBit ou Betwinner : même famille que 1xbet, mêmes
lignes, un slot payé pour recopier le premier. Le script les refuse.

⛔ Aucun book sharp n'est atteignable : `Betfair Exchange` rend
`403 — sharp or exchange book […] only available on our paid plans`.
Reconfirmé en direct le 2026-08-27.

### Refaire la mesure sur un échantillon plus large

Le relevé ci-dessus porte sur 10 matchs (limite de `/odds/multi`) pris à une
heure creuse. Pour trancher sur une base plus solide, en début de journée
quand le budget est intact :

```bash
python scripts/odds_api_io_books.py list                        # les 276 books du plan
python scripts/odds_api_io_books.py suggest Bet365 Betway 10BET # 1 requête par candidat
```

`--marge` entame sciemment la marge de sûreté du budget journalier si besoin.

### Vérifier

```bash
python scripts/odds_api_io_books.py list       # doit afficher « Slots posés (2/2) »
```

Puis, au prochain scan, la ligne `odds-api.io[soccer]: … | books=1xbet,Bet365`
et un nombre de matchs cotés en hausse. Aucun déploiement n'est nécessaire :
`selected_bookmakers()` relit le compte à chaque run.

Réversible : `python scripts/odds_api_io_books.py clear`.

---

## 2bis. odds-api.io — un pool de comptes ⏳ CÔTÉ CODE PRÊT le 2026-08-28

### Le constat

Le plan gratuit se compte PAR COMPTE : 500 requêtes/jour et 2 bookmakers.
Avec un seul compte, à 13:00 UTC le 2026-08-28 le compteur était à 221/400,
tout dépensé pour le football ; tennis, basketball, MMA, baseball et hockey
sortaient en « rythme de dépense » à chaque tick. Le code accepte désormais
plusieurs comptes (`core/odds_api_io.candidate_keys`) : budget tenu par
compte, rythme de dépense sur le total, compte refusé (401/403/429) écarté
pour le run et requête rejouée sur le suivant.

⚠️ C'est un geste de COMPTE EXTERNE, et il n'est pas neutre : plusieurs
comptes gratuits chez un même fournisseur peuvent contrevenir à ses
conditions. api-sports a SUSPENDU le compte pour dépassement le 2026-08-20
— un compte suspendu l'est pour de bon. Le budget reste à 400 sur 500 par
compte, la marge ne se mange pas.

### La commande

1. Créer le ou les comptes supplémentaires sur odds-api.io, récupérer
   chaque clé.
2. Les poser dans `app_secrets` (lues avant l'environnement, sans
   redéploiement) :

   ```bash
   python scripts/ops.py supabase secrets set ODDS_API_IO_KEYS "<clé2>,<clé3>"
   ```

   `ODDS_API_IO_KEY` reste le compte n° 1.
3. Poser les 2 books de CHAQUE nouveau compte — une réponse est toujours
   servie par UN compte, donc chaque compte doit se suffire :

   ```bash
   python scripts/odds_api_io_books.py suggest --compte 2
   python scripts/odds_api_io_books.py set <book> --compte 2
   ```

### Vérifier

```bash
python scripts/ops.py sources        # odds-api.io : « N compte(s) · #1: books=… · #2: books=… »
```

Puis, au prochain scan, `odds-api.io[soccer]: … | comptes=2/2 | n/800 req`,
et les sports du matin qui sortaient en « rythme de dépense » repartent.
Un compte sans bookmaker se voit à la sonde ET au scan (« compte #N :
aucun bookmaker sélectionné — écarté »).

---

## 3. Cloudflare — Smart Placement ⚠️ ESSAYÉ, INSUFFISANT

Activé le 2026-08-27 à 21:41. Mesuré au run 33119345516 : le colo est passé
de **IAD** (Washington) à **SEA** (Seattle). Le réglage n'est donc pas inerte
— il déplace bien l'exécution — mais Cloudflare choisit par LATENCE et le
plus proche reste américain depuis les runners. 500.com refuse toujours.
Laissé activé : il ne nuit pas, et le proxy le contourne de toute façon.
Piste fermée. Section conservée pour la mémoire du geste.

### Le constat

Un Worker s'exécute au colo le plus proche de l'appelant. Le Smart Placement
inverse la règle : près de l'**origine**. Vérifié en base le 2026-08-27, il
n'a **jamais** été activé (`placement: {}`).

### La commande

```bash
python scripts/relay_smart_placement.py          # lecture seule, affiche l'état
python scripts/relay_smart_placement.py --oui    # active
python scripts/relay_smart_placement.py --annuler
```

Si tu obtiens `HTTP 403` : le jeton Cloudflare est en lecture seule sur les
Workers. Il lui faut la permission **Account | Workers Scripts | Edit**
(https://dash.cloudflare.com/profile/api-tokens, modèle « Edit Cloudflare
Workers »), puis remplacer `CLOUDFLARE_API_TOKEN` dans `.env`.

### Ce que ça ne promet pas

Cloudflare optimise la **latence**, pas la géographie : il choisit le colo
lui-même et rien ne garantit qu'il en retienne un dont 500.com accepte l'IP.
C'est une hypothèse gratuite qu'on ferme avant d'en payer une autre.

### Vérifier

Comme pour le proxy : seul un run depuis un runner GitHub tranche, et le
message d'erreur nomme le colo.

```bash
gh workflow run scan.yml -f mode=standard
gh run view <id> --log | grep -i odds500
```

Si le 403 persiste en nommant un colo américain, la conclusion tient : il
faut une sortie hors des colos US (relais épinglé en Europe sur Fly.io ou
Render, proxy à IP dédiée, ou runner auto-hébergé en Europe).

---

## 4. Groq/Tavily — nettoyer les secrets GitHub ⏳ CÔTÉ CODE FAIT le 2026-09-02

Groq et Tavily sont SUPPRIMÉS du pipeline (décision opérateur du 2026-09-02,
récit dans INCIDENTS.md § « Groq et Tavily SUPPRIMÉS »). Plus aucun workflow
ne transmet ces clés (gardien :
`tests/test_ci_env.py::test_aucun_pool_ne_transmet_groq_ou_tavily`), mais les
SECRETS GitHub, eux, existent encore — inertes, et un secret inerte finit
toujours par être rebranché par erreur.

### Le geste

```bash
for s in GROQ_API_KEY GROQ_API_KEY_2 GROQ_API_KEY_3 GROQ_API_KEY_4 \
         GROQ_API_KEY_5 TAVILY_API_KEY; do
  gh secret delete "$s" 2>/dev/null && echo "supprimé : $s"
done
```

Optionnel : fermer les comptes Groq (5 organisations) et Tavily eux-mêmes —
plus rien ici ne les consomme.

### Optionnel — TheSportsDB Patreon

La source de scores TheSportsDB tourne sur la clé publique gratuite (« 123 »).
Un compte Patreon (thesportsdb.com, quelques $/mois) lève les limites et
débloque `eventsday` complet ; poser alors la clé :

```bash
gh secret set THESPORTSDB_API_KEY   # et/ou app_secrets via ops.py
```

### Vérifier

Le premier `audit.yml` après le déploiement doit régler les signaux en
attente sans une seule ligne « compound-mini » ni « Tavily » dans son log —
chercher à la place `SETTLE api-sports`, `SETTLE mlb_statsapi`,
`SETTLE thesportsdb`. ⚠️ Si le log montre des erreurs réseau sur
statsapi.mlb.com/thesportsdb.com depuis les runners (leçon ESPN : filtrage
par IP possible), router par `FREE_SOURCES_PROXY` — la plomberie de
`core/net.py` existe déjà.

---

## 5. api-sports — nouveau compte ✅ FAIT le 2026-09-02

Le compte suspendu le 2026-08-20 l'était pour de bon (voir §2bis) et sa
suspension affamait le settlement : audit en échec 8 runs sur 12 depuis le
30/08, `settlement_starved_at` frais, apprentissage gelé faute de lignes
réglées. L'opérateur a créé un nouveau compte gratuit
(dashboard.api-football.com) et fourni la clé en session ; posée dans
`app_secrets` :

    python scripts/ops.py supabase secrets set API_SPORTS_KEY <clé>

Vérifié dans la foulée par `ops.py sources` : les 4 sports répondent OK,
plan Free 0/100. `core/secret_store.py` lisant Supabase avant
l'environnement, aucun redéploiement ni secret GitHub n'a été nécessaire.
La preuve finale est l'audit suivant : des lignes `SETTLE api-sports` au
log et plus de `RUN STÉRILE`.

## Le goulot qui suit — à surveiller

odds500 est débloquée, mais **le pont d'alias n'a encore rien produit.**
Relevé le 2026-08-28 00:32 UTC, après plusieurs runs avec le proxy actif :

    team_aliases          12 lignes, inchangé depuis le 2026-08-22
    sitemap 7M            854 identifiants, curseur à 90
    matchs passés mémorisés  41
    par run               30 identifiants balayés, budget 80 req/jour
    résultat              « 14 match(s) écarté(s) faute d'alias fiable »

Les noms d'odds500 sont en chinois et l'appariement se fait par temps + ligue
+ STRUCTURE de cotes, jamais par nom. Tant que le dictionnaire ne se remplit
pas, 14 matchs sharp sur 15 partent à la poubelle à chaque run.

À ce rythme, balayer le sitemap entier demande ~11 jours. Ce qui n'est PAS
encore établi, c'est si l'intersection entre les rencontres à venir de 7M et
le slate d'odds500 est seulement suffisante pour produire des alias — elle
n'a jamais pu être mesurée, odds500 ayant été bloquée jusqu'ici.

**Le signe qui tranche : `team_aliases` doit dépasser 12.** S'il n'a pas
bougé d'ici deux jours, ce n'est pas de la lenteur, c'est que le pont ne
fonctionne pas et il faut le diagnostiquer.

```bash
python scripts/ops.py supabase sql "select count(*) from team_aliases"
```

---

## Ce qu'aucun de ces gestes ne règle

- **Le tennis** n'a de source sharp que par OddsAPI (clés dynamiques
  `tennis_*`). 81 des 141 refus « Échec prix Sharp » mesurés le 2026-08-27
  (Tier 1 éteint) étaient du tennis. Tier 1 rallumé le 2026-09-01 : il
  revient tant que le pool `ODDS_API_KEYS` a du crédit — et seulement là.
- **7M** ne cote rien : c'est un dictionnaire de noms, et il n'est interrogé
  que lorsqu'odds500 rend des fixtures à résoudre. Il suit odds500, il ne la
  précède pas.
- **Kalshi/Polymarket** ne sont pas un gisement mais un garde-fou : ils
  mesurent et n'émettent jamais. Mesuré le 2026-08-27 : 74 marchés cotés,
  **0 apparié** au slate.

---

## Claude Code — le serveur MCP Supabase est ÉPINGLÉ et en lecture seule

`.mcp.json` lance `@supabase/mcp-server-supabase@0.11.0` (version exacte,
plus de `@latest`) avec le flag `--read-only`. Deux décisions, deux raisons :

- **Version épinglée** : même principe que `requirements*.txt` (verrouillé au
  `==`, transitives comprises — décision D2). `@latest` laissait npm décider
  à chaque session de ce qui tourne avec un jeton d'accès Supabase en main ;
  aucun tiers ne décide de ce qui s'exécute ici. Pour monter de version :
  `npm view @supabase/mcp-server-supabase version`, mettre à jour `.mcp.json`
  À LA MAIN, et relire le changelog avant.
- **`--read-only`** : une session Claude n'a aucune raison d'écrire en base
  par le MCP. Les écritures légitimes passent par le pipeline (workflows) ou
  par `scripts/ops.py supabase sql` sous contrôle opérateur. C'est la même
  logique que la règle dure n°9 (archiver, jamais supprimer) : le chemin qui
  ne peut pas détruire n'a pas besoin qu'on lui fasse confiance.

Gardien : `tests/test_claude_config.py` (refuse tout `@latest` dans
`.mcp.json`, vérifie la validité JSON des fichiers `.claude/`).

### Plugins (2026-09-02) — installés en scope user, PAS dans le dépôt

Installés sur ce devcontainer via le CLI (`claude plugin install
…@claude-plugins-official`) : `security-guidance`, `commit-commands`,
`pr-review-toolkit`, `vercel`, `github`, `context7`, `pyright-lsp`. Un
scope « user » est machine-local : à refaire sur un autre poste
(`/plugin` ou le même CLI).

Le plugin `supabase` (supabase-community) a été examiné et **écarté** : sa
config MCP pointe le service HÉBERGÉ `https://mcp.supabase.com/mcp` — pas
de version épinglable (le comportement change côté serveur) ni de
`--read-only`. Le `.mcp.json` du dépôt (0.11.0 épinglé, lecture seule)
reste la référence ; ne pas le remplacer par ce plugin.
