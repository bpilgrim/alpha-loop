"""Trade — closed position lifecycle summary."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class Trade:
    id: UUID = field(default_factory=uuid4)
    position_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    symbol: str = ""
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    entry_price: Optional[Decimal] = None
    exit_price: Optional[Decimal] = None    # avg across partial exits
    pnl_usd: Optional[Decimal] = None
    pnl_pct: Optional[Decimal] = None
    r_multiple: Optional[Decimal] = None
    mae: Optional[Decimal] = None
    mfe: Optional[Decimal] = None
    hold_time_minutes: Optional[int] = None
    exit_reason: str = ""   # stop | take_profit | time_stop | trailing_stop | liquidity_emergency | manual
    strategy_version: str = ""


@dataclass
class TradeEvent:
    id: UUID = field(default_factory=uuid4)
    trade_id: Optional[UUID] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    event_type: str = ""    # entry | partial_exit | stop_moved | liquidity_alert | emergency_exit | closed
    payload: dict = field(default_factory=dict)


@dataclass
class Order:
    id: UUID = field(default_factory=uuid4)
    position_id: Optional[UUID] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    side: str = "buy"                   # buy | sell
    requested_price: Optional[Decimal] = None
    fill_price: Optional[Decimal] = None
    slippage_bps: Optional[Decimal] = None
    size_usd: Optional[Decimal] = None
    execution_type: str = "paper"       # paper | live
    tx_hash: Optional[str] = None
    fill_status: str = "filled"         # filled | unconfirmed | failed
