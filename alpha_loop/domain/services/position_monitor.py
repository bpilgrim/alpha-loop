"""PositionMonitor — bar-by-bar exit evaluation for open positions."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ..entities.market_bar import MarketBar
from ..entities.position import Position, TakeProfitLevel
from ..value_objects.risk_decision import ExitDecision
from .strategy_engine import StrategyConfig

logger = logging.getLogger(__name__)

_LIQUIDITY_EMERGENCY_FLOOR = Decimal("1000")


class PositionMonitor:
    """
    Pure domain logic — no I/O, no async.

    evaluate() is called once per completed bar for each open position.
    It also updates position.mae, position.mfe, and position.stop_price in-place
    (caller is responsible for persisting the updated position).

    Exit priority order:
      1. Liquidity emergency (immediate full exit)
      2. Hard stop (initial or trailed stop_price)
      3. Take-profit partials (highest level first that hasn't triggered)
      4. Trailing stop (once activated)
      5. Time stop
    """

    def __init__(self, config: StrategyConfig) -> None:
        self._cfg = config

    def evaluate(
        self,
        position: Position,
        bar: MarketBar,
        now: Optional[datetime] = None,
    ) -> ExitDecision:
        if position.entry_price is None or position.entry_time is None:
            return ExitDecision(should_exit=False)

        if now is None:
            now = datetime.now(tz=timezone.utc)

        price = bar.close

        # Update MAE / MFE in-place
        self._update_excursion(position, bar)

        # 1. Liquidity emergency
        if self._check_liquidity_emergency(bar):
            logger.warning(
                "EMERGENCY EXIT %s — token_status=%s liquidity=%.0f",
                position.pair_address, bar.token_status, bar.liquidity_usd,
            )
            return ExitDecision(
                should_exit=True,
                exit_reason="liquidity_emergency",
                exit_portion=Decimal("1.0"),
                is_partial=False,
                emergency=True,
            )

        # 2. Trailing stop — owns all exit decisions once the trail is active.
        #    Requires confirmation_bars consecutive breaches before exiting, unless
        #    price drops hard_floor_atr × ATR below the trail (black swan bypass).
        trailing = self._evaluate_trailing_stop(position, bar)
        if trailing is not None and trailing.should_exit:
            return trailing

        # 3. Hard stop — initial stop only; never fires once trailing is active
        #    (trailing stop owns that territory exclusively via step 2 above).
        if position.stop_price is not None and bar.low <= position.stop_price:
            trailing_is_active = (
                position.initial_stop_price is not None
                and position.stop_price > position.initial_stop_price
            )
            if not trailing_is_active:
                logger.info(
                    "STOP %s — low=%.8f stop=%.8f",
                    position.pair_address, bar.low, position.stop_price,
                )
                return ExitDecision(
                    should_exit=True,
                    exit_reason="stop",
                    exit_portion=self._remaining_portion(position),
                    is_partial=False,
                    exit_price=position.stop_price,
                )

        # 4. Take-profit partials
        tp_decision = self._check_take_profits(position, price)
        if tp_decision is not None:
            return tp_decision

        # 5. Time stop
        elapsed_minutes = (now - position.entry_time).total_seconds() / 60
        if elapsed_minutes >= self._cfg.time_stop_minutes:
            logger.info(
                "TIME STOP %s — held %.1f minutes",
                position.pair_address, elapsed_minutes,
            )
            return ExitDecision(
                should_exit=True,
                exit_reason="time_stop",
                exit_portion=self._remaining_portion(position),
                is_partial=False,
            )

        return ExitDecision(should_exit=False)

    # ------------------------------------------------------------------
    # MAE / MFE
    # ------------------------------------------------------------------

    def _update_excursion(self, position: Position, bar: MarketBar) -> None:
        entry = position.entry_price
        # Adverse: how far low went below entry (negative value = worse)
        adverse = bar.low - entry
        if position.mae is None or adverse < position.mae:
            position.mae = adverse

        # Favorable: how far high went above entry (positive = better)
        favorable = bar.high - entry
        if position.mfe is None or favorable > position.mfe:
            position.mfe = favorable

    # ------------------------------------------------------------------
    # Liquidity emergency
    # ------------------------------------------------------------------

    def _check_liquidity_emergency(self, bar: MarketBar) -> bool:
        if bar.token_status in ("pair_dead", "rugpull_flagged"):
            return True
        if bar.liquidity_usd < _LIQUIDITY_EMERGENCY_FLOOR:
            return True
        return False

    # ------------------------------------------------------------------
    # Take-profit partials
    # ------------------------------------------------------------------

    def _check_take_profits(
        self, position: Position, price: Decimal
    ) -> Optional[ExitDecision]:
        for i, tp in enumerate(position.take_profit_levels):
            if tp.triggered:
                continue
            if price >= tp.price:
                tp.triggered = True
                remaining_before = self._remaining_portion(position) + tp.portion_pct
                logger.info(
                    "TAKE PROFIT %d %s — close=%.8f target=%.8f portion=%.0f%%",
                    i + 1, position.pair_address, price, tp.price,
                    float(tp.portion_pct) * 100,
                )
                # Check if all TPs are now triggered (last one closes fully)
                all_triggered = all(t.triggered for t in position.take_profit_levels)
                return ExitDecision(
                    should_exit=True,
                    exit_reason=f"take_profit_{i + 1}",
                    exit_portion=tp.portion_pct,
                    is_partial=not all_triggered,
                )
        return None

    # ------------------------------------------------------------------
    # Trailing stop
    # ------------------------------------------------------------------

    def _evaluate_trailing_stop(
        self,
        position: Position,
        bar: MarketBar,
    ) -> Optional[ExitDecision]:
        if not self._cfg.trailing_stop_enabled:
            return None
        if bar.atr_14 is None:
            return None
        if position.entry_price is None or position.stop_price is None:
            return None
        atr = bar.atr_14
        price = bar.close

        initial_risk = position.entry_price - position.stop_price
        if initial_risk <= 0:
            return None

        # Activation threshold: price >= entry + (activate_r × initial_risk)
        activation_price = (
            position.entry_price + self._cfg.trailing_activates_after_r * initial_risk
        )
        if price < activation_price:
            return None  # trailing stop not yet active

        # Multiplier scales linearly from min to max based on unrealized R
        unrealized_r = (price - position.entry_price) / initial_risk
        t = min(
            (unrealized_r - self._cfg.trailing_activates_after_r)
            / self._cfg.trailing_activates_after_r,
            Decimal("1.0"),
        )
        t = max(t, Decimal("0.0"))
        multiplier = (
            self._cfg.trailing_atr_multiplier_min
            + t * (self._cfg.trailing_atr_multiplier_max - self._cfg.trailing_atr_multiplier_min)
        )

        # Candidate trailing stop
        candidate = price - atr * multiplier

        # Apply floor: never below entry × (1 - floor_pct)
        floor_price = position.entry_price * (1 - self._cfg.trailing_stop_floor_pct)
        candidate = max(candidate, floor_price)

        # Only ratchet up — never move stop down
        if candidate > position.stop_price:
            logger.debug(
                "Trailing stop raised %s: %.8f → %.8f (mult=%.2f)",
                position.pair_address, position.stop_price, candidate, multiplier,
            )
            position.stop_price = candidate

        # Check if bar.low has fallen through the (possibly just-updated) trailing stop
        if bar.low <= position.stop_price:
            depth = position.stop_price - bar.low

            # Black swan bypass: price dropped hard_floor_atr × ATR below the trail —
            # skip confirmation and exit immediately regardless of breach count.
            if depth >= self._cfg.trailing_stop_hard_floor_atr * atr:
                logger.info(
                    "TRAILING STOP (floor breach) %s — low=%.8f stop=%.8f depth=%.2f ATR",
                    position.pair_address, bar.low, position.stop_price,
                    float(depth / atr),
                )
                position.trailing_breach_count = 0
                return ExitDecision(
                    should_exit=True,
                    exit_reason="trailing_stop",
                    exit_portion=self._remaining_portion(position),
                    is_partial=False,
                    exit_price=position.stop_price,
                )

            # Confirmation mode: accumulate consecutive breach bars.
            position.trailing_breach_count += 1
            if position.trailing_breach_count >= self._cfg.trailing_stop_confirmation_bars:
                logger.info(
                    "TRAILING STOP %s — low=%.8f stop=%.8f (confirmed %d/%d bars)",
                    position.pair_address, bar.low, position.stop_price,
                    position.trailing_breach_count,
                    self._cfg.trailing_stop_confirmation_bars,
                )
                position.trailing_breach_count = 0
                return ExitDecision(
                    should_exit=True,
                    exit_reason="trailing_stop",
                    exit_portion=self._remaining_portion(position),
                    is_partial=False,
                    exit_price=position.stop_price,
                )

            logger.debug(
                "TRAILING STOP %s breach %d/%d — holding for confirmation",
                position.pair_address,
                position.trailing_breach_count,
                self._cfg.trailing_stop_confirmation_bars,
            )
            return ExitDecision(should_exit=False)

        # Bar closed above trailing stop — reset breach counter
        position.trailing_breach_count = 0
        return ExitDecision(should_exit=False)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _remaining_portion(position: Position) -> Decimal:
        triggered_sum = sum(
            tp.portion_pct for tp in position.take_profit_levels if tp.triggered
        )
        return Decimal("1.0") - triggered_sum
