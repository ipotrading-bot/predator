# Cadences cron et budget de déclenchements — détail

> Référence de la skill `predator-pipeline`. Sections déplacées verbatim
> depuis SKILL.md (découpage du 2026-09-02), rien n'a été résumé.

## L'arbitrage de cadence (2026-08-22)

Quand le pool OddsAPI est mort, CHAQUE scan paie sur les budgets journaliers
des sources gratuites : api-sports 80 req/sport (~8 scans), odds-api.io 400
(~14), titan007 500 (~12, 41 req/scan). À 12 engine + 12 guerrilla + 4 deep,
le budget entier partait avant 08:30 UTC — dernier signal du 2026-08-21 émis
à 08:24, soirée européenne à sec. Cadences réduites à 8+2+2 = 12 scans
finançables. Golden hour porte le Tier 1 OddsAPI depuis le 2026-09-01
(pool de 2 500 crédits, rythme mensuel dans `core/scan_windows.py`) : fenêtre
2 h, le pré-vol gratuit rend 0 ligue peuplée la plupart des ticks, et il
descend au Tier 2 (sources gratuites) dans tous les cas.
Ne pas remonter une cadence sans refaire ce tableau. Titan007 est branché
dans le chemin économique du coupe-circuit depuis le même jour (même classe
de bug que a0767c8 : source saine court-circuitée par ricochet).


## Cron cadence (GitHub Actions, `.github/workflows/`)

