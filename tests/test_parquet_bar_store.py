"""Unit tests for ParquetBarStore — write, dedup, partition, read."""

import pytest
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile

import pyarrow as pa

from alpha_loop.adapters.persistence.parquet_bar_store import (
    ParquetBarStore,
    _bars_to_table,
    _deduplicate,
    _path_date,
)
from alpha_loop.domain.entities.market_bar import MarketBar


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmpstore(tmp_path):
    return ParquetBarStore(base_dir=tmp_path)


def _bar(
    ts: datetime,
    close: float = 1.0,
    pair_address: str = "mintABC",
    chain: str = "solana",
    timeframe: str = "1m",
    volume: float = 1000.0,
    **kwargs,
) -> MarketBar:
    return MarketBar(
        symbol="TEST",
        pair_address=pair_address,
        chain=chain,
        timestamp=ts,
        timeframe=timeframe,
        open=Decimal(str(close * 0.99)),
        high=Decimal(str(close * 1.01)),
        low=Decimal(str(close * 0.98)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        liquidity_usd=Decimal("50000"),
        **kwargs,
    )


_D1 = datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc)
_D2 = datetime(2026, 4, 23, 10, 1, tzinfo=timezone.utc)
_D3 = datetime(2026, 4, 23, 10, 2, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# write_bars — basic persistence
# ---------------------------------------------------------------------------

class TestWriteBars:

    def test_creates_partition_file(self, tmpstore, tmp_path):
        tmpstore.write_bars([_bar(_D1)])
        expected = tmp_path / "solana" / "mintABC" / "2026-04-23.parquet"
        assert expected.exists()

    def test_empty_list_is_noop(self, tmpstore, tmp_path):
        tmpstore.write_bars([])
        assert list(tmp_path.rglob("*.parquet")) == []

    def test_written_rows_match_input(self, tmpstore):
        bars = [_bar(_D1, close=1.0), _bar(_D2, close=1.05)]
        tmpstore.write_bars(bars)
        table = tmpstore.read_bars("solana", "mintABC")
        assert len(table) == 2
        closes = table["close"].to_pylist()
        assert abs(closes[0] - 1.0) < 1e-9
        assert abs(closes[1] - 1.05) < 1e-9

    def test_all_ohlcv_fields_preserved(self, tmpstore):
        b = _bar(_D1, close=2.0, volume=9999.0)
        tmpstore.write_bars([b])
        table = tmpstore.read_bars("solana", "mintABC")
        row = {col: table[col][0].as_py() for col in table.column_names}
        assert abs(row["open"]   - float(b.open))   < 1e-9
        assert abs(row["high"]   - float(b.high))   < 1e-9
        assert abs(row["low"]    - float(b.low))    < 1e-9
        assert abs(row["close"]  - float(b.close))  < 1e-9
        assert abs(row["volume"] - float(b.volume)) < 1e-9

    def test_nullable_feature_columns_written_as_null(self, tmpstore):
        tmpstore.write_bars([_bar(_D1)])
        table = tmpstore.read_bars("solana", "mintABC")
        assert table["atr_14"][0].as_py() is None
        assert table["momentum_5"][0].as_py() is None
        assert table["volatility_regime"][0].as_py() is None

    def test_feature_columns_preserved_when_set(self, tmpstore):
        b = _bar(
            _D1,
            atr_14=Decimal("0.025"),
            relative_volume_10=Decimal("3.5"),
            volatility_regime="high",
            feature_version=1,
        )
        tmpstore.write_bars([b])
        table = tmpstore.read_bars("solana", "mintABC")
        assert abs(table["atr_14"][0].as_py() - 0.025) < 1e-9
        assert abs(table["relative_volume_10"][0].as_py() - 3.5) < 1e-9
        assert table["volatility_regime"][0].as_py() == "high"
        assert table["feature_version"][0].as_py() == 1


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:

    def test_duplicate_write_produces_one_row(self, tmpstore):
        tmpstore.write_bars([_bar(_D1, close=1.0)])
        tmpstore.write_bars([_bar(_D1, close=1.0)])
        table = tmpstore.read_bars("solana", "mintABC")
        assert len(table) == 1

    def test_last_write_wins_on_same_key(self, tmpstore):
        tmpstore.write_bars([_bar(_D1, close=1.0)])
        tmpstore.write_bars([_bar(_D1, close=1.99)])   # same timestamp, different close
        table = tmpstore.read_bars("solana", "mintABC")
        assert len(table) == 1
        assert abs(table["close"][0].as_py() - 1.99) < 1e-9

    def test_different_timestamps_both_kept(self, tmpstore):
        tmpstore.write_bars([_bar(_D1, close=1.0), _bar(_D2, close=1.05)])
        tmpstore.write_bars([_bar(_D1, close=1.0)])   # D1 again — no new row
        table = tmpstore.read_bars("solana", "mintABC")
        assert len(table) == 2

    def test_different_timeframes_are_distinct_rows(self, tmpstore):
        tmpstore.write_bars([
            _bar(_D1, timeframe="1m"),
            _bar(_D1, timeframe="5m"),
        ])
        table = tmpstore.read_bars("solana", "mintABC")
        assert len(table) == 2

    def test_overlapping_windows_no_duplication(self, tmpstore):
        # Simulates two consecutive get_bars(limit=5) calls with overlap
        window1 = [_bar(_D1 + timedelta(minutes=i), close=1.0 + i * 0.01) for i in range(5)]
        window2 = [_bar(_D1 + timedelta(minutes=i), close=1.0 + i * 0.01) for i in range(3, 8)]
        tmpstore.write_bars(window1)
        tmpstore.write_bars(window2)
        table = tmpstore.read_bars("solana", "mintABC")
        assert len(table) == 8   # 0..7 unique minutes


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------

class TestPartitioning:

    def test_separate_chains_create_separate_files(self, tmpstore, tmp_path):
        tmpstore.write_bars([_bar(_D1, chain="solana")])
        tmpstore.write_bars([_bar(_D1, chain="bsc", pair_address="mintABC")])
        assert (tmp_path / "solana" / "mintABC" / "2026-04-23.parquet").exists()
        assert (tmp_path / "bsc"    / "mintABC" / "2026-04-23.parquet").exists()

    def test_separate_pairs_create_separate_files(self, tmpstore, tmp_path):
        tmpstore.write_bars([_bar(_D1, pair_address="mintAAA")])
        tmpstore.write_bars([_bar(_D1, pair_address="mintBBB")])
        assert (tmp_path / "solana" / "mintAAA" / "2026-04-23.parquet").exists()
        assert (tmp_path / "solana" / "mintBBB" / "2026-04-23.parquet").exists()

    def test_bars_spanning_midnight_split_into_two_files(self, tmpstore, tmp_path):
        before_midnight = datetime(2026, 4, 22, 23, 59, tzinfo=timezone.utc)
        after_midnight  = datetime(2026, 4, 23,  0,  1, tzinfo=timezone.utc)
        tmpstore.write_bars([_bar(before_midnight), _bar(after_midnight)])
        assert (tmp_path / "solana" / "mintABC" / "2026-04-22.parquet").exists()
        assert (tmp_path / "solana" / "mintABC" / "2026-04-23.parquet").exists()

    def test_list_partitions_returns_sorted(self, tmpstore):
        for d in [22, 24, 23]:
            ts = datetime(2026, 4, d, 10, 0, tzinfo=timezone.utc)
            tmpstore.write_bars([_bar(ts)])
        partitions = tmpstore.list_partitions("solana", "mintABC")
        dates = [_path_date(p) for p in partitions]
        assert dates == sorted(dates)

    def test_list_partitions_empty_for_unknown_pair(self, tmpstore):
        assert tmpstore.list_partitions("solana", "nonexistent") == []


# ---------------------------------------------------------------------------
# read_bars — filtering and ordering
# ---------------------------------------------------------------------------

class TestReadBars:

    def _write_three_days(self, store):
        for day in [22, 23, 24]:
            ts = datetime(2026, 4, day, 10, 0, tzinfo=timezone.utc)
            store.write_bars([_bar(ts, close=float(day))])

    def test_returns_empty_table_for_unknown_pair(self, tmpstore):
        table = tmpstore.read_bars("solana", "nonexistent")
        assert len(table) == 0

    def test_reads_all_days_when_no_filter(self, tmpstore):
        self._write_three_days(tmpstore)
        table = tmpstore.read_bars("solana", "mintABC")
        assert len(table) == 3

    def test_date_from_filters_earlier_partitions(self, tmpstore):
        self._write_three_days(tmpstore)
        table = tmpstore.read_bars("solana", "mintABC", date_from=date(2026, 4, 23))
        assert len(table) == 2
        closes = table["close"].to_pylist()
        assert all(c >= 23.0 for c in closes)

    def test_date_to_filters_later_partitions(self, tmpstore):
        self._write_three_days(tmpstore)
        table = tmpstore.read_bars("solana", "mintABC", date_to=date(2026, 4, 23))
        assert len(table) == 2
        closes = table["close"].to_pylist()
        assert all(c <= 23.0 for c in closes)

    def test_date_range_exact(self, tmpstore):
        self._write_three_days(tmpstore)
        table = tmpstore.read_bars(
            "solana", "mintABC",
            date_from=date(2026, 4, 23),
            date_to=date(2026, 4, 23),
        )
        assert len(table) == 1
        assert abs(table["close"][0].as_py() - 23.0) < 1e-9

    def test_result_is_sorted_by_timestamp(self, tmpstore):
        # Write out of order
        bars = [_bar(_D3, close=3.0), _bar(_D1, close=1.0), _bar(_D2, close=2.0)]
        tmpstore.write_bars(bars)
        table = tmpstore.read_bars("solana", "mintABC")
        timestamps = table["timestamp"].to_pylist()
        assert timestamps == sorted(timestamps)

    def test_returns_pyarrow_table(self, tmpstore):
        tmpstore.write_bars([_bar(_D1)])
        result = tmpstore.read_bars("solana", "mintABC")
        assert isinstance(result, pa.Table)


# ---------------------------------------------------------------------------
# _deduplicate helper (unit)
# ---------------------------------------------------------------------------

class TestDeduplicateHelper:

    def _make_table(self, rows: list[tuple]) -> pa.Table:
        """rows = list of (pair_address, timestamp_str, timeframe, close)"""
        from alpha_loop.adapters.persistence.parquet_bar_store import _SCHEMA
        bars = []
        for pair, ts_str, tf, close in rows:
            ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
            bars.append(_bar(ts, close=close, pair_address=pair, timeframe=tf))
        return _bars_to_table(bars)

    def test_no_duplicates_unchanged(self):
        table = self._make_table([
            ("mintA", "2026-04-23T10:00:00", "1m", 1.0),
            ("mintA", "2026-04-23T10:01:00", "1m", 1.05),
        ])
        result = _deduplicate(table)
        assert len(result) == 2

    def test_duplicate_removed(self):
        table = self._make_table([
            ("mintA", "2026-04-23T10:00:00", "1m", 1.0),
            ("mintA", "2026-04-23T10:00:00", "1m", 1.99),
        ])
        result = _deduplicate(table)
        assert len(result) == 1
        assert abs(result["close"][0].as_py() - 1.99) < 1e-9   # last wins

    def test_different_pairs_not_deduplicated(self):
        table = self._make_table([
            ("mintA", "2026-04-23T10:00:00", "1m", 1.0),
            ("mintB", "2026-04-23T10:00:00", "1m", 1.0),
        ])
        result = _deduplicate(table)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _path_date helper
# ---------------------------------------------------------------------------

class TestPathDate:

    def test_extracts_date_from_filename(self, tmp_path):
        p = tmp_path / "2026-04-23.parquet"
        p.touch()
        assert _path_date(p) == date(2026, 4, 23)
