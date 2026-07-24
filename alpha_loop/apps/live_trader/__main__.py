"""Live Trader — paper/live trading orchestrator for alpha-loop."""

import asyncio
import hashlib
import json
import logging
import os
import signal as _signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("live_trader")

app = typer.Typer()


@app.command()
def main(
    session: str = typer.Option(
        "alpha_loop/configs/live/session_paper.yaml",
        help="Path to session config YAML",
    ),
    dry_run: bool = typer.Option(
        False, help="Scan and evaluate signals but do not submit orders"
    ),
):
    """Run the alpha-loop paper/live trader."""
    asyncio.run(_run(session_path=session, dry_run=dry_run))


async def _run(session_path: str, dry_run: bool) -> None:
    from ...adapters.market_data.birdeye_adapter import BirdeyeAdapter
    from ...adapters.execution.paper_execution_adapter import PaperExecutionAdapter
    from ...adapters.persistence.postgres_trade_store import PostgresTradeStore
    from ...adapters.persistence.parquet_bar_store import ParquetBarStore
    from ...configs.loader import load_strategy_config, load_risk_config
    from ...domain.services.strategy_engine import StrategyEngine
    from ...domain.services.risk_engine import RiskEngine
    from ...domain.services.position_monitor import PositionMonitor
    from ...domain.services.feature_pipeline import compute_features

    # --- Load session config ---
    with open(session_path) as f:
        session_cfg = yaml.safe_load(f)

    strategy_yaml = session_cfg["strategy"]
    capital_usd = Decimal(str(session_cfg["capital_usd"]))
    chain = session_cfg.get("chain", "solana")
    poll_interval = int(session_cfg.get("poll_interval_seconds", 60))
    symbols: list[str] = session_cfg.get("symbols", [])
    mode = session_cfg.get("mode", "paper")
    discover_count = int(session_cfg.get("discover_tokens_count", 0))

    # --- Env vars ---
    api_key = os.environ.get("BIRDEYE_API_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not api_key:
        logger.error("BIRDEYE_API_KEY not set")
        sys.exit(1)
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    # --- Load strategy/risk config ---
    strategy_cfg = load_strategy_config(strategy_yaml, relative_to=session_path)
    risk_cfg = load_risk_config(strategy_yaml, relative_to=session_path)

    config_hash = _hash_file(strategy_yaml)

    # --- Wire up components ---
    market = BirdeyeAdapter(api_key=api_key, chain=chain)

    if not symbols and discover_count == 0:
        logger.error("No symbols in session config — add token addresses to symbols: [] or set discover_tokens_count")
        sys.exit(1)

    static_symbols: list[str] = symbols  # fixed anchors; discovery adds to these each tick

    if mode == "live":
        from ...adapters.execution.jupiter_execution_adapter import JupiterExecutionAdapter
        solana_key = os.environ.get("SOLANA_PRIVATE_KEY")
        solana_rpc = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        if not solana_key:
            logger.error("SOLANA_PRIVATE_KEY not set — required for live mode")
            sys.exit(1)
        execution = JupiterExecutionAdapter(
            private_key_b58=solana_key,
            rpc_url=solana_rpc,
        )
        logger.info("Live execution mode: Jupiter v6 | RPC=%s", solana_rpc)
        # Clear any kill switch state left over from a prior crashed session.
        # In production, review on-chain state before calling this.
        execution.reset_kill_switch()
    else:
        execution = PaperExecutionAdapter()
        logger.info("Paper execution mode")

    parquet_dir = Path(session_cfg.get("parquet_dir", "data/bars"))
    parquet_store = ParquetBarStore(base_dir=parquet_dir)

    store = PostgresTradeStore(dsn=database_url)
    await store.connect()

    strategy_engine = StrategyEngine(config=strategy_cfg)
    risk_engine = RiskEngine(config=risk_cfg, capital_usd=capital_usd)
    position_monitor = PositionMonitor(config=strategy_cfg)

    # --- Register session ---
    session_id = await store.write_strategy_run({
        "strategy_name": strategy_cfg.strategy_name,
        "strategy_version": strategy_cfg.version,
        "config_hash": config_hash,
        "mode": mode,
        "session_start": datetime.now(tz=timezone.utc),
        "capital_start": capital_usd,
    })
    logger.info(
        "Session %s started | strategy=%s mode=%s capital=$%.2f static_symbols=%d discover=%d",
        session_id, strategy_cfg.strategy_name, mode, capital_usd,
        len(static_symbols), discover_count,
    )
    if dry_run:
        logger.info("DRY RUN — orders will not be submitted")

    # In-memory position tracking (authoritative within session)
    open_positions: dict[str, "Position"] = {}  # pair_address → Position

    # --- Graceful shutdown ---
    shutdown = asyncio.Event()

    def _handle_signal(*_):
        logger.info("Shutdown signal received")
        shutdown.set()

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        _signal.signal(sig, _handle_signal)

    # --- Main loop ---
    try:
        while not shutdown.is_set():
            loop_start = datetime.now(tz=timezone.utc)

            # 0. Refresh discovered tokens each tick so new movers are picked up
            if discover_count > 0:
                discovered = await market.get_top_tokens(limit=discover_count)
                static_set = set(static_symbols)
                symbols = static_symbols + [t for t in discovered if t not in static_set]
                logger.info(
                    "Discovery: %d candidates → watchlist=%d (%d static + %d new)",
                    len(discovered), len(symbols),
                    len(static_symbols), len(symbols) - len(static_symbols),
                )

            # 1. Monitor open positions (exits take priority each tick)
            for pair_address in list(open_positions.keys()):
                pos = open_positions[pair_address]
                try:
                    bars = await market.get_bars(pair_address, "1m", 20)
                    if not bars:
                        logger.warning("No bars for open position %s", pair_address)
                        continue
                    parquet_store.write_bars(bars)
                    await store.write_bars(bars)
                    bars = compute_features(bars)
                    bar = bars[-1]

                    decision = position_monitor.evaluate(pos, bar)

                    if decision.should_exit:
                        if decision.is_partial:
                            await _partial_exit(
                                pos, decision, bar, execution, store,
                                session_id, dry_run,
                            )
                        else:
                            await _full_exit(
                                pos, decision, bar, execution, store,
                                risk_engine, session_id, dry_run,
                            )
                            del open_positions[pair_address]
                    else:
                        # Persist updated MAE/MFE/stop_price
                        await store.update_position(pos)

                except Exception as exc:
                    logger.error("Position monitor error for %s: %s", pair_address, exc, exc_info=True)

                await asyncio.sleep(1.0)  # rate limit between position polls

            # 2. Scan for new signals (skip pairs already in a position)
            open_count = len(open_positions)
            for pair_address in symbols:
                if pair_address in open_positions:
                    continue
                try:
                    bars = await market.get_bars(pair_address, "1m", 100)
                    if not bars:
                        continue
                    parquet_store.write_bars(bars)
                    await store.write_bars(bars)
                    token_status = await market.get_token_status(pair_address)
                    launch_time = await market.get_launch_time(pair_address)
                    for bar in bars:
                        bar.token_status = token_status
                        bar.launch_time = launch_time
                        if launch_time:
                            delta = (bar.timestamp - launch_time).total_seconds() / 60
                            bar.time_since_launch_minutes = Decimal(str(delta))
                    bars = compute_features(bars)
                    bar = bars[-1]

                    signal, stop_price = strategy_engine.evaluate(
                        bars, open_count, session_id
                    )
                    await store.write_signal_candidate(signal)

                    if signal.passed_filters and stop_price is not None:
                        decision = risk_engine.evaluate(
                            signal, bar, stop_price, bar.timestamp
                        )
                        if decision.approved and not dry_run:
                            pos = await _open_position(
                                signal, decision, bar, execution, store,
                                session_id, strategy_cfg,
                            )
                            open_positions[pair_address] = pos
                            risk_engine.record_open()
                            open_count += 1
                            logger.info(
                                "POSITION OPENED %s | size=$%.2f stop=%.8f",
                                pair_address, decision.position_size_usd, stop_price,
                            )
                        elif not decision.approved:
                            logger.info(
                                "Signal rejected by RiskEngine: %s",
                                decision.rejection_reasons,
                            )
                    else:
                        logger.debug(
                            "No signal %s: %s", pair_address, signal.rejection_reason
                        )

                except Exception as exc:
                    logger.error("Signal scan error for %s: %s", pair_address, exc, exc_info=True)

                await asyncio.sleep(1.0)  # rate limit between symbol scans

            # 3. Sleep until next poll
            elapsed = (datetime.now(tz=timezone.utc) - loop_start).total_seconds()
            sleep_for = max(0, poll_interval - elapsed)
            logger.info(
                "Tick done | open_positions=%d sleep=%.0fs",
                len(open_positions), sleep_for,
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.ensure_future(shutdown.wait())),
                    timeout=sleep_for,
                )
            except asyncio.TimeoutError:
                pass  # normal — time to tick again

    finally:
        await store.close()
        logger.info("Session %s ended", session_id)


