-- Migration 002: Add surrogate UUID to market_snapshots
-- Enables feature_snapshot_id FK on signal_candidates and positions to resolve correctly.
-- Existing rows get a generated UUID via gen_random_uuid().

ALTER TABLE market_snapshots
    ADD COLUMN IF NOT EXISTS id UUID NOT NULL DEFAULT gen_random_uuid();

-- Make it unique so it can act as a surrogate key
CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_id ON market_snapshots (id);

-- Update signal_candidates and positions FKs to be enforceable if desired later
-- (left as logical/soft for now — no ALTER CONSTRAINT added)
