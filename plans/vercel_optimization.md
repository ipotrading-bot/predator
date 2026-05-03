# Refactoring Plan: Vercel Hobby Optimization

## 1. Objectives
- Reduce execution frequency to 1/24h.
- Eliminate long-running synchronous HTTP requests.
- Optimize Supabase/API usage via caching.

## 2. Proposed Architectural Changes
- **Pipeline Splitting:**
    - `Task 1 (Data Fetching):` Fetch all odds and store in `temp_odds`. (Fast, low CPU)
    - `Task 2 (Validation/Processing):` Read `temp_odds`, run engine/validator, store signals in `signals`.
- **State-based API:**
    - APIs (`/api/scan`) will no longer run full scan. They will only serve data from the `signals` table.
- **Workflow:**
    - GitHub Actions will run `Task 1`, then trigger `Task 2`.

## 3. Implementation Steps
1. Update `predator_paim.yml` cron to 1/day.
2. Refactor `signals/scanner.py` to allow splitting Fetching/Processing.
3. Update `api/scan.py` to act as a trigger/read-only interface.
4. Update `config.py` if needed.
