"""ReviewEngine — deterministic metrics from session trades and signals.

Pure domain logic: no I/O, no async. Receives lists of Trade and SignalCandidate
objects, returns a SessionMetrics dict suitable for LLM prompt building.
"""

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Optional

from ..entities.signal_candidate import SignalCandidate
from ..entities.trade import Trade


def compute_metrics(
    trades: list[Trade],
    signals: list[SignalCandidate],
    n_extremes: int = 5,
) -> dict:
    """
    Compute all FR-55 metrics from a session's trade and signal data.

    Returns a dict with:
      signal_stats, trade_stats, r_distribution, mae_mfe, exit_breakdown,
      hold_time, best_trades, worst_trades
    """
    signal_stats = _signal_stats(signals)
    trade_stats = _trade_stats(trades)
    r_dist = _r_distribution(trades)
    mae_mfe = _mae_mfe_stats(trades)
    exit_breakdown = _exit_breakdown(trades)
    hold_time = _hold_time_stats(trades)
    best, worst = _extremes(trades, n=n_extremes)

    return {
        "signal_stats": signal_stats,
        "trade_stats": trade_stats,
        "r_distribution": r_dist,
        "mae_mfe": mae_mfe,
        "exit_breakdown": exit_breakdown,
        "hold_time": hold_time,
        "best_trades": best,
        "worst_trades": worst,
    }


# ---------------------------------------------------------------------------
# Signal statistics
# ---------------------------------------------------------------------------

def _signal_stats(signals: list[SignalCandidate]) -> dict:
    total = len(signals)
    passed = sum(1 for s in signals if s.passed_filters)
    rejected = total - passed

    rejection_counts: Counter = Counter()
    for s in signals:
        if not s.passed_filters and s.rejection_reason:
            # Bucket by first colon-delimited token (e.g. "no_breakout:..." → "no_breakout")
            key = s.rejection_reason.split(":")[0]
            rejection_counts[key] += 1

    return {
        "total": total,
        "passed": passed,
        "rejected": rejected,
        "pass_rate_pct": round(passed / total * 100, 1) if total else 0.0,
        "rejection_breakdown": dict(rejection_counts.most_common()),
    }


# ---------------------------------------------------------------------------
# Trade statistics
# ---------------------------------------------------------------------------

