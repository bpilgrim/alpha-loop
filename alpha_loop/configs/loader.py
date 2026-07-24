"""Config loader — reads YAML strategy and session configs."""

from pathlib import Path

import yaml

from ..domain.services.strategy_engine import StrategyConfig
from ..domain.services.risk_engine import RiskConfig
from decimal import Decimal


def _resolve(path: str | Path, relative_to: str | Path | None = None) -> Path:
    """Resolve a path, trying CWD first, then relative to a base file if given."""
    p = Path(path)
    if p.is_absolute():
        return p
    if p.exists():
        return p  # CWD-relative and exists — use as-is
    if relative_to is not None:
        candidate = Path(relative_to).parent / p
        if candidate.exists():
            return candidate
    return p  # return as-is; caller will get a clear FileNotFoundError


def load_strategy_config(path: str | Path, relative_to: str | Path | None = None) -> StrategyConfig:
    resolved = _resolve(path, relative_to)
    with open(resolved) as f:
        d = yaml.safe_load(f)
    return StrategyConfig.from_dict(d)


def load_risk_config(path: str | Path, relative_to: str | Path | None = None) -> RiskConfig:
    """Pull risk parameters from a strategy YAML into a RiskConfig."""
    resolved = _resolve(path, relative_to)
    with open(resolved) as f:
        d = yaml.safe_load(f)
    risk = d.get("risk", {})
    exit_ = d.get("exit", {})
    return RiskConfig(
        max_risk_per_trade_pct=Decimal(str(risk.get("max_risk_per_trade_pct", "0.5"))),
        max_concurrent_positions=risk.get("max_concurrent_positions", 5),
        daily_max_drawdown_pct=Decimal(str(risk.get("daily_max_drawdown_pct", "5.0"))),
        loss_streak_cooldown_minutes=risk.get("loss_streak_cooldown_minutes", 30),
        loss_streak_count=risk.get("loss_streak_count", 3),
        stale_data_max_age_seconds=risk.get("stale_data_max_age_seconds", 180),
        min_liquidity_hard_floor_usd=Decimal(str(risk.get("min_liquidity_hard_floor_usd", "1000"))),
        max_position_size_pct=Decimal(str(risk["max_position_size_pct"])) if "max_position_size_pct" in risk else None,
        fibonacci_tp_multiples=[
            Decimal(str(m)) for m in exit_.get("take_profit_multiples", [1.618, 2.618, 4.236])
        ],
    )
