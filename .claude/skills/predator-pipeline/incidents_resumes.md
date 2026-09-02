# Incidents résumés côté pipeline — détail

> Référence de la skill `predator-pipeline`. Sections déplacées verbatim
> depuis SKILL.md (découpage du 2026-09-02), rien n'a été résumé.
> Le récit complet vit dans INCIDENTS.md, à la racine.

## Incident 10→20 août 2026 : dix jours à « 0 matchs, 0 signaux » — et ce qui l'empêche désormais

Toutes les sources sont mortes la même journée : clé OddsAPI à 0 crédit (401
`OUT_OF_USAGE_CREDITS`, jamais tournée), LineFeed 1xbet/Melbet en timeout
depuis les runners GitHub, Tavily au plafond mensuel (HTTP 432), les 3 clés
Groq à 100k TPD — brûlées par les ~40 runs/jour qui retentaient le harvest
web à vide — et API-Football muet (un /odds par fixture ≠ 200 → `continue`
sans log). Quatre mécanismes v10.3 (commit « fix: key pool… ») :

- **Pool de clés OddsAPI** — `core/odds_api.py::candidate_keys()` lit
  `ODDS_API_KEYS` (plusieurs clés) puis `ODDS_API_KEY` (app_secrets puis env),
  sonde chaque clé gratuitement (`/v4/sports`), et sur 401/403/422 en cours de
  scan bascule sur la suivante EN REJOUANT LA MÊME LIGUE. Seul un pool
  entièrement mort rend `[]`. Ajouter : `python scripts/rotate_odds_key.py
  --add <clé>` ; état : `--show`. Tests : `tests/test_odds_api_keypool.py`.
- **Alertes Telegram avec la cause, dédupliquées 6h** — `_alert_oddsapi_pool_if_dead()`
  nomme « N/N clés épuisées — rotation requise » ; l'ancien « Melbet
  inaccessible » partait 40×/jour sans jamais le dire. Horodatages dans `meta`
  (`alert_*`).
