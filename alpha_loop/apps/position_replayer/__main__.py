"""Position replayer — reconstructs PositionMonitor decisions bar-by-bar for any position.

Primary use case: validate whether a monitoring gap (machine sleep, network outage)
caused a position to avoid or survive a stop-out.

Usage:
    # Replay a specific position
    python -m alpha_loop.apps.position_replayer --position-id <UUID> --strategy alpha_loop/configs/strategies/breakout_reclaim_v9.yaml

    # Replay all positions in a session
    python -m alpha_loop.apps.position_replayer --session-id <UUID> --strategy alpha_loop/configs/strategies/breakout_reclaim_v9.yaml

    # Fetch Birdeye bars for any gaps (e.g. machine sleep windows)
    python -m alpha_loop.apps.position_replayer --session-id <UUID> --strategy ... --fetch-gaps

    # What-if: override confirmation bars
    python -m alpha_loop.apps.position_replayer --position-id <UUID> --strategy ... --confirmation-bars 1
"""

import argparse
import asyncio
import copy
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# Resolve project root so relative imports work when run as __main__
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from alpha_loop.adapters.market_data.birdeye_adapter import BirdeyeAdapter
from alpha_loop.domain.entities.market_bar import MarketBar
from alpha_loop.domain.entities.position import Position, TakeProfitLevel
from alpha_loop.domain.services.position_monitor import PositionMonitor
from alpha_loop.domain.services.strategy_engine import StrategyConfig
from alpha_loop.configs.loader import load_strategy_config

_GAP_THRESHOLD_SECONDS = 90  # two consecutive bars >90s apart → gap


def _dsn(raw: str) -> str:
    return raw.replace("postgresql+asyncpg://", "postgresql://")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _fetch_positions(conn, session_id=None, position_id=None) -> list[dict]:
    if position_id:
        rows = await conn.fetch(
            "SELECT * FROM positions WHERE id = $1", UUID(position_id)
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM positions WHERE session_id = $1 ORDER BY entry_time",
            UUID(session_id),
        )
    return [dict(r) for r in rows]