| Workflow (mode) | Cadence | Purpose |
|---|---|---|
| `scan.yml` — `golden` | horaire (H+25), 24/j | scan de mouvement de ligne à T-120min, **Tier 1 OddsAPI depuis le 2026-09-01** (`ODDS_API=1` posé par `scripts/ci_scan_mode.py`, dépense bornée par le rythme mensuel de `core/scan_windows.py`), purge à chaque run, lit `meta.scan_request` (bouton Scan → promu en scan **standard** depuis le 2026-09-01). Fantôme **baseball** (`SHADOW_SPORTS`) LEVÉ le 2026-09-01 — seul `SHADOW_GOLDEN_HOUR` reste. **Ses signaux partent en FANTÔME depuis le 2026-08-06** (`SHADOW_GOLDEN_HOUR`) : persistés et réglés, jamais recommandés — 39% de réussite pour 54,5% requis, p=0,007. Porte aussi le step **REPRICE** (section dédiée) — gratuit, non fantôme, avec un pool de secrets qui ne contient aucune clé payante. Ne PAS ajouter de poller dédié pour compenser la latence du bouton Scan — c'est l'erreur du 2026-07-07. |
| `scan.yml` — `standard` | **8x/jour sur les FENÊTRES FAVORABLES** (02/06/09/12/17/19/21/23 UTC) depuis le 2026-08-22 (était 12x/2h uniforme) | scan complet, fenêtre **24h**. Placement = `core/scan_windows.py` ; cadence dimensionnée sur le budget des sources gratuites — voir « L'arbitrage de cadence » |
| `scan.yml` — `deep` | **2x/jour (05:33, 17:33)** depuis le 2026-08-22 (était 4) | **fenêtre 24h elle aussi** — `HOURS_AHEAD: "24"` explicite. Ce qui reste « deep » = `MAX_MATCHES=100` et `_QUOTA_DEEP`, pas l'horizon. |
| `scan.yml` — `guerrilla` | **2x/jour (09:47, 21:47)** depuis le 2026-08-22 (était toutes les 2h) | scan sans OddsAPI (sources gratuites, caches raccourcis, horizon 48h venu du CODE et non d'une variable) ; le coupe-circuit `harvest_empty_at` le neutralise 3h après un Tier 2 vide |
| `scan.yml` — passe closing line | **à la fin de chaque tick** (36/j) | `run_closing_line.py`, `continue-on-error` : une passe ratée n'annule pas le scan déjà persisté |
| `audit.yml` | toutes les 6h | settlement + CLV + couche d'apprentissage. **Ne pas renommer ce fichier** : `api/index.py` le déclenche par son nom. |
| `closing_line.yml` | **3 ticks/h (H+14/34/54)** depuis le 2026-08-26 (était `4-59/10`, 144/j) | capture de la ligne de clôture, cadence alignée sur `CLOSING_LINE_REFRESH_MIN`. **Hors du verrou d'écriture** — voir CLAUDE.md pour la justification exacte, la version courte (« aucune ligne en commun ») étant fausse. |
| `reports.yml` — `rapport` | **toutes les 2h (H+35)** | rapport Telegram. `run_rapport.py:REPORT_WINDOW_H` (2h) doit rester égal à l'intervalle du cron, sinon un même signal repart dans plusieurs rapports. |
| `reports.yml` — `hebdo` | **lundi 07:00 UTC** | classement des sports + `calibration_report.py` + **rapport hebdo de vérité** (`scripts/weekly_report.py` : CLV réel, Brier, ROI net taxe, SUSPECT, verdicts promotion/retrait) |
| `tools.yml` | manuel uniquement | `monte_carlo` et `backfill_ledger` (réparation one-shot de `ai_learning_ledger`) |
| `ci.yml` | sur push/PR | tests + lint, **puis** déploiement Vercel — le gate n'est réel que parce que `vercel.json` désactive le déploiement Git |

Total : **124 déclenchements planifiés/jour** (contre 196 avant le 2026-08-26).

**Le scheduler GitHub sous-livre ces crons** (mesuré 2026-08-27 : closing line
~5 %, trou de 9 h sur scan.yml). Un chien de garde Cloudflare
(`scripts/cloudflare_watchdog_worker.js`, cron `*/10`, déployé par
`scripts/deploy_watchdog_worker.py`) dispatche un rattrapage `workflow_dispatch`
quand un workflow est en retard sur sa cadence — récit et règles dans
INCIDENTS.md (« Le scheduler GitHub ne livre qu'une fraction des crons »),
invariants gardés par `tests/test_watchdog_worker.py`. Avant de diagnostiquer
une cadence : les runs `workflow_dispatch` dans `gh run list` peuvent être des
rattrapages du chien de garde, pas des clics d'opérateur.
Le mode d'un tick de `scan.yml` est déduit du cron qui a tiré
(`scripts/ci_scan_mode.py::CRON_MODES`) : un cron ajouté sans sa ligne fait
échouer le run ET le test.

When a fix touches purge, audit, or learning-layer logic, sanity-check it against
this cadence table — anything that runs more often than `audit.yml` (6h) can race
ahead of settlement if it isn't carefully scoped to `status='active'`.

**2026-07-07 incident**: `on_demand.yml` used to poll `meta.scan_request` on its
own `*/5 * * * *` schedule (288 triggers/day, ~81% of every scheduled trigger in
the repo combined). GitHub Actions silently delays/drops scheduled runs under
that kind of load — le tick golden (alors `golden_hour.yml`), despite being declared `*/30`, was
actually landing 1–4.5h apart, leaving the dashboard's "Dernier scan" hours
stale. Fix: the schedule was removed from `on_demand.yml`, and its
`meta.scan_request` check was folded into a step at the top of the golden
scan (free — it rides its existing cadence instead of its own separate
schedule ; depuis le 2026-08-26 ce step est `scripts/ci_scan_mode.py`, lu à
chacun des 36 ticks de `scan.yml`). `on_demand.yml` itself was deleted
outright on 2026-07-07 — once the golden scan absorbed the check, the file
was pure dead weight (a `workflow_dispatch`-only duplicate of logic that now
lives in the scan workflow) and it additionally never passed
`SUPABASE_SERVICE_KEY` to `run_engine.py`, so any manual trigger of it was
guaranteed to fail every write via RLS regardless of secret correctness. When
`scan_request` is pending, golden_hour runs `run_engine.py` with
`GUERRILLA=1` instead of `GOLDEN_HOUR=1` for that tick and clears the flag
(using `SUPABASE_SERVICE_KEY` for the DELETE — the anon key can't write
`meta` either, see [[project_predator_supabase]]). If dashboard "Scan" button
latency or scan cadence looks off again, check this step first before
re-adding a dedicated poller — a new dedicated schedule is exactly the
mistake that caused the original throttling.
