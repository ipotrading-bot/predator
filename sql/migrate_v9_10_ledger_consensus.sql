ALTER TABLE ai_learning_ledger
    ADD COLUMN IF NOT EXISTS sharp_sources jsonb,
    ADD COLUMN IF NOT EXISTS consensus_score integer;
