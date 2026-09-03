# Cadences cron et budget de déclenchements — détail

> Référence de la skill `predator-pipeline`. Sections déplacées verbatim
> depuis SKILL.md (découpage du 2026-09-02), rien n'a été résumé.

## L'arbitrage de cadence (2026-08-22)

Quand le pool OddsAPI est mort, CHAQUE scan paie sur les budgets journaliers
des sources gratuites : api-sports 80 req/sport (~8 scans), odds-api.io 400
(~14), titan007 500 (~12, 41 req/scan). À 12 engine + 12 guerrilla + 4 deep,
le budget entier partait avant 08:30 UTC — dernier signal du 2026-08-21 émis
à 08:24, soirée européenne à sec. Cadences réduites à 8+2+2 = 12 scans
finançables, puis à **8 scans complets/jour** le 2026-09-03 (deux modes :
deep et guerrilla supprimés, le tick horaire golden remplacé par un reprice
gratuit qui ne touche aucune source à budget).
Ne pas remonter une cadence sans refaire ce tableau. Titan007 est branché
dans le chemin économique du coupe-circuit depuis le même jour (même classe
de bug que a0767c8 : source saine court-circuitée par ricochet).


## Cron cadence (GitHub Actions, `.github/workflows/`)

| Workflow (mode) | Cadence | Purpose |
|---|---|---|
| `scan.yml` — `standard` | **8x/jour sur les FENÊTRES FAVORABLES** (02/06/09/12/17/19/21/23 UTC, H+03) depuis le 2026-08-22 | scan complet, fenêtre **24h**, Tier 1 OddsAPI (`ODDS_API=1` posé par `scripts/ci_scan_mode.py`, dépense bornée par le rythme mensuel de `core/scan_windows.py`) + Tier 2 gratuit, purge, photographie du slate soft pour reprice. Placement = `core/scan_windows.py` ; cadence dimensionnée sur le budget des sources gratuites — voir « L'arbitrage de cadence ». Un signal à < 2 h du coup d'envoi part en FANTÔME par signal (`is_shadow`). |
| `scan.yml` — `reprice` | horaire (H+25), 24/j | tick GRATUIT (pool sans clé payante) : slate soft en cache (TTL 4 h) vs Matchbook/Betfair, closing line d'exchange, heartbeat, purge ; se tait s'il n'y a rien de neuf ; deux ticks muets/jour attendus (06:25, 16:25 : cache périmé). Lit `meta.scan_request` (bouton Scan → promu en scan **standard**). Ne PAS ajouter de poller dédié — erreur du 2026-07-07. **golden, deep et guerrilla supprimés le 2026-09-03** (décision opérateur) — INCIDENTS.md « Cinq modes de scan, deux qui servent ». |
| `scan.yml` — passe closing line | **à la fin de chaque scan standard** (8/j) | `run_closing_line.py`, `continue-on-error` : une passe ratée n'annule pas le scan déjà persisté |
| `audit.yml` | toutes les 6h | settlement + CLV + couche d'apprentissage. **Ne pas renommer ce fichier** : `api/index.py` le déclenche par son nom. |
| `closing_line.yml` | **3 ticks/h (H+14/34/54)** depuis le 2026-08-26 (était `4-59/10`, 144/j) | capture de la ligne de clôture, cadence alignée sur `CLOSING_LINE_REFRESH_MIN`. **Hors du verrou d'écriture** — voir CLAUDE.md pour la justification exacte, la version courte (« aucune ligne en commun ») étant fausse. |
| `reports.yml` — `rapport` | **toutes les 2h (H+35)** | rapport Telegram. `run_rapport.py:REPORT_WINDOW_H` (2h) doit rester égal à l'intervalle du cron, sinon un même signal repart dans plusieurs rapports. |
| `reports.yml` — `hebdo` | **lundi 07:00 UTC** | classement des sports + `calibration_report.py` + **rapport hebdo de vérité** (`scripts/weekly_report.py` : CLV réel, Brier, ROI net taxe, SUSPECT, verdicts promotion/retrait) |
| `tools.yml` | manuel uniquement | `monte_carlo` et `backfill_ledger` (réparation one-shot de `ai_learning_ledger`) |
| `ci.yml` | sur push/PR | tests + lint, **puis** déploiement Vercel — le gate n'est réel que parce que `vercel.json` désactive le déploiement Git |

Total : **120 déclenchements planifiés/jour** (32 scan + 72 closing + 4 audit + 12 rapport ; 124 avant le 2026-09-03, 196 avant le 2026-08-26).

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
chacun des ticks de `scan.yml` — 32 depuis le 2026-09-03). `on_demand.yml` itself was deleted
outright on 2026-07-07 — once the golden scan absorbed the check, the file
was pure dead weight (a `workflow_dispatch`-only duplicate of logic that now
lives in the scan workflow) and it additionally never passed
`SUPABASE_SERVICE_KEY` to `run_engine.py`, so any manual trigger of it was
guaranteed to fail every write via RLS regardless of secret correctness. When
`scan_request` is pending, le tick (reprice depuis le 2026-09-03) est promu
en scan `standard` complet for that tick and clears the flag
(using `SUPABASE_SERVICE_KEY` for the DELETE — the anon key can't write
`meta` either, see [[project_predator_supabase]]). If dashboard "Scan" button
latency or scan cadence looks off again, check this step first before
re-adding a dedicated poller — a new dedicated schedule is exactly the
mistake that caused the original throttling.
