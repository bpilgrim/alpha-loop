"""ExecutionPort — abstract interface for order execution (paper or live DEX)."""

from abc import ABC, abstractmethod

from ..domain.entities.trade import Order


class ExecutionPort(ABC):

    @abstractmethod
    async def submit_entry(self, order: Order) -> Order:
        """
        Submit a buy order. Returns the order with fill_price, slippage_bps,
        fill_status, and tx_hash (None for paper) populated.
        """

    @abstractmethod
    async def submit_exit(self, order: Order) -> Order:
        """
        Submit a sell order. Returns updated order with fill details.
        On emergency exits the adapter should use maximum slippage tolerance.
        """

    @abstractmethod
    async def get_fill_status(self, order_id: str) -> str:
        """Return fill_status for a previously submitted order: filled | unconfirmed | failed."""
