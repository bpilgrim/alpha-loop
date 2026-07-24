"""RiskEngine — deterministic hard controls. No strategy logic lives here."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ..entities.market_bar import MarketBar
from ..entities.signal_candidate import SignalCandidate
from ..value_objects.risk_decision import RiskDecision

logger = logging.getLogger(__name__)

# From a prior live-trading system: max order = 2% of pool liquidity
_MAX_POOL_IMPACT_PCT = Decimal("0.02")


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: Decimal = Decimal("0.5")    # 0.5% of capital
    max_concurrent_positions: int = 10
    daily_max_drawdown_pct: Decimal = Decimal("5.0")    # halt session at 5% DD
    loss_streak_cooldown_minutes: int = 30
    loss_streak_count: int = 3
    stale_data_max_age_seconds: int = 180               # 3 minutes
    min_liquidity_hard_floor_usd: Decimal = Decimal("1000")   # from a prior live-trading system
    max_pool_impact_pct: Decimal = _MAX_POOL_IMPACT_PCT
    max_position_size_pct: Optional[Decimal] = None      # None = uncapped
    # Fibonacci profit targets (from a prior live-trading system)
    fibonacci_tp_multiples: list[Decimal] = None

    def __post_init__(self):
        if self.fibonacci_tp_multiples is None:
            self.fibonacci_tp_multiples = [
                Decimal("1.618"),
                Decimal("2.618"),
                Decimal("4.236"),
            ]


class RiskEngine:
    def __init__(self, config: RiskConfig, capital_usd: Decimal) -> None:
        self._config = config
        self._capital = capital_usd
        self._open_positions: int = 0
        self._daily_pnl: Decimal = Decimal("0")
        self._consecutive_losses: int = 0
        self._last_loss_time: Optional[datetime] = None
        self._kill_switch_active: bool = False

    def evaluate(
        self,
        signal: SignalCandidate,
        bar: MarketBar,
        stop_price: Decimal,
        latest_bar_time: datetime,
    ) -> RiskDecision:
        """Evaluate a signal candidate against all hard risk controls."""

        if self._kill_switch_active:
            return RiskDecision(approved=False, rejection_reasons=["kill_switch_active"])

        reasons = []

        # Stale data guard
        age_seconds = (datetime.now(tz=timezone.utc) - latest_bar_time).total_seconds()
        if age_seconds > self._config.stale_data_max_age_seconds:
            reasons.append(f"stale_data:{age_seconds:.0f}s")

        # Max concurrent positions
        if self._open_positions >= self._config.max_concurrent_positions:
            reasons.append("max_positions_reached")

        # Daily drawdown halt
        dd_pct = (self._daily_pnl / self._capital * Decimal("100")).copy_abs()
        if self._daily_pnl < 0 and dd_pct >= self._config.daily_max_drawdown_pct:
            reasons.append(f"daily_drawdown:{dd_pct:.2f}%")

        # Loss streak cooldown
        if (
            self._consecutive_losses >= self._config.loss_streak_count
            and self._last_loss_time is not None
        ):
            elapsed = (datetime.now(tz=timezone.utc) - self._last_loss_time).total_seconds() / 60
            if elapsed < self._config.loss_streak_cooldown_minutes:
                reasons.append(f"loss_streak_cooldown:{elapsed:.1f}min_remaining")

        # Hard liquidity floor (from a prior live-trading system)
        if bar.liquidity_usd < self._config.min_liquidity_hard_floor_usd:
            reasons.append(f"liquidity_below_floor:{bar.liquidity_usd}")

        # Token status gate
        if bar.token_status in ("pair_dead", "rugpull_flagged"):
            reasons.append(f"token_status:{bar.token_status}")

        if reasons:
            return RiskDecision(approved=False, rejection_reasons=reasons)

        # Position sizing: fixed fractional
        # size = (capital * risk_pct) / (entry - stop)
        entry = bar.close
        risk_per_unit = entry - stop_price
        if risk_per_unit <= 0:
            return RiskDecision(
                approved=False,
                rejection_reasons=["invalid_stop:stop_above_entry"],
            )

        risk_usd = self._capital * (self._config.max_risk_per_trade_pct / Decimal("100"))
        position_size_usd = risk_usd / (risk_per_unit / entry)

        # Cap at max_position_size_pct of capital (hard notional ceiling)
        if self._config.max_position_size_pct is not None:
            max_size_from_capital = self._capital * self._config.max_position_size_pct / Decimal("100")
            if position_size_usd > max_size_from_capital:
                logger.debug(
                    f"Position size {position_size_usd:.2f} capped to {max_size_from_capital:.2f} "
                    f"({self._config.max_position_size_pct}% of capital)"
                )
                position_size_usd = max_size_from_capital

        # Cap at 2% pool impact (from a prior live-trading system)
        max_size_from_liquidity = bar.liquidity_usd * self._config.max_pool_impact_pct / 2
        if position_size_usd > max_size_from_liquidity:
            logger.debug(
                f"Position size {position_size_usd} capped to {max_size_from_liquidity} "
                f"(2% pool impact on {bar.liquidity_usd} liquidity)"
            )
            position_size_usd = max_size_from_liquidity

        # Fibonacci take-profit levels
        tp_prices = [
            entry * multiple
            for multiple in self._config.fibonacci_tp_multiples
        ]

        return RiskDecision(
            approved=True,
            position_size_usd=position_size_usd.quantize(Decimal("0.01")),
            stop_price=stop_price,
            take_profit_prices=tp_prices,
        )

    def record_open(self) -> None:
        self._open_positions += 1

    def record_close(self, pnl_usd: Decimal) -> None:
        self._open_positions = max(0, self._open_positions - 1)
        self._daily_pnl += pnl_usd
        if pnl_usd < 0:
            self._consecutive_losses += 1
            self._last_loss_time = datetime.now(tz=timezone.utc)
        else:
            self._consecutive_losses = 0

    def activate_kill_switch(self, reason: str) -> None:
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
        self._kill_switch_active = True

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active
