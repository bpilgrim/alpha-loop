-- Migration 004: Persist initial_stop_price on positions
-- Required for trade replay: stop_price is overwritten as the trailing stop moves,
-- so without this column there's no way to reconstruct the position's starting state.

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS initial_stop_price NUMERIC;
