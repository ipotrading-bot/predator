-- v9.11 — when the closing price was actually taken.
--
-- Without this, closing_pinnacle_price is uninterpretable: a price fetched
-- 10 minutes before kickoff and one fetched 3 hours before are stored
-- identically, so any CLV derived from it carries an unknown lead time.
-- core/audit_engine.py refreshes the price as kickoff approaches and stamps
-- this column each time, so the surviving value says how close to the real
-- close the measurement got.

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS closing_captured_at timestamptz;

ALTER TABLE ai_learning_ledger
    ADD COLUMN IF NOT EXISTS closing_captured_at timestamptz;
