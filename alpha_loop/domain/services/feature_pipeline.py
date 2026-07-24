"""FeaturePipeline — computes all FR-11 features from a bar series."""

import logging
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd

from ..entities.market_bar import MarketBar

logger = logging.getLogger(__name__)

FEATURE_VERSION = 2


def compute_features(bars: list[MarketBar]) -> list[MarketBar]:
    """
    Compute all FR-11 features in-place on a list of MarketBar objects.
    Requires at least 14 bars for full ATR calculation.
    Returns bars with feature fields populated (None where insufficient history).
    """
    if not bars:
        return bars

    df = _bars_to_df(bars)
    df = _add_features(df)

    for i, bar in enumerate(bars):
        row = df.iloc[i]
        bar.feature_version = FEATURE_VERSION
        bar.momentum_5 = _dec(row.get("momentum_5"))
        bar.relative_volume_10 = _dec(row.get("relative_volume_10"))
        bar.relative_volume_slope_3 = _dec(row.get("relative_volume_slope_3"))
        bar.vwap_distance = _dec(row.get("vwap_distance"))
        bar.atr_14 = _dec(row.get("atr_14"))
        bar.volatility_regime = row.get("volatility_regime")
        bar.pullback_depth = _dec(row.get("pullback_depth"))
        # time_since_launch_minutes set by caller if launch_time is known
        bar.spread_estimate_bps = _dec(row.get("spread_estimate_bps"))
        bar.lifecycle_regime = row.get("lifecycle_regime") or None

    return bars


def _add_features(df: pd.DataFrame) -> pd.DataFrame:
    # momentum_5: (close / close[5] bars ago) - 1
    df["momentum_5"] = (df["close"] / df["close"].shift(5)) - 1

    # relative_volume_10: volume / rolling 10-bar avg volume
    avg_vol = df["volume"].rolling(10).mean()
    df["relative_volume_10"] = df["volume"] / avg_vol.replace(0, pd.NA)

    # relative_volume_slope_3: 3-bar slope of relative_volume_10
    df["relative_volume_slope_3"] = df["relative_volume_10"].diff(3) / 3

    # vwap_distance: (close - VWAP) / VWAP  (session VWAP using cumulative)
    df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
    df["vwap_distance"] = (df["close"] - df["vwap"]) / df["vwap"].replace(0, pd.NA)

    # atr_14: 14-bar ATR
    high_low = df["high"] - df["low"]
    high_prev = (df["high"] - df["close"].shift(1)).abs()
    low_prev = (df["low"] - df["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_prev, low_prev], axis=1).max(axis=1)
    df["atr_14"] = true_range.rolling(14).mean()

    # volatility_regime: percentile rank of atr_14 over rolling 100 bars
    df["atr_rank"] = df["atr_14"].rolling(100, min_periods=14).rank(pct=True)
    df["volatility_regime"] = df["atr_rank"].apply(_regime_label)

    # pullback_depth: % drop from 20-bar rolling high
    rolling_high = df["high"].rolling(20).max()
    df["pullback_depth"] = (df["close"] - rolling_high) / rolling_high.replace(0, pd.NA)

    # spread_estimate_bps: (high - low) / close * 10000 / 2  (mid-range proxy)
    df["spread_estimate_bps"] = ((df["high"] - df["low"]) / df["close"].replace(0, pd.NA)) * 5000

    # lifecycle_regime: token lifecycle state from volume + momentum signals
    df = _add_lifecycle_regime(df)

    return df


def _add_lifecycle_regime(df: pd.DataFrame) -> pd.DataFrame:
    """
    Classify each bar into a token lifecycle state using relative_volume_10,
    relative_volume_slope_3, and momentum_5. These must already be computed.

    States (priority order — later overwrites earlier for has_features rows):
      post_surge_consolidation  default when features exist but no other state matches
      accumulation              low vol, flat price
      decay                     falling price, volume below average
      ignition                  volume spike + accelerating + positive momentum
    """
    rv = pd.to_numeric(df["relative_volume_10"], errors="coerce")
    slope = pd.to_numeric(df["relative_volume_slope_3"], errors="coerce")
    mom = pd.to_numeric(df["momentum_5"], errors="coerce")

    has_features = rv.notna() & slope.notna() & mom.notna()

    result = pd.Series(index=df.index, dtype=object)
    result[has_features] = "post_surge_consolidation"
    result[has_features & (rv < 1.2) & (mom.abs() < 0.01)] = "accumulation"
    result[has_features & (mom < -0.02) & (rv < 0.8)] = "decay"
    result[has_features & (rv >= 2.0) & (slope > 0) & (mom > 0.02)] = "ignition"

    df["lifecycle_regime"] = result
    return df


def _bars_to_df(bars: list[MarketBar]) -> pd.DataFrame:
    return pd.DataFrame([{
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": float(bar.volume),
        "liquidity_usd": float(bar.liquidity_usd),
    } for bar in bars])


def _regime_label(rank: float) -> Optional[str]:
    if pd.isna(rank):
        return None
    if rank < 0.33:
        return "low"
    if rank < 0.67:
        return "medium"
    return "high"


def _dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return Decimal(str(round(value, 10)))
