"""ParquetBarStore — append-and-deduplicate bar data to partitioned Parquet files.

Partition layout:
    {base_dir}/{chain}/{pair_address}/{YYYY-MM-DD}.parquet

One file per (chain, pair, date). Writes are idempotent: existing rows with the
same (pair_address, timestamp, timeframe) are kept; new rows are appended.
Decimal fields are stored as float64 (sufficient for OHLCV analysis).
"""

import logging
from datetime import date, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from ...domain.entities.market_bar import MarketBar

logger = logging.getLogger(__name__)

# Schema for all bar columns — must stay in sync with MarketBar
_SCHEMA = pa.schema([
    ("pair_address",                pa.string()),
    ("symbol",                      pa.string()),
    ("chain",                       pa.string()),
    ("timestamp",                   pa.timestamp("us", tz="UTC")),
    ("timeframe",                   pa.string()),
    ("open",                        pa.float64()),
    ("high",                        pa.float64()),
    ("low",                         pa.float64()),
    ("close",                       pa.float64()),
    ("volume",                      pa.float64()),
    ("liquidity_usd",               pa.float64()),
    ("token_status",                pa.string()),
    # Feature columns (nullable)
    ("feature_version",             pa.int32()),
    ("momentum_5",                  pa.float64()),
    ("relative_volume_10",          pa.float64()),
    ("relative_volume_slope_3",     pa.float64()),
    ("vwap_distance",               pa.float64()),
    ("atr_14",                      pa.float64()),
    ("volatility_regime",           pa.string()),
    ("pullback_depth",              pa.float64()),
    ("time_since_launch_minutes",   pa.float64()),
    ("spread_estimate_bps",         pa.float64()),
    ("min_recent_trades",           pa.int32()),
])

# Deduplication key — uniquely identifies a bar row
_DEDUP_KEY = ("pair_address", "timestamp", "timeframe")


class ParquetBarStore:
    """
    Sync (non-async) Parquet writer for MarketBar data.

    Call write_bars() after every market.get_bars() fetch. Files are
    appended+deduplicated so calling multiple times with overlapping
    bar windows is safe.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    def write_bars(self, bars: list[MarketBar]) -> None:
        """Write bars to Parquet, partitioned by (chain, pair_address, date)."""
        if not bars:
            return

        # Group by (chain, pair_address, date) — bars may span midnight
        groups: dict[tuple, list[MarketBar]] = {}
        for bar in bars:
            dt = bar.timestamp.astimezone(timezone.utc).date()
            key = (bar.chain, bar.pair_address, dt)
            groups.setdefault(key, []).append(bar)

        for (chain, pair_address, dt), group in groups.items():
            self._write_partition(chain, pair_address, dt, group)

    def _write_partition(
        self,
        chain: str,
        pair_address: str,
        dt: date,
        bars: list[MarketBar],
    ) -> None:
        path = self._partition_path(chain, pair_address, dt)
        path.parent.mkdir(parents=True, exist_ok=True)

        new_table = _bars_to_table(bars)

        if path.exists():
            try:
                existing = pq.read_table(path)
                # Cast to canonical schema so concat works even if a column was added later
                existing = existing.cast(new_table.schema)
                combined = pa.concat_tables([existing, new_table])
                deduped = _deduplicate(combined)
            except Exception as exc:
                logger.warning("Failed to read existing Parquet %s — overwriting: %s", path, exc)
                deduped = new_table
        else:
            deduped = new_table

        pq.write_table(deduped, path, compression="snappy")
        logger.debug(
            "Parquet written: %s (%d rows)",
            path.relative_to(self._base_dir), len(deduped),
        )

    def _partition_path(self, chain: str, pair_address: str, dt: date) -> Path:
        return self._base_dir / chain / pair_address / f"{dt.isoformat()}.parquet"

    def list_partitions(self, chain: str, pair_address: str) -> list[Path]:
        """Return all Parquet partition files for a given pair, sorted by date."""
        base = self._base_dir / chain / pair_address
        if not base.exists():
            return []
        return sorted(base.glob("*.parquet"))

    def read_bars(
        self,
        chain: str,
        pair_address: str,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> pa.Table:
        """
        Read all bars for a pair across dates, optionally filtered.
        Returns a pyarrow Table sorted by timestamp.
        """
        partitions = self.list_partitions(chain, pair_address)
        if date_from:
            partitions = [p for p in partitions if _path_date(p) >= date_from]
        if date_to:
            partitions = [p for p in partitions if _path_date(p) <= date_to]

        if not partitions:
            return pa.table(
                {field.name: pa.array([], type=field.type) for field in _SCHEMA},
                schema=_SCHEMA,
            )

        tables = [pq.read_table(p) for p in partitions]
        combined = pa.concat_tables(tables)
        deduped = _deduplicate(combined)

        # Sort by timestamp
        sort_indices = pa.compute.sort_indices(deduped, sort_keys=[("timestamp", "ascending")])
        return deduped.take(sort_indices)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bars_to_table(bars: list[MarketBar]) -> pa.Table:
    def _f(val) -> Optional[float]:
        return float(val) if val is not None else None

    def _i(val) -> Optional[int]:
        return int(val) if val is not None else None

    data = {
        "pair_address":              [b.pair_address for b in bars],
        "symbol":                    [b.symbol for b in bars],
        "chain":                     [b.chain for b in bars],
        "timestamp":                 [b.timestamp for b in bars],
        "timeframe":                 [b.timeframe for b in bars],
        "open":                      [float(b.open) for b in bars],
        "high":                      [float(b.high) for b in bars],
        "low":                       [float(b.low) for b in bars],
        "close":                     [float(b.close) for b in bars],
        "volume":                    [float(b.volume) for b in bars],
        "liquidity_usd":             [float(b.liquidity_usd) for b in bars],
        "token_status":              [b.token_status for b in bars],
        "feature_version":           [_i(b.feature_version) for b in bars],
        "momentum_5":                [_f(b.momentum_5) for b in bars],
        "relative_volume_10":        [_f(b.relative_volume_10) for b in bars],
        "relative_volume_slope_3":   [_f(b.relative_volume_slope_3) for b in bars],
        "vwap_distance":             [_f(b.vwap_distance) for b in bars],
        "atr_14":                    [_f(b.atr_14) for b in bars],
        "volatility_regime":         [b.volatility_regime for b in bars],
        "pullback_depth":            [_f(b.pullback_depth) for b in bars],
        "time_since_launch_minutes": [_f(b.time_since_launch_minutes) for b in bars],
        "spread_estimate_bps":       [_f(b.spread_estimate_bps) for b in bars],
        "min_recent_trades":         [_i(b.min_recent_trades) for b in bars],
    }
    return pa.table(data, schema=_SCHEMA)


def _deduplicate(table: pa.Table) -> pa.Table:
    """Keep the last occurrence of each (pair_address, timestamp, timeframe) row."""
    pair_addresses = table["pair_address"].to_pylist()
    timestamps     = table["timestamp"].to_pylist()
    timeframes     = table["timeframe"].to_pylist()

    # Scan from the end — last row wins for each key
    seen: set[tuple] = set()
    keep_indices = []
    for i in range(len(pair_addresses) - 1, -1, -1):
        k = (pair_addresses[i], timestamps[i], timeframes[i])
        if k not in seen:
            seen.add(k)
            keep_indices.append(i)

    keep_indices.sort()
    return table.take(pa.array(keep_indices, type=pa.int64()))


def _path_date(path: Path) -> date:
    """Extract date from partition filename YYYY-MM-DD.parquet."""
    return date.fromisoformat(path.stem)