- **Coupe-circuit harvest** — `meta.harvest_empty_at` : un Tier 2 vide n'est
  pas retenté avant `HARVEST_EMPTY_TTL_H` (3h). (Il préservait le TPD Groq ;
  depuis le 2026-09-02 il n'épargne plus que du temps et le LineFeed.) Tests :
  `tests/test_engine_circuit_breaker.py`.
- **API-Football utile** — `/odds` PAR DATE paginé (≤ 7 req/cycle au lieu de
  50+) et **Pinnacle extrait de la réponse** (`odds_pinnacle`) → signaux foot
  sans recherche web ; run_engine honore un `odds_pinnacle` déjà présent et
  ne l'envoie pas à `fetch_pinnacle_prices`. Une ligne de log « API-Football: »
  par cycle, succès ou échec. Tests : `tests/test_api_football.py`.


## The OddsAPI quota reality (2026-07-23)

**RÉSOLU depuis le 2026-08-04 — ne plus diagnostiquer ainsi.** Il y avait
autrefois DEUX clés `ODDS_API_KEY` distinctes (une sur Vercel pour
`/api/odds-quota`, une dans les secrets GitHub pour le moteur), et le dashboard
pouvait afficher un rassurant 500 pendant que le moteur était à sec. La table
`app_secrets` (Supabase) est désormais la source unique : `core/secret_store.py`
la lit AVANT `os.environ`, et le 2026-08-06 il a été vérifié en direct que
Vercel la lit bien (rotation de clé → `/api/odds-quota` passé à 500/0 sans
redeploy). Le widget et le moteur voient donc la même clé.

Deux corollaires qui restent vrais :
- une valeur PÉRIMÉE dans `app_secrets` bat un `os.environ` correct ;
- ~~le secret GitHub `ODDS_API_KEY` doit rester NON VIDE~~ **PÉRIMÉ.** Cette
  garde échouait FERMÉ : elle aurait tué tous les scans le jour du retrait du
  secret. Elle a été supprimée avec l'obsolescence d'OddsAPI (2026-08-26), et
  le préflight actuel (`scripts/ci_env.py`) ne l'exige plus — c'est même un
  test explicite
  (`tests/test_ci_env.py::test_preflight_odds_api_key_nest_plus_requise`).

Les logs de scan (`x-requests-used` / `x-requests-remaining`) restent la mesure
la plus fiable de la consommation réelle.

**PÉRIMÉ — ce garde n'existe plus.** Supprimé le 2026-08-01 sur décision
opérateur « ne pas rationner » ; le scan ne s'arrête plus que sur un vrai 422.
Conservé ici parce que la SIGNATURE décrite reste utile à reconnaître dans
d'anciens logs. Ce qui subsiste est le pré-vol GRATUIT `_events_in_window()`
(endpoints `/v4/sports` et `/events`, 0 crédit) qui évite de payer une ligue
vide. Conséquence historique du garde disparu :
below 50, it trips after the FIRST sport key of every scan, so the engine
silently falls back to harvester/cache/Betfair for everything. The counter
then looks frozen (47 across five consecutive runs) because that single
request isn't billed. A frozen quota number is the signature of this state,
not of a healthy one.

Ordre de grandeur (à jour 2026-08-06) : le plan fait 500 req/MOIS. `SPORT_KEYS`
compte **18** clés depuis le retrait de la Coupe du Monde. Le coût se paie par
LIGUE PEUPLÉE et non par match : mesuré via `/events` en fenêtre 24h, un scan
coûte **14 crédits** (5 ligues peuplées ce jour-là), et 0 en fenêtre 2h. À 12
scans engine + 4 deep par jour, cela fait ~224 crédits/jour, soit **une clé
tous les ~2 jours**. La rotation passe par `app_secrets` (Supabase), pas par le
secret GitHub ni par Vercel — voir `core/secret_store.py`.


## La couche d'apprentissage — deux pièges corrigés le 2026-08-06

`core/learning_layer.py` fixe les planchers d'edge (`meta.threshold_<sport>`)
que le prochain scan lira. Deux défauts s'y combinaient et ont fini par étouffer
l'émission (1 signal/jour début août contre 22 le 2 août) :

1. **Le critère était absolu** — monter à <60% de réussite, ne descendre qu'à
   >82%. Un pari à cote 1,85 est rentable dès 54,1% et aucun segment du ledger
   n'a jamais atteint 82% : la montée se déclenchait toujours, la descente
   jamais. Cliquet jusqu'au plafond dur de 6,0%, puis silence. La règle est
   désormais ancrée sur `p_breakeven` (cote moyenne + `TAX_RATE`), et reste
   asymétrique : monter ne demande pas de preuve, descendre exige que la borne
   basse de Wilson passe la rentabilité.
2. **Il apprenait sur des paris qu'on ne joue plus** — `playable_rows()` filtre
   maintenant sur la zone 2-24h avant le coup d'envoi. Hors zone : 113 paris,
   ROI -28,5%, p=0,002, le seul segment significatif du ledger — et il n'est
   plus jouable (>24h hors fenêtre de scan, <2h en fantôme). Le football était
   jugé sur 50,0% quand sa zone jouable fait 65,1%.

**Le piège d'analyse à ne pas refaire.** Pris marginalement, les totals
(-19,2%), les edges ≥10% (-25,3%) et les cotes ≥2,00 (-24,7%) semblent être les
coupables. C'est faux : ces trois ensembles se recoupent (33 paris communs) et
sont concentrés HORS zone jouable. À l'intérieur, ce sont les MEILLEURS
segments — totals +27,2%, edge ≥10% +10,5%, cote >1,95 +17,8% — et une
régression logistique contrôlant la cote ne laisse aucun d'eux significatif.
Les couper ferait tomber le ROI de la zone de +9,4% à +3,4%. **Toujours
conditionner sur la zone jouable avant de conclure quoi que ce soit sur un
sport, un marché ou une bande d'edge.**
