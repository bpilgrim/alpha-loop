"""MarketBar — normalized OHLCV + liquidity snapshot for a single candle."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class MarketBar:
    symbol: str
    pair_address: str
    chain: str                          # solana | bsc | eth | base
    timestamp: datetime
    timeframe: str                      # 1m | 5m | 15m | 1h
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    liquidity_usd: Decimal
    token_status: str = "active"        # active | liquidity_low | pair_dead | rugpull_flagged
    launch_time: Optional[datetime] = None

    # Feature columns — None until feature pipeline runs
    feature_version: Optional[int] = None
    momentum_5: Optional[Decimal] = None
    relative_volume_10: Optional[Decimal] = None
    relative_volume_slope_3: Optional[Decimal] = None
    vwap_distance: Optional[Decimal] = None
    atr_14: Optional[Decimal] = None
    volatility_regime: Optional[str] = None   # low | medium | high
    pullback_depth: Optional[Decimal] = None
    time_since_launch_minutes: Optional[Decimal] = None
    spread_estimate_bps: Optional[Decimal] = None
    min_recent_trades: Optional[int] = None   # trades in last hour
    lifecycle_regime: Optional[str] = None    # ignition | accumulation | post_surge_consolidation | decay
