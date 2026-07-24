-- Migration 003: Add lifecycle_regime column to market_snapshots
-- Stores the token lifecycle state computed by the feature pipeline:
--   ignition | accumulation | post_surge_consolidation | decay

ALTER TABLE market_snapshots
    ADD COLUMN IF NOT EXISTS lifecycle_regime VARCHAR;
