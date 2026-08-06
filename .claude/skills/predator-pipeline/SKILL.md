---
name: predator-pipeline
description: Reference map of the PREDATOR PAIM data pipeline (odds ingestion → signal engine → Supabase → audit → learning layer → dashboard) and its known cross-file invariants. Use this BEFORE diagnosing "why is X empty/wrong/not updating" anywhere in this repo (run_engine.py, core/*, api/index.py, templates/*), before touching purge/audit/learning-layer logic, and before adding a new sport or Supabase column — it names the exact files that must stay in sync and the manual steps this stack does NOT automate.
---

# PREDATOR pipeline map

This project has no automated migration runner and no test suite — the only way to
catch a break is to trace the pipeline by hand. This skill is that trace, pre-done.

## Data flow (in order)

1. **Odds ingestion** — `core/odds_api.py` (Tier 1, real Pinnacle+1XBet via The Odds
   API) → `core/harvester.py` (Tier 2/3, recherche web + MMA/eSports/alt sports) →
   `core/oracle.py` (repli match par match, max 3 appels/scan). **Gemini a été
   SUPPRIMÉ du repo le 2026-07-21 (commit 0a7332e)** : toute la recherche passe
   par `core/ai_search.py` (Groq + Tavily). Si un diagnostic vous ramène à
   Gemini, c'est cette skill qui était périmée, pas le code.
2. **Signal generation** — `run_engine.py` `run()` calls `_process_h2h` /
   `_process_totals` / `_process_spreads`, which call into `core/math_engine.py`
   (devigging: `calc_dnb`, `devig_prob`, `to_binary`) and `core/paim_engine.py`
   (`compute_alpha`, `calculate_consensus_price`, `strict_team_match`). Output rows
   are quota-balanced by `_portfolio_balance` and written to Supabase `signals`
   (`status='active'`).
3. **Purge** — `_purge_old_signals()` runs at the TOP of every `run_engine.py`
   invocation (Golden Hour: hourly since 2026-07-23, was every 30 min). It must only ever delete rows scoped to
   `status='active'` for anything keyed on `match_time`/lifecycle. Never add an
   unscoped `.lt("match_time", ...)` or `.lt("created_at", ...)` rule without an
   explicit `.eq("status", "active")` — history for `settled`/`closed`/`expired`
   rows is retained here on purpose so `core/audit_engine.py` (which runs on a much
   slower 6h cadence) has time to reach them. A single unscoped purge rule
   previously deleted signals the instant kickoff passed, silently starving
   `ai_learning_ledger` and the `/performance` page for months — check this file
   first if either looks empty again.
4. **Audit** — `run_audit.py` → `core/audit_engine.py` (`run()`, cron: every 6h).
   Pass 1: `core/settlement.py` (`settle_signal`, score réel via `core/ai_search.py` — Groq/Tavily) →
   `status='settled'`. Pass 2 fallback: CLV vs current Pinnacle line via
   `core/oracle.py` → `status='closed'` (real closing line) or `'expired'` (proxy).
   Every successful path inserts one row into `ai_learning_ledger`.
5. **Learning layer** — `core/learning_layer.py` `compute_and_save()`, called at the
   end of `audit_engine.run()`. Reads last 50 `ai_learning_ledger` rows per sport,
   needs ≥10 samples with `outcome not in ('expired', None)` before it will move a
   threshold. Thresholds persist to Supabase `meta` as `threshold_<sport>` and are
   read back by `run_engine.py` (`_load_thresholds`) as the next scan's `min_edge`.
6. **Wiz (v10.0, side branch — does NOT feed back into 1–5)** — `run_wiz.py` →
   `core/wiz_engine.py` + `core/wiz_ai.py` (cron: every 2h). Reads `signals`
   (`status='active'`, kickoff < 24h) read-only, groups them **by `match_id`**,
   makes ONE Mistral call per match (the `web_search` connector does the
   searching inside that call), and writes one row into `wiz_analysis`. Its job is to catch a FALSE edge — a high edge that
   exists because the soft book knows something (starter out, MLB pitcher
   changed, team already qualified), not because it's slow.
   Three things that must stay true, and that a well-meaning refactor would
   break silently:
   - **It writes nothing outside `wiz_analysis`.** Not `signals`, not `meta`.
     The quantitative edge is validated; the qualitative data Wiz collects is
     a losing bet on average the moment it touches the maths. Separate tables
     are the mechanical guarantee, not a convention.
   - **It uses Mistral, never Groq/Tavily** — separate failure domain on
     purpose. Steps 1/4 already depend on Groq and its daily quota dies
     regularly (see `core/ai_search.py`); sharing it would let an optional
     layer starve a real settlement. Brave was dropped 2026-07-23 (its free
     tier demands a credit card); Mistral's built-in `web_search` connector
     replaced it and is itself Brave-powered under the hood.
   - **The run is bounded by TIME, not by a request quota.** Mistral's free
     tier is 2 requests/minute, so one match costs ~31s of pure waiting.
     `WIZ_RUN_BUDGET` (20) and `timeout-minutes: 20` in the workflow must be
     raised together or the job gets killed mid-run.
   - **Tier C (pundit consensus) carries a NEGATIVE weight** in
     `core/constants.py` `WIZ_TIER_WEIGHTS`. Public consensus agreeing with a
     signal is a yellow flag (odds inflated by public flow), never a
     confirmation. This is encoded in the sign of a coefficient rather than in
     the prompt, precisely so a model can't ignore it — `tests/test_wiz_engine.py`
     guards it. Flipping it to a small positive is the single easiest way to
     silently make Wiz harmful.
   `WIZ_ENFORCE` (default `0`) gates the `VETO` verdict's power to block
   anything; nothing reads it today except the `/wiz` banner. It stays off
   until `wiz_confidence` has been validated against real outcomes via
   `core/learning_layer.py`'s Brier score (~30 settled signals).
7. **Dashboard** — `api/index.py` Flask routes render `templates/*.html`. `/` and
   `/ledger`/`/audit` read the `signals` table directly (so they only ever show the
   last ~48h — that's by design, not a bug). `/performance` reads
   `ai_learning_ledger` directly — if it's empty, the bug is almost always upstream
   in step 3 or a not-yet-applied migration (see below), not in `api/index.py`.
   `/wiz` and `/api/wiz` read `wiz_analysis` joined against active `signals` —
   **read-only, no AI call and no web search in the request cycle**. One
   analysis takes 10–60s (Mistral is throttled to 2 RPM); Vercel's serverless
   timeout would kill the request before the first match finished. All the
   work lives in `wiz.yml`/`run_wiz.py`. An empty `/wiz` is almost always
   `sql/migrate_v10_0_wiz.sql` not applied, or `MISTRAL_API_KEY` missing.

## The sport-key invariant

These four places must list the exact same sport keys, or a sport silently gets
scanned but never learned-from (or vice versa):
- `core/odds_api.py` `SPORT_KEYS` (what's actually fetched) — the ground truth.
- `core/constants.py` `KELLY_FRACTION`.
- `core/learning_layer.py` `SPORT_DEFAULTS`.
- `run_engine.py` `SPORT_QUOTA` / `_SPORT_ORDER` (portfolio balancer).

`api/index.py`'s `_SPORT_EMOJI`/`_SPORT_LABEL`/`_DEFAULT_T` dicts (used by
`/ledger`) intentionally list a *superset* of display-only sports (tennis, mma,
darts, cricket, etc.) that are not currently harvested — that's harmless UI cruft,
not a bug, unless one of those keys starts appearing in real `signals` rows.

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
- le secret GitHub `ODDS_API_KEY` doit rester NON VIDE même s'il est périmé —
  `engine.yml`/`golden_hour.yml` ont un préflight `[ -z "$ODDS_API_KEY" ] && exit 1`
  qui ferait échouer le job avant tout scan. Ne pas « faire le ménage » en le
  supprimant.

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

## Manual steps this stack does NOT automate

- **Supabase schema changes** live in `sql/migrate_vX_Y.sql` but nothing runs them
  automatically — they must be pasted into the Supabase SQL Editor by a human with
  DB access. Check `sql/` for the latest unapplied migration before assuming a
  column exists.
- **`backfill_ledger.py`** (workflow: `.github/workflows/backfill.yml`) is
  `workflow_dispatch`-only, idempotent, one-shot. It re-populates
  `ai_learning_ledger` from historical terminal-status `signals` rows. Needed after
  any period where step 3's purge bug (or similar) silently dropped rows.
- Both of the above require credentials/permissions this agent does not have by
  default in a fresh sandbox (no Supabase URL/key, and the sandbox's `GITHUB_TOKEN`
  is an app-installation token without `workflow` scope — `gh workflow run` and
  `gh api .../dispatches` both 403). Don't assume a prior session's access
  persists; re-check with `env | grep -i supabase` and `gh auth status` rather than
  telling the user "done" on faith.

## Cron cadence (GitHub Actions, `.github/workflows/`)

| Workflow | Cadence | Purpose |
|---|---|---|
| `golden_hour.yml` | horaire (H+25) | scan de mouvement de ligne à T-120min, purge à chaque run ; vérifie aussi `meta.scan_request`. **Ses signaux partent en FANTÔME depuis le 2026-08-06** (`SHADOW_GOLDEN_HOUR`) : persistés et réglés, jamais recommandés — mesuré à 39% de réussite pour 54,5% requis, p=0,007. Ne PAS ajouter de poller dédié pour compenser la latence du bouton Scan — c'est l'erreur du 2026-07-07. |
| `engine.yml` | **12x/jour, toutes les 2h (H+03)** | scan complet, fenêtre **24h** (était 72h jusqu'au 2026-08-06) |
| `deep_scan.yml` | 4x/jour (H+33) | **fenêtre 24h elle aussi** — `HOURS_AHEAD: "24"` explicite. Le workflow s'appelait « Deep Scan 48h » alors qu'il faisait déjà 24h : renommé « Deep Scan 24h » le 2026-08-06. Ce qui reste « deep » = `MAX_MATCHES=100` et `_QUOTA_DEEP`, pas l'horizon. |
| `audit.yml` | toutes les 6h | settlement + CLV + couche d'apprentissage |
| `rapport.yml` | **toutes les 2h (H+35)** | rapport Telegram — était 07:05 & 18:05 jusqu'au 2026-08-06. `run_rapport.py:REPORT_WINDOW_H` (2h) doit rester égal à l'intervalle du cron, sinon un même signal repart dans plusieurs rapports. |
| `wiz.yml` | toutes les 2h (H+15) | analyse contextuelle Wiz — écrit `wiz_analysis` uniquement, jamais `signals`. Délibérément HORS du groupe `predator-signals-write` (il ne lit que `signals` ; le mettre en file derrière un audit de 45 min lui ferait manquer la fenêtre de compositions T-3h). Ne pas raccourcir cette cadence — voir l'incident du 2026-07-07. |
| `closing_line.yml` | horaire (H+00) | capture de la ligne de clôture |
| `guerrilla.yml` | manuel | scan sans OddsAPI (1XBet direct + recherche web) quand le quota est épuisé |
| `backfill.yml` | manuel | réparation one-shot de `ai_learning_ledger` |

When a fix touches purge, audit, or learning-layer logic, sanity-check it against
this cadence table — anything that runs more often than `audit.yml` (6h) can race
ahead of settlement if it isn't carefully scoped to `status='active'`.

**2026-07-07 incident**: `on_demand.yml` used to poll `meta.scan_request` on its
own `*/5 * * * *` schedule (288 triggers/day, ~81% of every scheduled trigger in
the repo combined). GitHub Actions silently delays/drops scheduled runs under
that kind of load — `golden_hour.yml`, despite being declared `*/30`, was
actually landing 1–4.5h apart, leaving the dashboard's "Dernier scan" hours
stale. Fix: the schedule was removed from `on_demand.yml`, and its
`meta.scan_request` check was folded into a step at the top of
`golden_hour.yml` (free — it rides golden_hour's existing 30-min cadence
instead of its own separate schedule). `on_demand.yml` itself was deleted
outright on 2026-07-07 — once golden_hour.yml absorbed the check, the file
was pure dead weight (a `workflow_dispatch`-only duplicate of logic that now
lives in golden_hour.yml) and it additionally never passed
`SUPABASE_SERVICE_KEY` to `run_engine.py`, so any manual trigger of it was
guaranteed to fail every write via RLS regardless of secret correctness. When
`scan_request` is pending, golden_hour runs `run_engine.py` with
`GUERRILLA=1` instead of `GOLDEN_HOUR=1` for that tick and clears the flag
(using `SUPABASE_SERVICE_KEY` for the DELETE — the anon key can't write
`meta` either, see [[project_predator_supabase]]). If dashboard "Scan" button
latency or scan cadence looks off again, check this step first before
re-adding a dedicated poller — a new dedicated schedule is exactly the
mistake that caused the original throttling.

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
