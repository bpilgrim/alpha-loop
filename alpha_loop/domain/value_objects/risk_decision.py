"""RiskDecision — output of the risk engine for a given signal."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class RiskDecision:
    approved: bool
    rejection_reasons: list[str] = field(default_factory=list)
    position_size_usd: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    take_profit_prices: list[Decimal] = field(default_factory=list)
    estimated_slippage_bps: Optional[Decimal] = None


@dataclass
class ExitDecision:
    should_exit: bool
    exit_reason: str = ""
    exit_portion: Decimal = Decimal("1.0")
    is_partial: bool = False
    emergency: bool = False
    exit_price: Optional[Decimal] = None  # stop exits carry stop_price here; None = use bar.close