# ---------------------------------------------------------------------------
# Position lifecycle helpers
# ---------------------------------------------------------------------------

async def _open_position(signal, decision, bar, execution, store, session_id, strategy_cfg):
    from ...domain.entities.position import Position, TakeProfitLevel
    from ...domain.entities.trade import Order, TradeEvent
    from uuid import uuid4

    execution_type = getattr(execution, "_execution_type", "paper")

    position = Position(
        session_id=session_id,
        symbol=bar.symbol,
        pair_address=bar.pair_address,
        chain=bar.chain,
        entry_time=datetime.now(tz=timezone.utc),
        entry_price=bar.close,
        position_size_usd=decision.position_size_usd,
        stop_price=decision.stop_price,
        initial_stop_price=decision.stop_price,
        take_profit_levels=[
            TakeProfitLevel(
                price=tp_price,
                portion_pct=portion,
            )
            for tp_price, portion in zip(
                decision.take_profit_prices,
                strategy_cfg.take_profit_portions,
            )
        ],
        strategy_name=signal.strategy_name,
        strategy_version=signal.strategy_version,
        feature_snapshot_id=signal.feature_snapshot_id,
        status="open",
    )

    # Convention: tx_hash carries the token mint address pre-execution for JupiterAdapter.
    # PaperExecutionAdapter ignores this field.
    order = Order(
        position_id=position.id,
        side="buy",
        requested_price=bar.close,
        size_usd=decision.position_size_usd,
        execution_type=execution_type,
        tx_hash=bar.pair_address,   # token mint; Jupiter adapter reads this pre-fill
    )
    filled_order = await execution.submit_entry(order)
    position.entry_price = filled_order.fill_price

    # Persist
    await store.write_position(position)
    await store.write_order(filled_order)
    await store.write_trade_event(TradeEvent(
        trade_id=None,
        event_type="entry",
        payload={
            "fill_price": str(filled_order.fill_price),
            "size_usd": str(decision.position_size_usd),
            "slippage_bps": str(filled_order.slippage_bps),
            "stop_price": str(decision.stop_price),
        },
    ))
    return position


