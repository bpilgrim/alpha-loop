"""PostgresTradeStore — asyncpg-backed implementation of TradeStorePort."""

import json
import logging
from decimal import Decimal
from uuid import UUID

import asyncpg

from ...domain.entities.market_bar import MarketBar
from ...domain.entities.position import Position, TakeProfitLevel
from ...domain.entities.signal_candidate import SignalCandidate
from ...domain.entities.trade import Order, Trade, TradeEvent
from ...ports.trade_store_port import TradeStorePort

logger = logging.getLogger(__name__)


class PostgresTradeStore(TradeStorePort):
    """
    All writes use upsert or insert-or-ignore where the schema has a natural key.
    Connections are pooled; call connect() before use and close() on shutdown.
    asyncpg maps Python Decimal → NUMERIC and datetime (aware) → TIMESTAMPTZ natively.
    JSONB columns receive pre-serialised strings via json.dumps().
    """

    def __init__(self, dsn: str) -> None:
        # SQLAlchemy uses "postgresql+asyncpg://"; asyncpg wants "postgresql://"
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        logger.info("PostgresTradeStore connected")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("PostgresTradeStore closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _db(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresTradeStore.connect() was not called")
        return self._pool

    @staticmethod
    def _d(value: Decimal | None) -> str | None:
        """Decimal → string for asyncpg NUMERIC params (avoids float precision loss)."""
        return str(value) if value is not None else None

    # ------------------------------------------------------------------
    # Market snapshots
    # ------------------------------------------------------------------

    async def write_bars(self, bars: list[MarketBar]) -> None:
        """Upsert bars into market_snapshots. Conflict key: (pair_address, timestamp, timeframe)."""
        if not bars:
            return

        sql = """
            INSERT INTO market_snapshots (
                pair_address, timestamp, timeframe, symbol, chain,
                open, high, low, close, volume, liquidity_usd,
                token_status, launch_time,
                feature_version, momentum_5, relative_volume_10,
                relative_volume_slope_3, vwap_distance, atr_14,
                volatility_regime, pullback_depth,
                time_since_launch_minutes, spread_estimate_bps, min_recent_trades,
                lifecycle_regime
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10, $11,
                $12, $13,
                $14, $15, $16,
                $17, $18, $19,
                $20, $21,
                $22, $23, $24,
                $25
            )
            ON CONFLICT (pair_address, timestamp, timeframe) DO UPDATE SET
                liquidity_usd           = EXCLUDED.liquidity_usd,
                token_status            = EXCLUDED.token_status,
                feature_version         = EXCLUDED.feature_version,
                momentum_5              = EXCLUDED.momentum_5,
                relative_volume_10      = EXCLUDED.relative_volume_10,
                relative_volume_slope_3 = EXCLUDED.relative_volume_slope_3,
                vwap_distance           = EXCLUDED.vwap_distance,
                atr_14                  = EXCLUDED.atr_14,
                volatility_regime       = EXCLUDED.volatility_regime,
                pullback_depth          = EXCLUDED.pullback_depth,
                time_since_launch_minutes = EXCLUDED.time_since_launch_minutes,
                spread_estimate_bps     = EXCLUDED.spread_estimate_bps,
                min_recent_trades       = EXCLUDED.min_recent_trades,
                lifecycle_regime        = EXCLUDED.lifecycle_regime
        """

        rows = [
            (
                b.pair_address, b.timestamp, b.timeframe, b.symbol, b.chain,
                self._d(b.open), self._d(b.high), self._d(b.low),
                self._d(b.close), self._d(b.volume), self._d(b.liquidity_usd),
                b.token_status, b.launch_time,
                b.feature_version,
                self._d(b.momentum_5), self._d(b.relative_volume_10),
                self._d(b.relative_volume_slope_3), self._d(b.vwap_distance),
                self._d(b.atr_14),
                b.volatility_regime,
                self._d(b.pullback_depth),
                self._d(b.time_since_launch_minutes),
                self._d(b.spread_estimate_bps),
                b.min_recent_trades,
                b.lifecycle_regime,
            )
            for b in bars
        ]

        async with self._db.acquire() as conn:
            await conn.executemany(sql, rows)

        logger.debug("write_bars: upserted %d rows for %s", len(bars), bars[0].pair_address)

    # ------------------------------------------------------------------
    # Strategy runs
    # ------------------------------------------------------------------

    async def write_strategy_run(self, run: dict) -> UUID:
        sql = """
            INSERT INTO strategy_runs (
                id, strategy_name, strategy_version, config_hash,
                mode, session_start, capital_start
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
        """
        from uuid import uuid4
        run_id = run.get("id") or uuid4()
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                run_id,
                run["strategy_name"],
                run["strategy_version"],
                run["config_hash"],
                run["mode"],
                run["session_start"],
                self._d(run.get("capital_start")),
            )
        return row["id"]

    # ------------------------------------------------------------------
    # Signal candidates
    # ------------------------------------------------------------------

    async def write_signal_candidate(self, signal: SignalCandidate) -> None:
        sql = """
            INSERT INTO signal_candidates (
                id, timestamp, strategy_name, strategy_version,
                symbol, pair_address, signal_direction,
                feature_snapshot_id, passed_filters, rejection_reason,
                confidence, session_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (id) DO NOTHING
        """
        async with self._db.acquire() as conn:
            await conn.execute(
                sql,
                signal.id, signal.timestamp, signal.strategy_name,
                signal.strategy_version, signal.symbol, signal.pair_address,
                signal.signal_direction, signal.feature_snapshot_id,
                signal.passed_filters, signal.rejection_reason,
                self._d(signal.confidence), signal.session_id,
            )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def write_position(self, position: Position) -> None:
        sql = """
            INSERT INTO positions (
                id, session_id, symbol, pair_address, chain,
                entry_time, entry_price, position_size_usd, position_size_tokens,
                stop_price, initial_stop_price, take_profit_levels,
                strategy_name, strategy_version, feature_snapshot_id,
                status, mae, mfe
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT (id) DO NOTHING
        """
        async with self._db.acquire() as conn:
            await conn.execute(
                sql,
                position.id, position.session_id,
                position.symbol, position.pair_address, position.chain,
                position.entry_time,
                self._d(position.entry_price),
                self._d(position.position_size_usd),
                self._d(position.position_size_tokens),
                self._d(position.stop_price),
                self._d(position.initial_stop_price),
                json.dumps(self._tp_levels_to_json(position.take_profit_levels)),
                position.strategy_name, position.strategy_version,
                position.feature_snapshot_id,
                position.status,
                self._d(position.mae), self._d(position.mfe),
            )

    async def update_position(self, position: Position) -> None:
        sql = """
            UPDATE positions SET
                stop_price          = $2,
                take_profit_levels  = $3,
                status              = $4,
                mae                 = $5,
                mfe                 = $6
            WHERE id = $1
        """
        async with self._db.acquire() as conn:
            await conn.execute(
                sql,
                position.id,
                self._d(position.stop_price),
                json.dumps(self._tp_levels_to_json(position.take_profit_levels)),
                position.status,
                self._d(position.mae),
                self._d(position.mfe),
            )

    async def get_open_positions(self, session_id: UUID) -> list[Position]:
        sql = """
            SELECT * FROM positions
            WHERE session_id = $1 AND status = 'open'
            ORDER BY entry_time ASC
        """
        async with self._db.acquire() as conn:
            rows = await conn.fetch(sql, session_id)
        return [self._row_to_position(r) for r in rows]

    # ------------------------------------------------------------------
    # Trades, events, orders
    # ------------------------------------------------------------------

    async def close_position(self, position_id: UUID, trade: Trade) -> None:
        """Atomically write the trade record and mark the position closed."""
        insert_trade_sql = """
            INSERT INTO trades (
                id, position_id, session_id, symbol,
                entry_time, exit_time, entry_price, exit_price,
                pnl_usd, pnl_pct, r_multiple,
                mae, mfe, hold_time_minutes, exit_reason, strategy_version
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (id) DO NOTHING
        """
        update_position_sql = """
            UPDATE positions SET status = 'closed' WHERE id = $1
        """
        async with self._db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    insert_trade_sql,
                    trade.id, trade.position_id, trade.session_id,
                    trade.symbol, trade.entry_time, trade.exit_time,
                    self._d(trade.entry_price), self._d(trade.exit_price),
                    self._d(trade.pnl_usd), self._d(trade.pnl_pct),
                    self._d(trade.r_multiple),
                    self._d(trade.mae), self._d(trade.mfe),
                    trade.hold_time_minutes, trade.exit_reason,
                    trade.strategy_version,
                )
                await conn.execute(update_position_sql, position_id)

    async def write_trade_event(self, event: TradeEvent) -> None:
        sql = """
            INSERT INTO trade_events (id, trade_id, timestamp, event_type, payload)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO NOTHING
        """
        async with self._db.acquire() as conn:
            await conn.execute(
                sql,
                event.id, event.trade_id, event.timestamp,
                event.event_type, json.dumps(event.payload),
            )

    async def write_order(self, order: Order) -> None:
        sql = """
            INSERT INTO orders (
                id, position_id, timestamp, side,
                requested_price, fill_price, slippage_bps,
                size_usd, execution_type, tx_hash, fill_status
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (id) DO NOTHING
        """
        async with self._db.acquire() as conn:
            await conn.execute(
                sql,
                order.id, order.position_id, order.timestamp, order.side,
                self._d(order.requested_price), self._d(order.fill_price),
                self._d(order.slippage_bps), self._d(order.size_usd),
                order.execution_type, order.tx_hash, order.fill_status,
            )

    # ------------------------------------------------------------------
    # Review runner queries
    # ------------------------------------------------------------------

    async def get_session(self, session_id: UUID) -> dict:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM strategy_runs WHERE id = $1", session_id
            )
        if row is None:
            raise KeyError(f"session {session_id} not found")
        return dict(row)

    async def get_session_trades(self, session_id: UUID) -> list[Trade]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trades WHERE session_id = $1 ORDER BY exit_time ASC",
                session_id,
            )
        return [self._row_to_trade(r) for r in rows]

    async def get_session_signals(self, session_id: UUID) -> list[SignalCandidate]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM signal_candidates WHERE session_id = $1 ORDER BY timestamp ASC",
                session_id,
            )
        return [self._row_to_signal(r) for r in rows]

    async def get_feature_snapshot(self, pair_address: str, at_time) -> dict:
        """Return market_snapshots row closest to at_time for this pair.
        The 'id' field is present only after migration 002 has been applied.
        """
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM market_snapshots
                WHERE pair_address = $1
                ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - $2::timestamptz)))
                LIMIT 1
                """,
                pair_address, at_time,
            )
        return dict(row) if row else {}

    async def write_trade_review(self, review: dict) -> UUID:
        from uuid import uuid4
        review_id = review.get("id") or uuid4()
        sql = """
            INSERT INTO trade_reviews (
                id, session_id, review_version, reviewed_at,
                hypothesis, rationale, expected_effect,
                change_type, fields_needed, proposed_config_diff, risk
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            RETURNING id
        """
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                review_id,
                review["session_id"],
                review.get("review_version", 1),
                review.get("reviewed_at"),
                review.get("hypothesis"),
                review.get("rationale"),
                review.get("expected_effect"),
                review.get("change_type"),
                json.dumps(review.get("fields_needed", [])),
                json.dumps(review.get("proposed_config_diff", {})),
                review.get("risk"),
            )
        return row["id"]

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_trade(row: asyncpg.Record) -> Trade:
        return Trade(
            id=row["id"],
            position_id=row["position_id"],
            session_id=row["session_id"],
            symbol=row["symbol"],
            entry_time=row["entry_time"],
            exit_time=row["exit_time"],
            entry_price=Decimal(str(row["entry_price"])) if row["entry_price"] else None,
            exit_price=Decimal(str(row["exit_price"])) if row["exit_price"] else None,
            pnl_usd=Decimal(str(row["pnl_usd"])) if row["pnl_usd"] else None,
            pnl_pct=Decimal(str(row["pnl_pct"])) if row["pnl_pct"] else None,
            r_multiple=Decimal(str(row["r_multiple"])) if row["r_multiple"] else None,
            mae=Decimal(str(row["mae"])) if row["mae"] else None,
            mfe=Decimal(str(row["mfe"])) if row["mfe"] else None,
            hold_time_minutes=row["hold_time_minutes"],
            exit_reason=row["exit_reason"] or "",
            strategy_version=row["strategy_version"] or "",
        )

    @staticmethod
    def _row_to_signal(row: asyncpg.Record) -> SignalCandidate:
        return SignalCandidate(
            id=row["id"],
            timestamp=row["timestamp"],
            strategy_name=row["strategy_name"] or "",
            strategy_version=row["strategy_version"] or "",
            symbol=row["symbol"] or "",
            pair_address=row["pair_address"] or "",
            signal_direction=row["signal_direction"] or "long",
            feature_snapshot_id=row["feature_snapshot_id"],
            passed_filters=row["passed_filters"],
            rejection_reason=row["rejection_reason"],
            confidence=Decimal(str(row["confidence"])) if row["confidence"] else None,
            session_id=row["session_id"],
        )

    @staticmethod
    def _tp_levels_to_json(levels: list[TakeProfitLevel]) -> list[dict]:
        return [
            {"price": str(tp.price), "portion_pct": str(tp.portion_pct), "triggered": tp.triggered}
            for tp in levels
        ]

    @staticmethod
    def _row_to_position(row: asyncpg.Record) -> Position:
        raw_tp = row["take_profit_levels"]
        tp_list = json.loads(raw_tp) if raw_tp else []
        tp_levels = [
            TakeProfitLevel(
                price=Decimal(tp["price"]),
                portion_pct=Decimal(tp["portion_pct"]),
                triggered=tp["triggered"],
            )
            for tp in tp_list
        ]
        return Position(
            id=row["id"],
            session_id=row["session_id"],
            symbol=row["symbol"],
            pair_address=row["pair_address"],
            chain=row["chain"],
            entry_time=row["entry_time"],
            entry_price=Decimal(str(row["entry_price"])) if row["entry_price"] else None,
            position_size_usd=Decimal(str(row["position_size_usd"])) if row["position_size_usd"] else None,
            position_size_tokens=Decimal(str(row["position_size_tokens"])) if row["position_size_tokens"] else None,
            stop_price=Decimal(str(row["stop_price"])) if row["stop_price"] else None,
            initial_stop_price=Decimal(str(row["initial_stop_price"])) if row["initial_stop_price"] else None,
            take_profit_levels=tp_levels,
            strategy_name=row["strategy_name"],
            strategy_version=row["strategy_version"],
            feature_snapshot_id=row["feature_snapshot_id"],
            status=row["status"],
            mae=Decimal(str(row["mae"])) if row["mae"] else None,
            mfe=Decimal(str(row["mfe"])) if row["mfe"] else None,
        )
