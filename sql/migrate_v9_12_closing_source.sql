-- v9.12 — which feed produced the stored closing price.
--
-- Two paths now write closing_pinnacle_price and they are not equivalent:
--
--   'oddsapi' — core/closing_line.py, off the scan payload run_engine.py
--               already downloaded. The real Pinnacle/Circa/CRIS consensus
--               for the exact side AND the exact line, every market
--               (h2h, totals, spreads). Costs nothing extra.
--   'oracle'  — core/audit_engine.py's web-search fallback. One number, the
--               ML/DNB favourite only, for events OddsAPI never scanned
--               (MMA/eSports/alt sports) or stopped scanning before kickoff.
--
-- Without this column the two are indistinguishable, so the oracle would
-- happily overwrite an exact price with an estimate on the next refresh, and
-- no consumer could weight them differently. core/audit_engine.py's
-- _needs_refresh() reads it to hold off on signals the free path measured.
--
-- Both write sites pass these columns as `optional_cols`, so the code keeps
-- working (minus the provenance) until this migration is applied.

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS closing_source text;

ALTER TABLE ai_learning_ledger
    ADD COLUMN IF NOT EXISTS closing_source text;