async def _partial_exit(pos, decision, bar, execution, store, session_id, dry_run):
    from ...domain.entities.trade import Order, TradeEvent

    execution_type = getattr(execution, "_execution_type", "paper")
    exit_size_usd = pos.position_size_usd * decision.exit_portion
    order = Order(
        position_id=pos.id,
        side="sell",
        requested_price=bar.close,
        size_usd=exit_size_usd,
        execution_type=execution_type,
        tx_hash=pos.pair_address,   # token mint for JupiterAdapter
    )
    if not dry_run:
        filled = await execution.submit_exit(order, emergency=False)
        await store.write_order(filled)
        await store.write_trade_event(TradeEvent(
            event_type=decision.exit_reason,
            payload={
                "fill_price": str(filled.fill_price),
                "exit_portion": str(decision.exit_portion),
                "size_usd": str(exit_size_usd),
                "slippage_bps": str(filled.slippage_bps),
            },
        ))
    await store.update_position(pos)
    logger.info(
        "PARTIAL EXIT %s | reason=%s portion=%.0f%% price=%.8f",
        pos.pair_address, decision.exit_reason,
        float(decision.exit_portion) * 100, bar.close,
    )


async def _full_exit(pos, decision, bar, execution, store, risk_engine, session_id, dry_run):
    from ...domain.entities.trade import Order, Trade, TradeEvent

    execution_type = getattr(execution, "_execution_type", "paper")
    exit_size_usd = pos.position_size_usd * decision.exit_portion
    requested_price = decision.exit_price or bar.close
    order = Order(
        position_id=pos.id,
        side="sell",
        requested_price=requested_price,
        size_usd=exit_size_usd,
        execution_type=execution_type,
        tx_hash=pos.pair_address,   # token mint for JupiterAdapter
        fill_status="filled",
    )
    if not dry_run:
        filled = await execution.submit_exit(order, emergency=decision.emergency)
    else:
        filled = order
        filled.fill_price = requested_price

    exit_price = filled.fill_price or bar.close
    entry_price = pos.entry_price or bar.close
    now = datetime.now(tz=timezone.utc)

    pnl_pct = (exit_price - entry_price) / entry_price
    pnl_usd = pnl_pct * (pos.position_size_usd or Decimal("0"))
    initial_risk_usd = (
        (entry_price - pos.initial_stop_price) / entry_price * pos.position_size_usd
        if pos.initial_stop_price and pos.position_size_usd
        else Decimal("1")
    )
    r_multiple = pnl_usd / initial_risk_usd if initial_risk_usd != 0 else Decimal("0")
    hold_minutes = int((now - pos.entry_time).total_seconds() / 60) if pos.entry_time else 0

    trade = Trade(
        position_id=pos.id,
        session_id=session_id,
        symbol=pos.symbol,
        entry_time=pos.entry_time,
        exit_time=now,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl_usd=pnl_usd.quantize(Decimal("0.0001")),
        pnl_pct=pnl_pct.quantize(Decimal("0.000001")),
        r_multiple=r_multiple.quantize(Decimal("0.0001")),
        mae=pos.mae,
        mfe=pos.mfe,
        hold_time_minutes=hold_minutes,
        exit_reason=decision.exit_reason,
        strategy_version=pos.strategy_version,
    )

    if not dry_run:
        await store.write_order(filled)
        await store.close_position(pos.id, trade)
        await store.write_trade_event(TradeEvent(
            trade_id=trade.id,
            event_type="closed",
            payload={
                "exit_reason": decision.exit_reason,
                "fill_price": str(exit_price),
                "pnl_usd": str(pnl_usd),
                "r_multiple": str(r_multiple),
                "hold_minutes": hold_minutes,
            },
        ))

    risk_engine.record_close(pnl_usd)
    logger.info(
        "POSITION CLOSED %s | reason=%s pnl=$%.4f (%.2fR) hold=%dmin",
        pos.pair_address, decision.exit_reason,
        float(pnl_usd), float(r_multiple), hold_minutes,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _hash_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


if __name__ == "__main__":
    app()
