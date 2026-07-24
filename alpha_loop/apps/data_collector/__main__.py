"""Data Collector — pulls OHLCV from Birdeye, normalizes, stores to PostgreSQL."""

import asyncio
import logging
import os
from datetime import datetime, timezone

import typer
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("data_collector")

app = typer.Typer()


@app.command()
def main(
    chain: str = typer.Option("solana", help="Chain: solana | bsc | eth | base"),
    symbol: str = typer.Option(None, help="Specific pair address (omit to use session config)"),
    timeframe: str = typer.Option("1m", help="Candle timeframe: 1m | 5m | 15m | 1h"),
    limit: int = typer.Option(500, help="Bars to fetch per symbol"),
):
    """Pull OHLCV data from Birdeye and store to database."""
    asyncio.run(_collect(chain=chain, symbol=symbol, timeframe=timeframe, limit=limit))


async def _collect(chain: str, symbol: str | None, timeframe: str, limit: int) -> None:
    from ...adapters.market_data.birdeye_adapter import BirdeyeAdapter
    from ...adapters.persistence.postgres_trade_store import PostgresTradeStore
    from ...domain.services.feature_pipeline import compute_features

    api_key = os.environ.get("BIRDEYE_API_KEY")
    if not api_key:
        logger.error("BIRDEYE_API_KEY not set in environment")
        raise SystemExit(1)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set in environment")
        raise SystemExit(1)

    adapter = BirdeyeAdapter(api_key=api_key, chain=chain)
    store = PostgresTradeStore(dsn=database_url)
    await store.connect()

    symbols = [symbol] if symbol else _load_symbols_from_config()
    if not symbols:
        logger.error("No symbols to collect. Pass --symbol or configure session_paper.yaml")
        await store.close()
        raise SystemExit(1)

    try:
        for pair_address in symbols:
            logger.info(f"Collecting {pair_address} [{timeframe}] x{limit} bars")
            try:
                bars = await adapter.get_bars(pair_address, timeframe, limit)
                if not bars:
                    logger.warning(f"No bars returned for {pair_address}")
                    continue

                # Enrich with token status and launch time (overview cached in adapter)
                token_status = await adapter.get_token_status(pair_address)
                launch_time = await adapter.get_launch_time(pair_address)
                for bar in bars:
                    bar.token_status = token_status
                    bar.launch_time = launch_time
                    if launch_time:
                        delta = (bar.timestamp - launch_time).total_seconds() / 60
                        bar.time_since_launch_minutes = delta  # type: ignore

                # Compute features
                bars = compute_features(bars)

                # Persist
                await store.write_bars(bars)
                logger.info(
                    f"  Stored {len(bars)} bars — token_status={token_status}, "
                    f"liquidity=${bars[-1].liquidity_usd:,.0f}"
                )

            except Exception as exc:
                logger.error(f"Failed to collect {pair_address}: {exc}", exc_info=True)
    finally:
        await store.close()


def _load_symbols_from_config() -> list[str]:
    import yaml
    config_path = "alpha_loop/configs/live/session_paper.yaml"
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("symbols", [])
    except Exception:
        return []


if __name__ == "__main__":
    app()
