"""Trade Reviewer - compute session metrics and generate strategy hypothesis via LLM."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import typer
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trade_reviewer")

app = typer.Typer()


@app.command()
def main(
    session_id: str = typer.Option(..., help="UUID of the strategy_runs session to review"),
    n_extremes: int = typer.Option(5, help="Number of best/worst trades to include in LLM prompt"),
    no_llm: bool = typer.Option(False, help="Skip LLM call - only print metrics"),
    output_dir: str = typer.Option("research/hypotheses", help="Directory for hypothesis JSON files"),
):
    """Review a completed paper/live trading session and generate a strategy hypothesis."""
    asyncio.run(_review(
        session_id=UUID(session_id),
        n_extremes=n_extremes,
        call_llm=not no_llm,
        output_dir=Path(output_dir),
    ))


async def _review(
    session_id: UUID,
    n_extremes: int,
    call_llm: bool,
    output_dir: Path,
) -> None:
    from ...adapters.persistence.postgres_trade_store import PostgresTradeStore
    from ...adapters.llm.anthropic_adapter import AnthropicAdapter
    from ...domain.services.review_engine import compute_metrics
    from ...domain.value_objects.strategy_hypothesis import StrategyHypothesis

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    store = PostgresTradeStore(dsn=database_url)
    await store.connect()

    try:
        # --- Load session data ---
        session = await store.get_session(session_id)
        trades = await store.get_session_trades(session_id)
        signals = await store.get_session_signals(session_id)

        logger.info(
            "Session %s | strategy=%s mode=%s | trades=%d signals=%d",
            session_id, session.get("strategy_name"), session.get("mode"),
            len(trades), len(signals),
        )

        if not trades and not signals:
            logger.warning("No data found for session %s - nothing to review", session_id)
            return

        # --- Compute metrics ---
        metrics = compute_metrics(trades, signals, n_extremes=n_extremes)
        metrics["session"] = {
            "session_id": str(session_id),
            "strategy_name": session.get("strategy_name"),
            "strategy_version": session.get("strategy_version"),
            "mode": session.get("mode"),
            "session_start": session["session_start"].isoformat() if session.get("session_start") else None,
            "capital_start": str(session.get("capital_start")),
        }

        _print_metrics(metrics)

        if not call_llm:
            logger.info("--no-llm flag set; skipping hypothesis generation")
            return

        # --- Call LLM ---
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_key:
            logger.error("ANTHROPIC_API_KEY not set - cannot call LLM")
            sys.exit(1)

        llm = AnthropicAdapter(api_key=anthropic_key)
        raw_hypothesis = await llm.review_session(metrics)
        hypothesis = StrategyHypothesis.from_dict(raw_hypothesis)

        logger.info("Hypothesis generated: %s", hypothesis.hypothesis)
        logger.info("Change type: %s | Expected effect: %s", hypothesis.change_type, hypothesis.expected_effect)

        # --- Persist to DB ---
        review_record = {
            "session_id": session_id,
            "review_version": 1,
            "reviewed_at": datetime.now(tz=timezone.utc),
            **hypothesis.to_dict(),
        }
        review_id = await store.write_trade_review(review_record)
        logger.info("Trade review written to DB: %s", review_id)

        # --- Write JSON file ---
        output_dir.mkdir(parents=True, exist_ok=True)
        strategy_name = session.get("strategy_name", "unknown")
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        filename = output_dir / f"{date_str}-{strategy_name}-hypothesis.json"

        output = {
            "review_id": str(review_id),
            "session_id": str(session_id),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "metrics_summary": {
                "trades": metrics["trade_stats"],
                "signals": metrics["signal_stats"],
                "r_distribution": metrics["r_distribution"],
            },
            "hypothesis": hypothesis.to_dict(),
        }
        filename.write_text(json.dumps(output, indent=2))
        logger.info("Hypothesis saved to %s", filename)

    finally:
        await store.close()


def _print_metrics(metrics: dict) -> None:
    """Print a human-readable metrics summary to stdout."""
    ss = metrics.get("signal_stats", {})
    ts = metrics.get("trade_stats", {})
    rd = metrics.get("r_distribution", {})
    mm = metrics.get("mae_mfe", {})
    eb = metrics.get("exit_breakdown", {})
    ht = metrics.get("hold_time", {})

    print("\n" + "=" * 60)
    print("SESSION REVIEW")
    sess = metrics.get("session", {})
    print(f"  strategy : {sess.get('strategy_name')} v{sess.get('strategy_version')}")
    print(f"  mode     : {sess.get('mode')}")
    print(f"  session  : {sess.get('session_id')}")

    print("\n-- Signal Funnel ------------------------------------------")
    print(f"  total   : {ss.get('total')}")
    print(f"  passed  : {ss.get('passed')} ({ss.get('pass_rate_pct')}%)")
    for reason, count in ss.get("rejection_breakdown", {}).items():
        print(f"    {reason:<35} {count}")

    print("\n-- Trade Performance --------------------------------------")
    print(f"  trades  : {ts.get('total')}  (wins {ts.get('wins')} / losses {ts.get('losses')})")
    print(f"  win rate: {ts.get('win_rate_pct')}%")
    print(f"  avg win : ${ts.get('avg_win_usd'):.4f}  avg loss: ${ts.get('avg_loss_usd'):.4f}")
    print(f"  expect  : ${ts.get('expectancy_usd'):.4f}/trade")
    print(f"  total   : ${ts.get('total_pnl_usd'):.4f}")

    print("\n-- R-Multiple ---------------------------------------------")
    print(f"  avg {rd.get('avg')}  median {rd.get('median')}  best {rd.get('best')}  worst {rd.get('worst')}")
    print(f"  positive R: {rd.get('positive_pct')}%")

    print("\n-- Stop Quality (winners only) ----------------------------")
    print(f"  MAE/MFE ratio: {mm.get('avg_mae_mfe_ratio')}  (lower = better)")
    print(f"  avg MAE: {mm.get('avg_mae_pct')}%  avg MFE: {mm.get('avg_mfe_pct')}%")

    print("\n-- Exit Reasons -------------------------------------------")
    for reason, stats in eb.items():
        print(f"  {reason:<20} {stats['count']:>3} ({stats['pct']}%)  winrate {stats['win_rate_pct']}%")

    print("\n-- Hold Time ----------------------------------------------")
    print(f"  avg {ht.get('avg_minutes')}min  median {ht.get('median_minutes')}min  range {ht.get('min_minutes')}-{ht.get('max_minutes')}min")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    app()
