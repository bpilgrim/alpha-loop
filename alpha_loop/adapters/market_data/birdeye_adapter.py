"""BirdeyeAdapter — MarketDataPort implementation using Birdeye REST API."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import aiohttp

from ...domain.entities.market_bar import MarketBar
from ...ports.market_data_port import MarketDataPort

logger = logging.getLogger(__name__)

_BIRDEYE_BASE = "https://public-api.birdeye.so"
_TIMEFRAME_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H"}

# Liquidity thresholds (from a prior live-trading system)
_LIQUIDITY_LOW_USD = Decimal("10000")
_LIQUIDITY_FLOOR_USD = Decimal("1000")

# Rug signals (from prior live-trading experience)
_MAX_TOP10_HOLDER_PCT = 0.85
_MAX_DRAWDOWN_THRESHOLD = -0.75
_MIN_TRADES_LAST_HOUR = 60


_OVERVIEW_CACHE_TTL = 30  # seconds — reuse within one data-collection cycle


class BirdeyeAdapter(MarketDataPort):
    def __init__(self, api_key: str, chain: str = "solana") -> None:
        self._api_key = api_key
        self._chain = chain
        self._headers = {
            "X-API-KEY": api_key,
            "x-chain": chain,
        }
        self._overview_cache: dict[str, tuple[float, dict]] = {}  # address → (ts, data)
        self._launch_time_cache: dict[str, Optional[datetime]] = {}  # address → creation time (immutable)
        self._launch_time_unavailable: bool = False  # set True on first 401 from token_creation_info
        self._security_unavailable: bool = False  # set True on first 401 from token_security

    async def get_bars(self, pair_address: str, timeframe: str, limit: int) -> list[MarketBar]:
        tf = _TIMEFRAME_MAP.get(timeframe, "1m")
        minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(timeframe, 1)
        now = int(time.time())
        time_from = now - (limit * minutes_per_bar * 60)

        raw = await self._get(
            f"{_BIRDEYE_BASE}/defi/ohlcv",
            {"address": pair_address, "type": tf, "time_from": time_from, "time_to": now},
        )
        await asyncio.sleep(1.0)
        overview = await self._get_token_overview(pair_address)

        liquidity = Decimal(str(overview.get("liquidity", 0)))
        items = raw.get("data", {}).get("items", [])
        return [self._normalize_bar(item, pair_address, timeframe, liquidity) for item in items]

    async def get_latest_bar(self, pair_address: str, timeframe: str) -> Optional[MarketBar]:
        bars = await self.get_bars(pair_address, timeframe, limit=2)
        if len(bars) >= 2:
            return bars[-2]    # Most recent *completed* bar
        return bars[-1] if bars else None

    async def get_token_status(self, pair_address: str) -> str:
        # Use token_overview for liquidity (free tier); security API requires paid plan
        overview = await self._get_token_overview(pair_address)
        liquidity = Decimal(str(overview.get("liquidity", 0)))

        if liquidity <= 0:
            # Try security API if available (paid plan)
            security = await self.get_security_info(pair_address)
            if security:
                if security.get("freezeable") or security.get("mutableMetadata"):
                    return "rugpull_flagged"
                top10 = security.get("top10HolderPercent", 0.0)
                if top10 > _MAX_TOP10_HOLDER_PCT:
                    return "rugpull_flagged"
                liquidity = Decimal(str(security.get("liquidity", 0)))

        if liquidity < _LIQUIDITY_FLOOR_USD:
            return "pair_dead"
        if liquidity < _LIQUIDITY_LOW_USD:
            return "liquidity_low"
        return "active"

    async def _get_token_overview(self, pair_address: str) -> dict:
        cached = self._overview_cache.get(pair_address)
        if cached and (time.time() - cached[0]) < _OVERVIEW_CACHE_TTL:
            return cached[1]
        try:
            raw = await self._get(
                f"{_BIRDEYE_BASE}/defi/token_overview",
                {"address": pair_address},
            )
            data = raw.get("data", {}) or {}
            self._overview_cache[pair_address] = (time.time(), data)
            return data
        except Exception as exc:
            logger.debug(f"token_overview failed for {pair_address}: {exc}")
            return {}

    async def get_liquidity(self, pair_address: str) -> Decimal:
        overview = await self._get_token_overview(pair_address)
        return Decimal(str(overview.get("liquidity", 0)))

    async def get_launch_time(self, pair_address: str) -> Optional[datetime]:
        if pair_address in self._launch_time_cache:
            return self._launch_time_cache[pair_address]
        if self._launch_time_unavailable:
            return None
        url = f"{_BIRDEYE_BASE}/defi/token_creation_info"
        result: Optional[datetime] = None
        try:
            raw = await self._get(url, {"address": pair_address})
            ts = raw.get("data", {}).get("creationTime")
            if ts:
                result = datetime.fromtimestamp(ts, tz=timezone.utc)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                self._launch_time_unavailable = True
                logger.info("token_creation_info requires a paid Birdeye plan — disabling for this session")
            else:
                logger.debug("get_launch_time failed for %s: %s", pair_address, exc)
        except Exception as exc:
            logger.debug("get_launch_time failed for %s: %s", pair_address, exc)
        self._launch_time_cache[pair_address] = result
        return result

    _STABLE_AND_LARGE_CAP: frozenset[str] = frozenset({
        "SOL", "USDC", "USDT", "WSOL", "WBTC", "WETH", "ETH", "BTC",
        "MSOL", "JITOSOL", "BSOL", "STSOL", "JSOL",
    })

    async def get_top_tokens(
        self,
        limit: int = 20,
        min_liquidity_usd: float = 10_000,
        min_volume_1h_usd: float = 5_000,
        min_trade_1h_count: int = 50,
        exclude_symbols: set[str] | None = None,
    ) -> list[str]:
        """Return mint addresses of momentum candidates via two concurrent Birdeye feeds.

        Merges /defi/token_trending and /defi/v3/token/list, scores each candidate on
        1h activity signals, and returns the top `limit` addresses.
        """
        excluded = self._STABLE_AND_LARGE_CAP | (exclude_symbols or set())

        trending_raw, v3_raw = await asyncio.gather(
            self._fetch_trending(40),
            self._fetch_v3_list(min_liquidity_usd, min_volume_1h_usd, min_trade_1h_count, 60),
            return_exceptions=True,
        )

        candidates: dict[str, dict] = {}

        if not isinstance(v3_raw, Exception):
            for item in v3_raw:
                addr = item.get("address")
                sym = (item.get("symbol") or "").upper()
                if not addr or sym in excluded:
                    continue
                candidates[addr] = self._normalize_v3(item)

        trending_addresses: set[str] = set()
        if not isinstance(trending_raw, Exception):
            for item in trending_raw:
                addr = item.get("address")
                sym = (item.get("symbol") or "").upper()
                if not addr or sym in excluded:
                    continue
                trending_addresses.add(addr)
                if addr not in candidates:
                    candidates[addr] = self._normalize_trending(item)

        if not candidates:
            logger.warning("get_top_tokens: both feeds returned no usable candidates")
            return []

        scored = self._score_discovery_candidates(candidates, trending_addresses)
        scored.sort(key=lambda x: x[1], reverse=True)

        logger.info(
            "get_top_tokens: %d candidates scored (trending=%d, v3=%d), returning top %d",
            len(scored),
            len(trending_addresses),
            len(v3_raw) if not isinstance(v3_raw, Exception) else 0,
            limit,
        )
        return [addr for addr, _ in scored[:limit]]

    async def _fetch_trending(self, fetch_limit: int) -> list[dict]:
        # Lite tier caps this endpoint; clamp to 20 to avoid 400s.
        clamped = min(fetch_limit, 20)
        try:
            raw = await self._get(
                f"{_BIRDEYE_BASE}/defi/token_trending",
                {"sort_by": "rank", "sort_type": "asc", "limit": clamped},
            )
            return raw.get("data", {}).get("tokens") or []
        except Exception as exc:
            logger.warning("_fetch_trending failed: %s", exc)
            return []

    async def _fetch_v3_list(
        self,
        min_liquidity_usd: float,
        min_volume_1h_usd: float,
        min_trade_1h_count: int,
        fetch_limit: int,
    ) -> list[dict]:
        try:
            raw = await self._get(
                f"{_BIRDEYE_BASE}/defi/v3/token/list",
                {
                    "sort_by": "volume_1h_usd",
                    "sort_type": "desc",
                    "min_liquidity": min_liquidity_usd,
                    "min_volume_1h_usd": min_volume_1h_usd,
                    "min_trade_1h_count": min_trade_1h_count,
                    "limit": fetch_limit,
                },
            )
            data = raw.get("data", {}) or {}
            return data.get("tokens") or data.get("items") or []
        except Exception as exc:
            logger.warning("_fetch_v3_list failed: %s", exc)
            return []

    def _normalize_trending(self, item: dict) -> dict:
        return {
            "volume_1h_usd": float(item.get("volume1hUSD") or item.get("v1hUSD") or 0),
            "trade_1h_count": int(item.get("trade1h") or 0),
            "price_change_1h_pct": float(item.get("price1hChangePercent") or 0),
            "liquidity_usd": float(item.get("liquidity") or 0),
            "volume_1h_change_pct": float(item.get("volumeChangePercent") or 0),
        }

    def _normalize_v3(self, item: dict) -> dict:
        return {
            "volume_1h_usd": float(item.get("volume_1h_usd") or 0),
            "trade_1h_count": int(item.get("trade_1h_count") or 0),
            "price_change_1h_pct": float(item.get("price_change_1h_percent") or 0),
            "liquidity_usd": float(item.get("liquidity") or 0),
            "volume_1h_change_pct": float(item.get("volume_1h_change_percent") or 0),
        }

    def _score_discovery_candidates(
        self,
        candidates: dict[str, dict],
        trending_addresses: set[str],
    ) -> list[tuple[str, float]]:
        """Score each candidate and return (address, score) pairs."""
        if not candidates:
            return []

        keys = ["volume_1h_usd", "trade_1h_count", "price_change_1h_pct",
                "liquidity_usd", "volume_1h_change_pct"]
        weights = {
            "volume_1h_usd": 0.30,
            "trade_1h_count": 0.20,
            "price_change_1h_pct": 0.15,
            "liquidity_usd": 0.10,
            "volume_1h_change_pct": 0.10,
        }
        trending_bonus = 0.10
        # remaining 0.05 weight reserved — all candidates get it equally (acts as base)

        mins = {k: min(c[k] for c in candidates.values()) for k in keys}
        maxs = {k: max(c[k] for c in candidates.values()) for k in keys}

        def _minmax(val: float, lo: float, hi: float) -> float:
            if hi == lo:
                return 0.5
            return (val - lo) / (hi - lo)

        results = []
        for addr, data in candidates.items():
            score = sum(
                weights[k] * _minmax(data[k], mins[k], maxs[k])
                for k in keys
            )
            if addr in trending_addresses:
                score += trending_bonus
            score += 0.05  # base component
            results.append((addr, score))
        return results

    async def get_security_info(self, pair_address: str) -> dict:
        if self._security_unavailable:
            return {}
        url = f"{_BIRDEYE_BASE}/defi/token_security"
        try:
            raw = await self._get(url, {"address": pair_address})
            data = raw.get("data", {})
            return {
                "freezeable": data.get("freezeAuthority") is not None,
                "mutableMetadata": data.get("mutableMetadata", False),
                "top10HolderPercent": data.get("top10HolderPercent", 0.0),
                "recentMaxDrawdown": data.get("maxDrawdown", 0.0),
                "tradesLastHour": data.get("trade1h", 0),
                "liquidity": data.get("liquidity", 0.0),
            }
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                self._security_unavailable = True
                logger.info("token_security requires a paid Birdeye plan — disabling for this session")
            else:
                logger.warning("get_security_info failed for %s: %s", pair_address, exc)
            return {}
        except Exception as exc:
            logger.warning("get_security_info failed for %s: %s", pair_address, exc)
            return {}

    def _normalize_bar(
        self,
        item: dict,
        pair_address: str,
        timeframe: str,
        liquidity_usd: Decimal = Decimal("0"),
    ) -> MarketBar:
        return MarketBar(
            symbol=item.get("address", pair_address),
            pair_address=pair_address,
            chain=self._chain,
            timestamp=datetime.fromtimestamp(item["unixTime"], tz=timezone.utc),
            timeframe=timeframe,
            open=Decimal(str(item["o"])),
            high=Decimal(str(item["h"])),
            low=Decimal(str(item["l"])),
            close=Decimal(str(item["c"])),
            volume=Decimal(str(item.get("v", 0))),
            liquidity_usd=liquidity_usd,
        )

    async def _get(self, url: str, params: dict, _retries: int = 3) -> dict:
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=self._headers,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        resp.raise_for_status()
                        return await resp.json()
            except aiohttp.ClientResponseError as exc:
                if 400 <= exc.status < 500:
                    raise  # 4xx = permanent client error; retrying won't help
                last_exc = exc
                if attempt < _retries - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "Birdeye request failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, _retries, wait, exc,
                    )
                    await asyncio.sleep(wait)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < _retries - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "Birdeye request failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, _retries, wait, exc,
                    )
                    await asyncio.sleep(wait)
        raise last_exc
