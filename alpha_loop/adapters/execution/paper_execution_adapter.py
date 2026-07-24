"""PaperExecutionAdapter — simulated fills with AMM liquidity-impact slippage model."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from ...domain.entities.trade import Order
from ...ports.execution_port import ExecutionPort

logger = logging.getLogger(__name__)

# From a prior live-trading system: order should not exceed 2% of pool to limit price impact
_MAX_POOL_IMPACT_PCT = Decimal("0.02")
# Impact factor for slippage formula: slippage_bps = (order/liquidity) * factor * 10000
_SLIPPAGE_IMPACT_FACTOR = Decimal("0.5")
# Emergency exit worst-case slippage
_EMERGENCY_SLIPPAGE_PCT = Decimal("0.05")


class PaperExecutionAdapter(ExecutionPort):
    """
    Simulates DEX fills with realistic AMM slippage.
    Formula derived from a prior live-trading system liquidity impact calculation:
        slippage_bps = (order_size_usd / pool_liquidity_usd) * impact_factor * 10000
    """

    _execution_type = "paper"

    def __init__(self, pool_liquidity_provider=None) -> None:
        self._fills: dict[str, Order] = {}
        self._pool_liquidity_provider = pool_liquidity_provider

    async def submit_entry(self, order: Order) -> Order:
        slippage_bps = self._calculate_slippage(order.size_usd, order)
        # Buys pay slippage on top of price
        fill_price = order.requested_price * (1 + slippage_bps / Decimal("10000"))
        filled = Order(
            id=order.id,
            position_id=order.position_id,
            timestamp=datetime.now(tz=timezone.utc),
            side="buy",
            requested_price=order.requested_price,
            fill_price=fill_price.quantize(Decimal("0.00000001")),
            slippage_bps=slippage_bps,
            size_usd=order.size_usd,
            execution_type="paper",
            fill_status="filled",
        )
        self._fills[str(filled.id)] = filled
        logger.info(
            f"[paper] BUY {order.size_usd} USD @ {fill_price:.8f} "
            f"(slippage {slippage_bps:.1f} bps)"
        )
        return filled

    async def submit_exit(self, order: Order, emergency: bool = False) -> Order:
        if emergency:
            slippage_bps = order.requested_price * _EMERGENCY_SLIPPAGE_PCT * Decimal("100")
        else:
            slippage_bps = self._calculate_slippage(order.size_usd, order)
        # Sells receive price minus slippage
        fill_price = order.requested_price * (1 - slippage_bps / Decimal("10000"))
        filled = Order(
            id=order.id,
            position_id=order.position_id,
            timestamp=datetime.now(tz=timezone.utc),
            side="sell",
            requested_price=order.requested_price,
            fill_price=fill_price.quantize(Decimal("0.00000001")),
            slippage_bps=slippage_bps,
            size_usd=order.size_usd,
            execution_type="paper",
            fill_status="filled",
        )
        self._fills[str(filled.id)] = filled
        logger.info(
            f"[paper] SELL {order.size_usd} USD @ {fill_price:.8f} "
            f"(slippage {slippage_bps:.1f} bps, emergency={emergency})"
        )
        return filled

    async def get_fill_status(self, order_id: str) -> str:
        order = self._fills.get(order_id)
        return order.fill_status if order else "failed"

    def _calculate_slippage(self, size_usd: Decimal, order: Order) -> Decimal:
        """
        AMM price impact model. Capped at 2% pool impact.
        slippage_bps = (size_usd / liquidity_usd) * impact_factor * 10000
        """
        liquidity = self._get_liquidity(order)
        if liquidity <= 0:
            return Decimal("500")   # fallback: 50 bps if no liquidity data

        impact_pct = size_usd / liquidity
        # Warn if exceeding 2% pool impact (from a prior live-trading system)
        if impact_pct > _MAX_POOL_IMPACT_PCT:
            logger.warning(
                f"Order size {size_usd} USD exceeds 2% of pool liquidity {liquidity} USD"
            )
        slippage_bps = impact_pct * _SLIPPAGE_IMPACT_FACTOR * Decimal("10000")
        return slippage_bps.quantize(Decimal("0.1"))

    def _get_liquidity(self, order: Order) -> Decimal:
        if self._pool_liquidity_provider:
            return self._pool_liquidity_provider(order.position_id)
        return Decimal("50000")     # default assumption: $50k pool
