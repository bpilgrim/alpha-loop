"""SignalCandidate — every setup detection, taken or rejected."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class SignalCandidate:
    id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    strategy_name: str = ""
    strategy_version: str = ""
    symbol: str = ""
    pair_address: str = ""
    signal_direction: str = "long"      # long | short
    feature_snapshot_id: Optional[UUID] = None
    passed_filters: bool = False
    rejection_reason: Optional[str] = None
    confidence: Optional[Decimal] = None
    session_id: Optional[UUID] = None
