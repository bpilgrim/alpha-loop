"""StrategyEngine — setup detection and filter chain (public shell).

This is the deterministic decision core of the live loop: given a completed bar
series (features already computed), it detects whether a configured setup is
present, runs the filter chain, computes an initial stop, and returns a
SignalCandidate — including the *rejected* candidates, which are journaled for
the research loop.

Public repository note
----------------------
The concrete setup-detection internals (the exact conditions that define a
tradeable pattern) and the production-tuned parameter values are **withheld**
from this public repository — see the README → "What's in this repository".
What remains here is the real architecture: the config-driven strategy surface,
the evaluate() orchestration, the filter chain, ATR-based stop placement, and
the signal/rejection journaling contract.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional
from uuid import UUID

from ..entities.market_bar import MarketBar
from ..entities.signal_candidate import SignalCandidate

logger = logging.getLogger(__name__)

# NOTE: The default values below are ILLUSTRATIVE PLACEHOLDERS, not the
# production-tuned strategy parameters. Strategies are defined declaratively in
# configs/strategies/*.yaml and loaded via StrategyConfig.from_dict(); the tuned
# configs are kept private. See configs/strategies/example_strategy.yaml.


@dataclass
class StrategyConfig:
    strategy_name: str = "example_strategy"
    version: str = "0.0.0"
    setup_type: str = "breakout_reclaim"    # breakout_reclaim | momentum_ignition

    # Setup detection — breakout_reclaim (placeholder defaults)
    lookback_bars: int = 20
    breakout_threshold_pct: Decimal = Decimal("0.005")

    # Setup detection — momentum_ignition (placeholder defaults)
    surge_bars: int = 5                                 # bars to measure the surge over
    min_surge_pct: Decimal = Decimal("0.03")            # minimum price increase across surge_bars
    min_volume_multiple: Decimal = Decimal("3.0")       # surge volume vs baseline multiple
    min_consecutive_green: int = 3                      # consecutive green bars in surge window
    max_extension_pct: Decimal = Decimal("0.15")        # reject if already too far above recent low

    # Filters
    min_liquidity_usd: Decimal = Decimal("50000")
    max_spread_bps: Decimal = Decimal("100")
    min_relative_volume: Decimal = Decimal("1.5")
    regime_allowed: list[str] = field(default_factory=lambda: ["medium", "high"])
    lifecycle_regime_allowed: list[str] = field(default_factory=list)  # empty = allow all
    min_time_since_launch_minutes: int = 60
    max_concurrent_positions: int = 5
    min_trades_last_hour: int = 60

    # Entry
    confirmation_bars: int = 1

    # Stop (initial)
    atr_multiple: Decimal = Decimal("1.5")

    # Exit — take-profit (Fibonacci extension targets)
    take_profit_multiples: list[Decimal] = field(
        default_factory=lambda: [Decimal("1.618"), Decimal("2.618"), Decimal("4.236")]
    )
    take_profit_portions: list[Decimal] = field(
        default_factory=lambda: [Decimal("0.50"), Decimal("0.30"), Decimal("0.20")]
    )

    # Exit — trailing stop
    trailing_stop_enabled: bool = True
    trailing_atr_multiplier_min: Decimal = Decimal("4.0")
    trailing_atr_multiplier_max: Decimal = Decimal("6.0")
    trailing_stop_floor_pct: Decimal = Decimal("0.04")
    trailing_activates_after_r: Decimal = Decimal("1.0")
    trailing_stop_confirmation_bars: int = 1        # consecutive breaches required before exit (1 = immediate)
    trailing_stop_hard_floor_atr: Decimal = Decimal("3.0")  # bypass confirmation if price drops this many ATRs below trailing stop

    # Exit — time stop
    time_stop_minutes: int = 90

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        setup = d.get("setup", {})
        filters = d.get("filters", {})
        entry = d.get("entry", {})
        risk = d.get("risk", {})
        exit_ = d.get("exit", {})
        return cls(
            strategy_name=d.get("strategy_name", "example_strategy"),
            version=d.get("version", "0.0.0"),
            setup_type=setup.get("type", "breakout_reclaim"),
            lookback_bars=setup.get("lookback_bars", 20),
            breakout_threshold_pct=Decimal(str(setup.get("breakout_threshold_pct", "0.005"))),
            surge_bars=setup.get("surge_bars", 5),
            min_surge_pct=Decimal(str(setup.get("min_surge_pct", "0.03"))),
            min_volume_multiple=Decimal(str(setup.get("min_volume_multiple", "3.0"))),
            min_consecutive_green=setup.get("min_consecutive_green", 3),
            max_extension_pct=Decimal(str(setup.get("max_extension_pct", "0.15"))),
            min_liquidity_usd=Decimal(str(filters.get("min_liquidity_usd", "50000"))),
            max_spread_bps=Decimal(str(filters.get("max_spread_bps", "100"))),
            min_relative_volume=Decimal(str(filters.get("min_relative_volume", "1.5"))),
            regime_allowed=filters.get("regime_allowed", ["medium", "high"]),
            lifecycle_regime_allowed=filters.get("lifecycle_regime_allowed", []),
            min_time_since_launch_minutes=filters.get("min_time_since_launch_minutes", 60),
            max_concurrent_positions=filters.get("max_concurrent_positions", 5),
            min_trades_last_hour=filters.get("min_trades_last_hour", 60),
            confirmation_bars=entry.get("confirmation_bars", 1),
            atr_multiple=Decimal(str(risk.get("atr_multiple", "1.5"))),
            take_profit_multiples=[
                Decimal(str(m))
                for m in exit_.get("take_profit_multiples", [1.618, 2.618, 4.236])
            ],
            take_profit_portions=[
                Decimal(str(p))
                for p in exit_.get("take_profit_portions", [0.50, 0.30, 0.20])
            ],
            trailing_stop_enabled=exit_.get("trailing_stop_enabled", True),
            trailing_atr_multiplier_min=Decimal(str(exit_.get("trailing_atr_multiplier_min", "4.0"))),
            trailing_atr_multiplier_max=Decimal(str(exit_.get("trailing_atr_multiplier_max", "6.0"))),
            trailing_stop_floor_pct=Decimal(str(exit_.get("trailing_stop_floor_pct", "0.04"))),
            trailing_activates_after_r=Decimal(str(exit_.get("trailing_activates_after_r", "1.0"))),
            trailing_stop_confirmation_bars=exit_.get("trailing_stop_confirmation_bars", 1),
            trailing_stop_hard_floor_atr=Decimal(str(exit_.get("trailing_stop_hard_floor_atr", "3.0"))),
            time_stop_minutes=exit_.get("time_stop_minutes", 90),
        )


class StrategyEngine:
    """
    Pure domain logic — no I/O, no async.

    evaluate() takes a bar series (oldest first, features already computed)
    and returns a (SignalCandidate, stop_price) pair.
    stop_price is None when passed_filters=False.
    """

    def __init__(self, config: StrategyConfig) -> None:
        self._cfg = config

    def evaluate(
        self,
        bars: list[MarketBar],
        open_position_count: int,
        session_id: Optional[UUID] = None,
    ) -> tuple[SignalCandidate, Optional[Decimal]]:
        """
        Returns (signal, stop_price).
        signal.passed_filters is True only when all filters pass AND setup is detected.
        """
        if len(bars) < self._cfg.lookback_bars + 1:
            return self._reject(bars, session_id, f"insufficient_bars:{len(bars)}"), None

        bar = bars[-1]  # current (most recent completed) bar
        prior = bars[-(self._cfg.lookback_bars + 1):-1]  # the lookback window before current

        # --- Setup detection (dispatch on config) ---
        if self._cfg.setup_type == "momentum_ignition":
            setup_detected, rejection = self._detect_momentum_ignition(bar, prior)
        else:
            setup_detected, _, rejection = self._detect_breakout_reclaim(bar, prior)
        if not setup_detected:
            return self._reject(bars, session_id, rejection), None

        # --- Filter chain ---
        failures = self._run_filters(bar, open_position_count)
        if failures:
            return self._reject(bars, session_id, "; ".join(failures)), None

        # --- Stop price ---
        stop_price = self._calculate_stop(bar)
        if stop_price is None:
            return self._reject(bars, session_id, "atr_unavailable"), None
        if stop_price >= bar.close:
            return self._reject(bars, session_id, "stop_above_entry"), None

        signal = SignalCandidate(
            strategy_name=self._cfg.strategy_name,
            strategy_version=self._cfg.version,
            symbol=bar.symbol,
            pair_address=bar.pair_address,
            signal_direction="long",
            passed_filters=True,
            session_id=session_id,
        )
        logger.info(
            "SIGNAL %s | setup=%s close=%.8f stop=%.8f",
            bar.pair_address, self._cfg.setup_type, bar.close, stop_price,
        )
        return signal, stop_price

    # ------------------------------------------------------------------
    # Setup detection
    #
    # The concrete pattern-detection rules are withheld from this public
    # repository. Each detector returns whether a tradeable setup is present on
    # the current bar, along with a structured rejection reason when it is not
    # (rejections are journaled so the research loop can learn from near-misses).
    # ------------------------------------------------------------------

    def _detect_breakout_reclaim(
        self,
        bar: MarketBar,
        prior: list[MarketBar],
    ) -> tuple[bool, Decimal, str]:
        """
        Breakout-reclaim family: a break above prior resistance that follows a
        pullback/retest, confirming demand at the level before entry.

        Returns (detected, resistance_level, rejection_reason).

        Detection internals withheld — see module docstring.
        """
        raise NotImplementedError(
            "Setup-detection internals are withheld from this public repository."
        )

    def _detect_momentum_ignition(
        self,
        bar: MarketBar,
        prior: list[MarketBar],
    ) -> tuple[bool, str]:
        """
        Momentum-ignition family: a rapid, volume-fuelled surge with sustained
        buying pressure, entered on continuation rather than on a pullback, and
        rejected when price is already over-extended.

        Returns (detected, rejection_reason).

        Detection internals withheld — see module docstring.
        """
        raise NotImplementedError(
            "Setup-detection internals are withheld from this public repository."
        )

    # ------------------------------------------------------------------
    # Filter chain
    # ------------------------------------------------------------------

    def _run_filters(self, bar: MarketBar, open_position_count: int) -> list[str]:
        failures = []

        if bar.liquidity_usd < self._cfg.min_liquidity_usd:
            failures.append(
                f"liquidity:{float(bar.liquidity_usd):.0f}<{float(self._cfg.min_liquidity_usd):.0f}"
            )

        if bar.spread_estimate_bps is not None and bar.spread_estimate_bps > self._cfg.max_spread_bps:
            failures.append(
                f"spread:{float(bar.spread_estimate_bps):.1f}bps>{float(self._cfg.max_spread_bps):.0f}bps"
            )

        if bar.relative_volume_10 is not None and bar.relative_volume_10 < self._cfg.min_relative_volume:
            failures.append(
                f"rel_vol:{float(bar.relative_volume_10):.2f}<{float(self._cfg.min_relative_volume):.1f}"
            )

        if bar.volatility_regime is not None and bar.volatility_regime not in self._cfg.regime_allowed:
            failures.append(f"regime:{bar.volatility_regime}_not_in_{self._cfg.regime_allowed}")

        if (
            self._cfg.lifecycle_regime_allowed
            and bar.lifecycle_regime is not None
            and bar.lifecycle_regime not in self._cfg.lifecycle_regime_allowed
        ):
            failures.append(
                f"lifecycle_regime:{bar.lifecycle_regime}_not_in_{self._cfg.lifecycle_regime_allowed}"
            )

        if (
            self._cfg.min_time_since_launch_minutes > 0
            and bar.time_since_launch_minutes is not None
            and bar.time_since_launch_minutes < self._cfg.min_time_since_launch_minutes
        ):
            failures.append(
                f"too_new:{float(bar.time_since_launch_minutes):.0f}min"
                f"<{self._cfg.min_time_since_launch_minutes}min"
            )

        if open_position_count >= self._cfg.max_concurrent_positions:
            failures.append(f"max_positions:{open_position_count}>={self._cfg.max_concurrent_positions}")

        if (
            bar.min_recent_trades is not None
            and bar.min_recent_trades < self._cfg.min_trades_last_hour
        ):
            failures.append(
                f"trade_activity:{bar.min_recent_trades}<{self._cfg.min_trades_last_hour}/hr"
            )

        return failures

    # ------------------------------------------------------------------
    # Stop price
    # ------------------------------------------------------------------

    def _calculate_stop(self, bar: MarketBar) -> Optional[Decimal]:
        """ATR stop: entry - (atr_14 × atr_multiple)."""
        if bar.atr_14 is None:
            return None
        return bar.close - (bar.atr_14 * self._cfg.atr_multiple)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reject(
        self,
        bars: list[MarketBar],
        session_id: Optional[UUID],
        reason: str,
    ) -> SignalCandidate:
        bar = bars[-1] if bars else None
        return SignalCandidate(
            strategy_name=self._cfg.strategy_name,
            strategy_version=self._cfg.version,
            symbol=bar.symbol if bar else "",
            pair_address=bar.pair_address if bar else "",
            signal_direction="long",
            passed_filters=False,
            rejection_reason=reason,
            session_id=session_id,
        )
