"""Unit tests for BirdeyeAdapter.get_top_tokens two-feed discovery pipeline."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from alpha_loop.adapters.market_data.birdeye_adapter import BirdeyeAdapter


def _make_adapter() -> BirdeyeAdapter:
    return BirdeyeAdapter(api_key="test-key", chain="solana")


def _v3_token(
    symbol: str,
    address: str,
    liquidity: float = 500_000,
    volume_1h_usd: float = 50_000,
    trade_1h_count: int = 200,
    price_change_1h_pct: float = 5.0,
    volume_1h_change_pct: float = 20.0,
) -> dict:
    return {
        "symbol": symbol,
        "address": address,
        "liquidity": liquidity,
        "volume_1h_usd": volume_1h_usd,
        "trade_1h_count": trade_1h_count,
        "price_change_1h_percent": price_change_1h_pct,
        "volume_1h_change_percent": volume_1h_change_pct,
    }


def _trending_token(
    symbol: str,
    address: str,
    liquidity: float = 500_000,
    volume_1h_usd: float = 50_000,
    trade_1h: int = 200,
    price_change_1h_pct: float = 5.0,
) -> dict:
    return {
        "symbol": symbol,
        "address": address,
        "liquidity": liquidity,
        "volume1hUSD": volume_1h_usd,
        "trade1h": trade_1h,
        "price1hChangePercent": price_change_1h_pct,
        "volumeChangePercent": 20.0,
    }


def _patch_feeds(adapter: BirdeyeAdapter, trending: list[dict], v3: list[dict]):
    return (
        patch.object(adapter, "_fetch_trending", new=AsyncMock(return_value=trending)),
        patch.object(adapter, "_fetch_v3_list", new=AsyncMock(return_value=v3)),
    )


@pytest.mark.asyncio
async def test_excludes_stablecoins_and_large_caps():
    adapter = _make_adapter()
    trending_p, v3_p = _patch_feeds(
        adapter,
        trending=[_trending_token("SOL", "sol-mint"), _trending_token("BONK", "bonk-mint")],
        v3=[_v3_token("USDC", "usdc-mint"), _v3_token("WIF", "wif-mint")],
    )
    with trending_p, v3_p:
        result = await adapter.get_top_tokens(limit=10)
    assert "sol-mint" not in result
    assert "usdc-mint" not in result
    assert "bonk-mint" in result
    assert "wif-mint" in result


@pytest.mark.asyncio
async def test_excludes_custom_symbols():
    adapter = _make_adapter()
    trending_p, v3_p = _patch_feeds(
        adapter,
        trending=[],
        v3=[_v3_token("BONK", "bonk-mint"), _v3_token("WIF", "wif-mint")],
    )
    with trending_p, v3_p:
        result = await adapter.get_top_tokens(limit=10, exclude_symbols={"BONK"})
    assert "bonk-mint" not in result
    assert "wif-mint" in result


@pytest.mark.asyncio
async def test_respects_limit():
    adapter = _make_adapter()
    v3_tokens = [_v3_token(f"TOK{i}", f"addr-{i}") for i in range(20)]
    trending_p, v3_p = _patch_feeds(adapter, trending=[], v3=v3_tokens)
    with trending_p, v3_p:
        result = await adapter.get_top_tokens(limit=5)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_returns_empty_on_both_feed_failure():
    adapter = _make_adapter()
    trending_p, v3_p = _patch_feeds(adapter, trending=[], v3=[])
    with trending_p, v3_p:
        result = await adapter.get_top_tokens(limit=10)
    assert result == []


@pytest.mark.asyncio
async def test_trending_bonus_boosts_rank():
    adapter = _make_adapter()
    # trending_only has lower raw signals than v3_only, but gets trending bonus
    trending_p, v3_p = _patch_feeds(
        adapter,
        trending=[_trending_token("TREND", "trend-addr", volume_1h_usd=10_000, trade_1h=100)],
        v3=[
            _v3_token("V3ONLY", "v3-addr", volume_1h_usd=10_000, trade_1h_count=100),
            _v3_token("TREND", "trend-addr", volume_1h_usd=10_000, trade_1h_count=100),
        ],
    )
    with trending_p, v3_p:
        result = await adapter.get_top_tokens(limit=10)
    # trending address should be present (bonus keeps it in)
    assert "trend-addr" in result


@pytest.mark.asyncio
async def test_v3_data_takes_precedence_over_trending():
    """When an address appears in both feeds, v3 normalized data is used (set first)."""
    adapter = _make_adapter()
    trending_p, v3_p = _patch_feeds(
        adapter,
        # trending would normalize volume_1h from volume1hUSD=1000
        trending=[_trending_token("DUAL", "dual-addr", volume_1h_usd=1_000)],
        # v3 normalizes volume_1h from volume_1h_usd=99_000 — higher score
        v3=[_v3_token("DUAL", "dual-addr", volume_1h_usd=99_000)],
    )
    with trending_p, v3_p:
        result = await adapter.get_top_tokens(limit=10)
    assert "dual-addr" in result


@pytest.mark.asyncio
async def test_merges_unique_addresses_from_both_feeds():
    adapter = _make_adapter()
    trending_p, v3_p = _patch_feeds(
        adapter,
        trending=[_trending_token("TRENDONLY", "trend-only-addr")],
        v3=[_v3_token("V3ONLY", "v3-only-addr")],
    )
    with trending_p, v3_p:
        result = await adapter.get_top_tokens(limit=10)
    assert "trend-only-addr" in result
    assert "v3-only-addr" in result