def _trade_stats(trades: list[Trade]) -> dict:
    if not trades:
        return {
            "total": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0,
            "avg_win_usd": 0.0, "avg_loss_usd": 0.0, "expectancy_usd": 0.0,
            "total_pnl_usd": 0.0,
        }

    wins = [t for t in trades if t.pnl_usd is not None and t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd is not None and t.pnl_usd <= 0]
    win_rate = len(wins) / len(trades)

    avg_win = float(sum(t.pnl_usd for t in wins) / len(wins)) if wins else 0.0
    avg_loss = float(sum(t.pnl_usd for t in losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    total_pnl = float(sum(t.pnl_usd for t in trades if t.pnl_usd))

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win_usd": round(avg_win, 4),
        "avg_loss_usd": round(avg_loss, 4),
        "expectancy_usd": round(expectancy, 4),
        "total_pnl_usd": round(total_pnl, 4),
    }


# ---------------------------------------------------------------------------
# R-multiple distribution
# ---------------------------------------------------------------------------

def _r_distribution(trades: list[Trade]) -> dict:
    rs = [float(t.r_multiple) for t in trades if t.r_multiple is not None]
    if not rs:
        return {"avg": None, "median": None, "best": None, "worst": None, "positive_pct": 0.0}

    rs_sorted = sorted(rs)
    n = len(rs_sorted)
    median = (rs_sorted[n // 2 - 1] + rs_sorted[n // 2]) / 2 if n % 2 == 0 else rs_sorted[n // 2]

    return {
        "avg": round(sum(rs) / n, 3),
        "median": round(median, 3),
        "best": round(max(rs), 3),
        "worst": round(min(rs), 3),
        "positive_pct": round(sum(1 for r in rs if r > 0) / n * 100, 1),
    }


# ---------------------------------------------------------------------------
# MAE / MFE (stop quality)
# ---------------------------------------------------------------------------

def _mae_mfe_stats(trades: list[Trade]) -> dict:
    """
    MAE/MFE ratio: how far price went against us vs. how far it went for us.
    A ratio near 0 means stops are placed well (little adverse excursion on winners).
    Computed on winning trades only — losing trades' MAE is expected.
    """
    winners_with_data = [
        t for t in trades
        if t.pnl_usd and t.pnl_usd > 0 and t.mae is not None and t.mfe is not None and t.mfe != 0
    ]
    if not winners_with_data:
        return {"avg_mae_mfe_ratio": None, "avg_mae_pct": None, "avg_mfe_pct": None}

    ratios = [abs(float(t.mae)) / float(t.mfe) for t in winners_with_data]
    mae_pcts = [float(t.mae) * 100 for t in winners_with_data]
    mfe_pcts = [float(t.mfe) * 100 for t in winners_with_data]

    return {
        "avg_mae_mfe_ratio": round(sum(ratios) / len(ratios), 3),
        "avg_mae_pct": round(sum(mae_pcts) / len(mae_pcts), 3),
        "avg_mfe_pct": round(sum(mfe_pcts) / len(mfe_pcts), 3),
    }


# ---------------------------------------------------------------------------
# Exit reason breakdown
# ---------------------------------------------------------------------------

def _exit_breakdown(trades: list[Trade]) -> dict:
    counts: Counter = Counter(t.exit_reason for t in trades if t.exit_reason)
    total = len(trades)
    breakdown = {}
    for reason, count in counts.most_common():
        wins = sum(1 for t in trades if t.exit_reason == reason and t.pnl_usd and t.pnl_usd > 0)
        breakdown[reason] = {
            "count": count,
            "pct": round(count / total * 100, 1) if total else 0.0,
            "win_rate_pct": round(wins / count * 100, 1) if count else 0.0,
        }
    return breakdown


# ---------------------------------------------------------------------------
# Hold time
# ---------------------------------------------------------------------------

def _hold_time_stats(trades: list[Trade]) -> dict:
    hold_times = [t.hold_time_minutes for t in trades if t.hold_time_minutes is not None]
    if not hold_times:
        return {"avg_minutes": None, "min_minutes": None, "max_minutes": None, "median_minutes": None}

    s = sorted(hold_times)
    n = len(s)
    median = (s[n // 2 - 1] + s[n // 2]) / 2 if n % 2 == 0 else s[n // 2]

    return {
        "avg_minutes": round(sum(hold_times) / n, 1),
        "min_minutes": min(hold_times),
        "max_minutes": max(hold_times),
        "median_minutes": round(median, 1),
    }


# ---------------------------------------------------------------------------
# Best / worst trades for LLM context
# ---------------------------------------------------------------------------

def _extremes(trades: list[Trade], n: int) -> tuple[list[dict], list[dict]]:
    with_r = [t for t in trades if t.r_multiple is not None]
    by_r = sorted(with_r, key=lambda t: float(t.r_multiple), reverse=True)
    best = [_trade_summary(t) for t in by_r[:n]]
    worst = [_trade_summary(t) for t in by_r[-n:]]
    return best, worst


def _trade_summary(t: Trade) -> dict:
    return {
        "symbol": t.symbol,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "entry_price": str(t.entry_price) if t.entry_price else None,
        "exit_price": str(t.exit_price) if t.exit_price else None,
        "pnl_usd": float(t.pnl_usd) if t.pnl_usd else None,
        "r_multiple": float(t.r_multiple) if t.r_multiple else None,
        "mae_pct": round(float(t.mae) * 100, 3) if t.mae else None,
        "mfe_pct": round(float(t.mfe) * 100, 3) if t.mfe else None,
        "hold_time_minutes": t.hold_time_minutes,
        "exit_reason": t.exit_reason,
    }
