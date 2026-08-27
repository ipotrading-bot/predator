# Actions opérateur — ce que le code ne peut pas faire à ta place

Trois gestes touchent des comptes EXTERNES (odds-api.io, Cloudflare, GitHub).
Aucun agent ni workflow ne les fait : ils modifient tes comptes, pas ce dépôt.
Chacun est réversible et documenté ici avec sa commande exacte, ce qu'on
attend en retour, et comment vérifier que ça a marché.

Tous se lancent depuis la racine du dépôt, avec le `.env` en place.

---

## 1. odds500 — poser le proxy (LE PLUS URGENT)

### Le constat

Vérifié le 2026-08-27 : **aucun proxy n'est posé nulle part.** Ni dans
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

## 2. odds-api.io — le second slot bookmaker (gratuit)

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

## 3. Cloudflare — tenter le Smart Placement du relais

À faire **seulement si tu n'as pas de proxy** (le geste 1 le rend inutile).

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

## Ce qu'aucun de ces gestes ne règle

- **Le tennis** n'a plus aucune source sharp depuis l'obsolescence d'OddsAPI.
  81 des 141 refus « Échec prix Sharp » mesurés le 2026-08-27 sont du tennis.
  Aucun réglage ne le rattrape ; il faudrait une source qui le price.
- **7M** ne cote rien : c'est un dictionnaire de noms, et il n'est interrogé
  que lorsqu'odds500 rend des fixtures à résoudre. Il suit odds500, il ne la
  précède pas.
- **Kalshi/Polymarket** ne sont pas un gisement mais un garde-fou : ils
  mesurent et n'émettent jamais. Mesuré le 2026-08-27 : 74 marchés cotés,
  **0 apparié** au slate.
