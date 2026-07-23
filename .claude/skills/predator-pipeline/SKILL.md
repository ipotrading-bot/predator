---
name: predator-pipeline
description: Reference map of the PREDATOR PAIM data pipeline (odds ingestion → signal engine → Supabase → audit → learning layer → dashboard) and its known cross-file invariants. Use this BEFORE diagnosing "why is X empty/wrong/not updating" anywhere in this repo (run_engine.py, core/*, api/index.py, templates/*), before touching purge/audit/learning-layer logic, and before adding a new sport or Supabase column — it names the exact files that must stay in sync and the manual steps this stack does NOT automate.
---

# PREDATOR pipeline map

This project has no automated migration runner and no test suite — the only way to
catch a break is to trace the pipeline by hand. This skill is that trace, pre-done.

## Data flow (in order)

1. **Odds ingestion** — `core/odds_api.py` (Tier 1, real Pinnacle+1XBet via The Odds
   API) → `core/harvester.py` (Tier 2/3, Gemini Search fallback + MMA/eSports/alt
   sports) → `core/oracle.py` (single-match Gemini fallback, max 3 calls/scan).
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
   Pass 1: `core/settlement.py` (`settle_signal`, real score via Gemini) →
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

Two DIFFERENT `ODDS_API_KEY` values are in play, and they report wildly
different numbers — check which one you are looking at before concluding
anything:
- The key on **Vercel** backs `/api/odds-quota` (and therefore the `/system`
  page). On 2026-07-23 it read 500 remaining / 0 used — a fresh free-tier key
  that nothing consumes.
- The key in **GitHub Actions secrets** is the one the engine actually burns.
  Same day it was at **47 remaining**, visible only in the scan logs
  (`OddsAPI quota guard — 47 remaining, stopping scan early`).

So the dashboard can show a reassuring 500 while the engine is starved. The
scan logs are the only trustworthy source.

Consequence of the guard (`quota_remaining < 50` in `core/odds_api.py`):
below 50, it trips after the FIRST sport key of every scan, so the engine
silently falls back to harvester/cache/Betfair for everything. The counter
then looks frozen (47 across five consecutive runs) because that single
request isn't billed. A frozen quota number is the signature of this state,
not of a healthy one.

Order of magnitude: the free tier is 500 req/MONTH. With 19 keys in
`SPORT_KEYS`, one full scan per DAY already costs 570/month. No cron cadence
fixes that — only shrinking `SPORT_KEYS` or paying for a higher tier does.

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
| `golden_hour.yml` | **hourly (H+25)** | T-120min line-movement scan, purges on every run; also checks `meta.scan_request` (see below). Was `*/30` until 2026-07-23 — halved to cut OddsAPI consumption (912 → 456 req/day). Side effect: the dashboard "Scan" button's latency doubled to ≤60 min. Do NOT add a dedicated poller to compensate — that is the 2026-07-07 mistake. |
| `engine.yml` | ~10x/day | full 72h-window scan |
| `deep_scan.yml` | 4x/day | 48h-window, all markets |
| `audit.yml` | every 6h | settlement + CLV + learning layer |
| `rapport.yml` | 07:05 & 18:05 UTC | Telegram performance report |
| `wiz.yml` | every 2h (H+15) | Wiz contextual analysis — writes `wiz_analysis` only, never `signals`. Deliberately NOT in the `predator-signals-write` concurrency group (it only reads `signals`; queueing it behind a 45-min audit would make it miss the T-3h lineup window). Do not shorten this cadence — see the 2026-07-07 incident below. |
| `guerrilla.yml` | manual only | scan without OddsAPI (1XBet direct + Gemini) when OddsAPI quota is exhausted |
| `backfill.yml` | manual only | one-shot `ai_learning_ledger` repair |

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
