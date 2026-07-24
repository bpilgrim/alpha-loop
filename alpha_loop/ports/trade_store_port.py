"""TradeStorePort — abstract interface for the trade journal."""

from abc import ABC, abstractmethod
from uuid import UUID

from ..domain.entities.market_bar import MarketBar
from ..domain.entities.signal_candidate import SignalCandidate
from ..domain.entities.position import Position
from ..domain.entities.trade import Trade, TradeEvent, Order


class TradeStorePort(ABC):

    @abstractmethod
    async def write_bars(self, bars: list[MarketBar]) -> None:
        """Upsert a batch of market_snapshot rows (bars + features)."""

    @abstractmethod
    async def write_signal_candidate(self, signal: SignalCandidate) -> None:
        """Persist a signal candidate — taken or rejected. Never skip."""

    @abstractmethod
    async def write_position(self, position: Position) -> None:
        """Persist a new open position."""

    @abstractmethod
    async def update_position(self, position: Position) -> None:
        """Update MAE, MFE, stop_price, status on an existing position."""

    @abstractmethod
    async def write_trade_event(self, event: TradeEvent) -> None:
        """Append a lifecycle event to the trade event log."""

    @abstractmethod
    async def write_order(self, order: Order) -> None:
        """Persist an order record."""

    @abstractmethod
    async def close_position(self, position_id: UUID, trade: Trade) -> None:
        """Write the trade summary and mark the position closed atomically."""

    @abstractmethod
    async def get_open_positions(self, session_id: UUID) -> list[Position]:
        """Return all open positions for the given session."""

    @abstractmethod
    async def write_strategy_run(self, run: dict) -> UUID:
        """Persist a strategy_runs record. Returns the session UUID."""

    @abstractmethod
    async def get_session(self, session_id: UUID) -> dict:
        """Return the strategy_runs row for a session."""

    @abstractmethod
    async def get_session_trades(self, session_id: UUID) -> list[Trade]:
        """Return all closed Trade records for a session."""

    @abstractmethod
    async def get_session_signals(self, session_id: UUID) -> list[SignalCandidate]:
        """Return all SignalCandidate records for a session."""

    @abstractmethod
    async def get_feature_snapshot(self, pair_address: str, at_time) -> dict:
        """Return the market_snapshots row closest to at_time for a pair."""

    @abstractmethod
    async def write_trade_review(self, review: dict) -> UUID:
        """Write a trade_reviews record. Returns the review UUID."""