async def _fetch_bars(conn, pair_address: str, since: datetime, until: datetime) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT timestamp, open, high, low, close, volume, liquidity_usd,
               atr_14, token_status
        FROM market_snapshots
        WHERE pair_address = $1
          AND timeframe    = '1m'
          AND timestamp   >= $2
          AND timestamp   <= $3
        ORDER BY timestamp ASC
        """,
        pair_address, since, until,
    )
    return [dict(r) for r in rows]


async def _fetch_trades(conn, position_id: UUID) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM trades WHERE position_id = $1", position_id
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def _detect_gaps(bars: list[dict]) -> list[tuple[datetime, datetime]]:
    gaps = []
    for i in range(1, len(bars)):
        delta = (bars[i]["timestamp"] - bars[i - 1]["timestamp"]).total_seconds()
        if delta > _GAP_THRESHOLD_SECONDS:
            gaps.append((bars[i - 1]["timestamp"], bars[i]["timestamp"]))
    return gaps


# ---------------------------------------------------------------------------
# Birdeye gap fill
# ---------------------------------------------------------------------------

async def _fill_gap(
    adapter: BirdeyeAdapter,
    pair_address: str,
    gap_start: datetime,
    gap_end: datetime,
) -> list[dict]:
    limit = int((gap_end - gap_start).total_seconds() / 60) + 5
    limit = min(limit, 1000)
    bars = await adapter.get_bars(pair_address, "1m", limit)
    # Filter to only bars within the gap window
    return [
        {
            "timestamp": b.timestamp,
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume": b.volume, "liquidity_usd": b.liquidity_usd,
            "atr_14": b.atr_14, "token_status": b.token_status,
            "source": "birdeye_backfill",
        }
        for b in bars
        if gap_start < b.timestamp < gap_end
    ]


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------

def _bar_to_market_bar(row: dict, pair_address: str) -> MarketBar:
    return MarketBar(
        symbol="",
        pair_address=pair_address,
        chain="solana",
        timestamp=row["timestamp"],
        timeframe="1m",
        open=Decimal(str(row["open"]))  if row["open"]  else Decimal("0"),
        high=Decimal(str(row["high"]))  if row["high"]  else Decimal("0"),
        low=Decimal(str(row["low"]))    if row["low"]   else Decimal("0"),
        close=Decimal(str(row["close"])) if row["close"] else Decimal("0"),
        volume=Decimal(str(row["volume"])) if row["volume"] else Decimal("0"),
        liquidity_usd=Decimal(str(row["liquidity_usd"])) if row["liquidity_usd"] else Decimal("0"),
        token_status=row.get("token_status") or "active",
        atr_14=Decimal(str(row["atr_14"])) if row["atr_14"] else None,
    )


def _compute_atr(bars: list[dict], period: int = 14) -> Decimal | None:
    """Wilder ATR from OHLCV bars (chronological order). Returns None if insufficient data."""
    needed = period + 1
    if len(bars) < needed:
        return None
    d = lambda x: Decimal(str(x)) if x else Decimal("0")
    trs = []
    for i in range(1, len(bars)):
        h = d(bars[i]["high"]); l = d(bars[i]["low"]); pc = d(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


async def _fetch_signal_bar_atr(conn, pair_address: str, entry_time) -> Decimal | None:
    """Compute ATR-14 from pre-entry bars stored in market_snapshots."""
    rows = await conn.fetch(
        """
        SELECT high, low, close FROM market_snapshots
        WHERE pair_address = $1
          AND timeframe    = '1m'
          AND timestamp   <= $2
        ORDER BY timestamp DESC
        LIMIT 30
        """,
        pair_address, entry_time,
    )
    if not rows:
        return None
    bars = [dict(r) for r in reversed(rows)]  # chronological
    return _compute_atr(bars)


async def _replay(
    conn,
    position_row: dict,
    bars: list[dict],
    cfg: StrategyConfig,
    actual_exit_reason: str | None,
    actual_r: Decimal | None = None,
) -> dict:
    """Run PositionMonitor against the full bar sequence and return a replay report."""

    monitor = PositionMonitor(cfg)
    pair_address = position_row["pair_address"]
    entry_price = Decimal(str(position_row["entry_price"]))
    initial_stop = position_row.get("initial_stop_price")
    stop_source = "db"

    if initial_stop is None:
        # Pre-migration position: reconstruct from the signal bar's ATR
        atr = await _fetch_signal_bar_atr(conn, pair_address, position_row["entry_time"])
        if atr is not None:
            initial_stop = entry_price - atr * cfg.atr_multiple
            stop_source = "reconstructed"
    if initial_stop is None:
        # Last resort: stored stop_price (may be trailed; replay will be inaccurate)
        initial_stop = position_row["stop_price"]
        stop_source = "fallback_trailed"

    # Build a fresh Position starting from initial state
    sim_pos = Position(
        id=position_row["id"],
        pair_address=pair_address,
        entry_time=position_row["entry_time"],
        entry_price=entry_price,
        stop_price=Decimal(str(initial_stop)),
        initial_stop_price=Decimal(str(initial_stop)),
        take_profit_levels=[
            TakeProfitLevel(
                price=Decimal(str(tp["price"])),
                portion_pct=Decimal(str(tp["portion_pct"])),
                triggered=False,
            )
            for tp in (
                __import__("json").loads(position_row["take_profit_levels"])
                if isinstance(position_row["take_profit_levels"], str)
                else position_row["take_profit_levels"] or []
            )
        ],
        status="open",
    )

    timeline = []
    simulated_exit = None

    for row in bars:
        bar = _bar_to_market_bar(row, pair_address)
        source = row.get("source", "db")

        stop_before = sim_pos.stop_price

        decision = monitor.evaluate(sim_pos, bar, now=bar.timestamp)

        timeline.append({
            "ts": bar.timestamp,
            "low": bar.low,
            "close": bar.close,
            "stop": sim_pos.stop_price,   # may have been ratcheted up inside evaluate()
            "stop_before": stop_before,
            "breach_count": sim_pos.trailing_breach_count,
            "exit": decision.should_exit,
            "exit_reason": decision.exit_reason if decision.should_exit else None,
            "source": source,
        })

        if decision.should_exit and simulated_exit is None:
            # Stop/trailing exits fill at the stop level; other exits at bar close.
            if decision.exit_reason in ("stop", "trailing_stop"):
                sim_exit_price = stop_before
            else:
                sim_exit_price = bar.close
            simulated_exit = {
                "ts": bar.timestamp,
                "reason": decision.exit_reason,
                "stop_price": stop_before,
                "bar_low": bar.low,
                "exit_price": sim_exit_price,
                "source": source,
            }
            break

    # Simulated R: (exit - entry) / (entry - initial_stop)
    initial_stop_dec = Decimal(str(initial_stop))
    risk_per_unit = entry_price - initial_stop_dec
    simulated_r = None
    if simulated_exit is not None and risk_per_unit > 0:
        simulated_r = (simulated_exit["exit_price"] - entry_price) / risk_per_unit

    return {
        "position_id": str(position_row["id"]),
        "symbol": position_row["symbol"],
        "pair_address": pair_address,
        "entry_time": position_row["entry_time"],
        "entry_price": entry_price,
        "initial_stop": initial_stop_dec,
        "stop_source": stop_source,
        "actual_exit_reason": actual_exit_reason,
        "actual_r": Decimal(str(actual_r)) if actual_r is not None else None,
        "simulated_exit": simulated_exit,
        "simulated_r": simulated_r,
        "timeline": timeline,
        "confirmation_bars": cfg.trailing_stop_confirmation_bars,
        "hard_floor_atr": cfg.trailing_stop_hard_floor_atr,
    }


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def _print_report(report: dict, gaps: list[tuple]) -> None:
    sym = report["symbol"] or report["pair_address"][:8]
    stop_src = report.get("stop_source", "db")
    stop_note = "" if stop_src == "db" else f" [{stop_src}]"
    if stop_src == "fallback_trailed":
        stop_note = " [WARNING: trailed stop — replay inaccurate, no signal bar ATR found]"

    initial_stop_fmt = f"{report['initial_stop']:.10f}"
    entry_price_fmt  = f"{report['entry_price']:.10f}"
    print(f"\n{'='*72}")
    print(f"  {sym}  |  entry {report['entry_time'].strftime('%H:%M UTC')}  "
          f"|  entry_price={entry_price_fmt}  |  initial_stop={initial_stop_fmt}{stop_note}")
    print(f"  confirmation_bars={report['confirmation_bars']}  "
          f"hard_floor_atr={report['hard_floor_atr']}")
    print(f"{'='*72}")

    gap_set = {(s, e) for s, e in gaps}
    in_gap = False

    for row in report["timeline"]:
        ts = row["ts"].strftime("%H:%M")
        src_tag = " [backfill]" if row["source"] == "birdeye_backfill" else ""

        # Mark gap boundaries
        for gs, ge in gap_set:
            if not in_gap and row["ts"] > gs and row["source"] == "birdeye_backfill":
                in_gap = True
                gap_mins = int((ge - gs).total_seconds() / 60)
                print(f"\n  ~~~ GAP: {gap_mins} bars missing "
                      f"({gs.strftime('%H:%M')}–{ge.strftime('%H:%M')} UTC) ~~~")
            elif in_gap and row["source"] == "db":
                in_gap = False
                print(f"  ~~~ MONITORING RESUMED ~~~\n")

        breach = f"  breach={row['breach_count']}" if row["breach_count"] > 0 else ""
        stop_moved = " ↑" if row["stop"] > row["stop_before"] else ""

        if row["exit"]:
            print(f"  {ts}{src_tag}  *** {row['exit_reason'].upper()} ***  "
                  f"low={row['low']:.8f}  stop={row['stop']:.8f}{breach}")
        else:
            print(f"  {ts}{src_tag}  low={row['low']:.8f}  "
                  f"stop={row['stop']:.8f}{stop_moved}{breach}")

    if in_gap:
        print(f"\n  ~~~ GAP: bars not in DB and --fetch-gaps not used ~~~")

    sim = report["simulated_exit"]
    actual = report["actual_exit_reason"]
    actual_r = report.get("actual_r")
    sim_r = report.get("simulated_r")

    actual_r_str = f"{actual_r:+.2f}R" if actual_r is not None else "n/a"
    print(f"\n  ACTUAL:    {actual or 'still open'} ({actual_r_str})")

    if sim:
        sim_r_str = f"{sim_r:+.2f}R" if sim_r is not None else "n/a"
        print(f"  SIMULATED: would have exited at {sim['ts'].strftime('%H:%M UTC')} "
              f"via {sim['reason']} ({sim_r_str}, source: {sim['source']})")

        # Verdict is driven by R comparison, not exit-reason type. A gap that
        # replaces a clean stop with a later time_stop can help OR hurt — the
        # sign and size of the R delta is what matters.
        if actual_r is not None and sim_r is not None:
            delta = actual_r - sim_r
            if abs(delta) < Decimal("0.15"):
                print(f"  [ok] Consistent -- gap did not materially change outcome "
                      f"(delta {delta:+.2f}R)")
            elif delta > 0:
                print(f"  [+] Gap HELPED: actual {actual_r:+.2f}R vs simulated "
                      f"{sim_r:+.2f}R (delta {delta:+.2f}R)")
            else:
                print(f"  [!] Gap HURT: actual {actual_r:+.2f}R vs simulated "
                      f"{sim_r:+.2f}R (delta {delta:+.2f}R) -- lost stop protection")
        elif actual == sim["reason"]:
            print(f"  [ok] Exit consistent -- gap did not affect outcome")
    else:
        print(f"  SIMULATED: no exit triggered in available bars")
        if actual in ("time_stop",):
            print(f"  [ok] Consistent with actual time_stop -- no early stop breach found")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Replay PositionMonitor decisions bar-by-bar")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--position-id", help="UUID of a specific position")
    group.add_argument("--session-id", help="UUID of a session (replays all positions)")
    parser.add_argument("--strategy", required=True, help="Path to strategy YAML")
    parser.add_argument("--fetch-gaps", action="store_true",
                        help="Backfill missing bars from Birdeye for gap windows")
    parser.add_argument("--confirmation-bars", type=int, default=None,
                        help="Override trailing_stop_confirmation_bars for what-if analysis")
    args = parser.parse_args()

    cfg: StrategyConfig = load_strategy_config(args.strategy)
    if args.confirmation_bars is not None:
        cfg.trailing_stop_confirmation_bars = args.confirmation_bars
        print(f"[override] trailing_stop_confirmation_bars={args.confirmation_bars}")

    database_url = os.environ.get("DATABASE_URL", "")
    conn = await asyncpg.connect(_dsn(database_url))

    adapter = None
    if args.fetch_gaps:
        api_key = os.environ.get("BIRDEYE_API_KEY")
        if not api_key:
            print("WARNING: --fetch-gaps requires BIRDEYE_API_KEY; gaps will not be filled")
        else:
            adapter = BirdeyeAdapter(api_key=api_key, chain="solana")

    position_rows = await _fetch_positions(
        conn,
        session_id=args.session_id,
        position_id=args.position_id,
    )

    if not position_rows:
        print("No positions found.")
        await conn.close()
        return

    for pos_row in position_rows:
        # Determine replay window: entry_time → close_time (from trades table)
        trades = await _fetch_trades(conn, pos_row["id"])
        if trades:
            replay_end = max(t["exit_time"] for t in trades)
            actual_exit_reason = trades[0]["exit_reason"]
            actual_r = trades[0].get("r_multiple")
        else:
            replay_end = datetime.now(tz=timezone.utc)
            actual_exit_reason = None
            actual_r = None

        bars = await _fetch_bars(conn, pos_row["pair_address"], pos_row["entry_time"], replay_end)

        # Gap detection
        gaps = _detect_gaps(bars)

        # Optionally fill gaps from Birdeye
        if gaps and adapter:
            all_bars = list(bars)
            for gap_start, gap_end in gaps:
                gap_mins = int((gap_end - gap_start).total_seconds() / 60)
                print(f"\n[gap fill] {pos_row['symbol']} — fetching {gap_mins} bars "
                      f"({gap_start.strftime('%H:%M')}–{gap_end.strftime('%H:%M')} UTC)")
                gap_bars = await _fill_gap(adapter, pos_row["pair_address"], gap_start, gap_end)
                print(f"[gap fill] got {len(gap_bars)} bars from Birdeye")
                all_bars.extend(gap_bars)
            # Re-sort by timestamp after inserting gap bars
            all_bars.sort(key=lambda r: r["timestamp"])
            bars = all_bars
            # Recompute gaps after fill (may still have holes if Birdeye lacked data)
            gaps = _detect_gaps([b for b in bars if b.get("source") != "birdeye_backfill"])

        report = await _replay(conn, pos_row, bars, copy.deepcopy(cfg), actual_exit_reason, actual_r)
        _print_report(report, gaps)

        if adapter:
            await asyncio.sleep(1.0)  # rate limit between positions

    await conn.close()
    if adapter:
        await adapter._session.close()


if __name__ == "__main__":
    asyncio.run(_main())
