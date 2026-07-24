"""Position — open or closed trade position."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class TakeProfitLevel:
    price: Decimal
    portion_pct: Decimal    # % of position to exit at this level (0.0–1.0)
    triggered: bool = False


@dataclass
class Position:
    id: UUID = field(default_factory=uuid4)
    session_id: Optional[UUID] = None
    symbol: str = ""
    pair_address: str = ""
    chain: str = "solana"
    entry_time: Optional[datetime] = None
    entry_price: Optional[Decimal] = None
    position_size_usd: Optional[Decimal] = None
    position_size_tokens: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    take_profit_levels: list[TakeProfitLevel] = field(default_factory=list)
    strategy_name: str = ""
    strategy_version: str = ""
    feature_snapshot_id: Optional[UUID] = None
    status: str = "open"                # open | closed | emergency_closed
    mae: Optional[Decimal] = None       # Maximum Adverse Excursion
    mfe: Optional[Decimal] = None       # Maximum Favorable Excursion
    initial_stop_price: Optional[Decimal] = None  # set once at open; never updated
    trailing_breach_count: int = 0               # consecutive bars below trailing stop; resets on recovery
