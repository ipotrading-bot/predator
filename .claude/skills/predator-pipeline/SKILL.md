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
   invocation (Golden Hour: every 30 min). It must only ever delete rows scoped to
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
6. **Dashboard** — `api/index.py` Flask routes render `templates/*.html`. `/` and
   `/ledger`/`/audit` read the `signals` table directly (so they only ever show the
   last ~48h — that's by design, not a bug). `/performance` reads
   `ai_learning_ledger` directly — if it's empty, the bug is almost always upstream
   in step 3 or a not-yet-applied migration (see below), not in `api/index.py`.

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
| `golden_hour.yml` | every 30 min | T-120min line-movement scan, purges on every run |
| `engine.yml` | ~10x/day | full 72h-window scan |
| `deep_scan.yml` | 4x/day | 48h-window, all markets |
| `audit.yml` | every 6h | settlement + CLV + learning layer |
| `rapport.yml` | 07:05 & 18:05 UTC | Telegram performance report |
| `on_demand.yml` | every 5 min (polls `meta.scan_request`) | dashboard "Scan" button |
| `backfill.yml` | manual only | one-shot `ai_learning_ledger` repair |

When a fix touches purge, audit, or learning-layer logic, sanity-check it against
this cadence table — anything that runs more often than `audit.yml` (6h) can race
ahead of settlement if it isn't carefully scoped to `status='active'`.
