"""MarketDataPort — abstract interface for DEX market data."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..domain.entities.market_bar import MarketBar


class MarketDataPort(ABC):

    @abstractmethod
    async def get_bars(
        self,
        pair_address: str,
        timeframe: str,
        limit: int,
    ) -> list[MarketBar]:
        """Return up to `limit` bars ending at now, newest last."""

    @abstractmethod
    async def get_latest_bar(self, pair_address: str, timeframe: str) -> Optional[MarketBar]:
        """Return the most recent completed bar, or None if unavailable."""

    @abstractmethod
    async def get_token_status(self, pair_address: str) -> str:
        """Return token_status string: active | liquidity_low | pair_dead | rugpull_flagged."""

    @abstractmethod
    async def get_launch_time(self, pair_address: str) -> Optional[datetime]:
        """Return the pair's creation/launch timestamp, or None if unknown."""

    @abstractmethod
    async def get_security_info(self, pair_address: str) -> dict:
        """
        Return token security metadata from Birdeye security API:
          freezeable: bool
          mutableMetadata: bool
          top10HolderPercent: float
          recentMaxDrawdown: float   # worst % drop in recent history
          tradesLastHour: int
        """
