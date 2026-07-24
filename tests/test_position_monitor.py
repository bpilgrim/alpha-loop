"""Tests for PositionMonitor — all five exit paths."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from alpha_loop.domain.entities.market_bar import MarketBar
from alpha_loop.domain.entities.position import Position, TakeProfitLevel
from alpha_loop.domain.services.position_monitor import PositionMonitor
from alpha_loop.domain.services.strategy_engine import StrategyConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
_ADDR = "TestAddr"


def _cfg(**overrides) -> StrategyConfig:
    defaults = dict(
        trailing_stop_enabled=True,
        trailing_atr_multiplier_min=Decimal("4.0"),
        trailing_atr_multiplier_max=Decimal("6.0"),
        trailing_stop_floor_pct=Decimal("0.04"),
        trailing_activates_after_r=Decimal("1.0"),
        time_stop_minutes=90,
        take_profit_multiples=[Decimal("1.618"), Decimal("2.618"), Decimal("4.236")],
        take_profit_portions=[Decimal("0.50"), Decimal("0.30"), Decimal("0.20")],
    )
    defaults.update(overrides)
    return StrategyConfig(**defaults)


def _pos(
    entry: float = 1.0,
    stop: float = 0.90,
    entry_minutes_ago: int = 10,
    tp_multiples: list[float] | None = None,
    tp_portions: list[float] | None = None,
) -> Position:
    if tp_multiples is None:
        tp_multiples = [1.618, 2.618, 4.236]
    if tp_portions is None:
        tp_portions = [0.50, 0.30, 0.20]
    e = Decimal(str(entry))
    s = Decimal(str(stop))
    return Position(
        pair_address=_ADDR,
        symbol="TEST",
        entry_price=e,
        stop_price=s,
        initial_stop_price=s,          # set once at open, never changed
        entry_time=_NOW - timedelta(minutes=entry_minutes_ago),
        take_profit_levels=[
            TakeProfitLevel(price=e * Decimal(str(m)), portion_pct=Decimal(str(p)))
            for m, p in zip(tp_multiples, tp_portions)
        ],
    )


def _eval(monitor, pos, bar, minutes_elapsed: int = 10) -> "ExitDecision":
    """Evaluate with a deterministic 'now' relative to _NOW."""
    return monitor.evaluate(pos, bar, now=_NOW + timedelta(minutes=minutes_elapsed))


def _bar(
    close: float,
    high: float | None = None,
    low: float | None = None,
    liquidity: float = 200_000,
    token_status: str = "active",
    atr: float = 0.05,
) -> MarketBar:
    c = Decimal(str(close))
    return MarketBar(
        symbol="TEST",
        pair_address=_ADDR,
        chain="solana",
        timestamp=_NOW,
        timeframe="1m",
        open=c,
        high=Decimal(str(high)) if high else c * Decimal("1.005"),
        low=Decimal(str(low)) if low else c * Decimal("0.995"),
        close=c,
        volume=Decimal("1000"),
        liquidity_usd=Decimal(str(liquidity)),
        token_status=token_status,
        atr_14=Decimal(str(atr)),
        feature_version=1,
    )


# ---------------------------------------------------------------------------
# Liquidity emergency (priority 1)
# ---------------------------------------------------------------------------

class TestLiquidityEmergency:
    def test_exits_on_rugpull_flag(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos()
        decision = _eval(monitor, pos, _bar(close=1.0, token_status="rugpull_flagged"))
        assert decision.should_exit
        assert decision.exit_reason == "liquidity_emergency"
        assert decision.emergency is True
        assert decision.is_partial is False

    def test_exits_on_pair_dead(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos()
        decision = _eval(monitor, pos, _bar(close=1.0, token_status="pair_dead"))
        assert decision.should_exit
        assert decision.exit_reason == "liquidity_emergency"
        assert decision.emergency is True

    def test_exits_on_liquidity_below_floor(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos()
        decision = _eval(monitor, pos, _bar(close=1.0, liquidity=500))
        assert decision.should_exit
        assert decision.emergency is True

    def test_overrides_in_profit(self):
        """Emergency exit fires even when position is well in profit."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0)
        decision = _eval(monitor, pos, _bar(close=3.0, token_status="rugpull_flagged"))
        assert decision.should_exit
        assert decision.emergency is True


# ---------------------------------------------------------------------------
# Hard stop (priority 2)
# ---------------------------------------------------------------------------

class TestHardStop:
    def test_fires_at_stop_price(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)
        decision = _eval(monitor, pos, _bar(close=0.90))
        assert decision.should_exit
        assert decision.exit_reason == "stop"
        assert decision.is_partial is False

    def test_fires_below_stop_price(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)
        decision = _eval(monitor, pos, _bar(close=0.85))
        assert decision.should_exit
        assert decision.exit_reason == "stop"

    def test_does_not_fire_above_stop(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)
        decision = _eval(monitor, pos, _bar(close=0.95))
        assert decision.exit_reason != "stop"

    def test_remaining_portion_after_partial_tp(self):
        """Hard stop fires after one TP partial — exits remaining 50%."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)
        pos.take_profit_levels[0].triggered = True  # 50% already exited
        decision = _eval(monitor, pos, _bar(close=0.89))
        assert decision.should_exit
        assert decision.exit_reason == "stop"
        assert decision.exit_portion == Decimal("0.50")  # 1.0 - 0.50


# ---------------------------------------------------------------------------
# Take-profit partials (priority 3)
# ---------------------------------------------------------------------------

class TestTakeProfits:
    def test_first_tp_fires(self):
        """Close at 1.618× entry → 50% exit."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0)
        decision = _eval(monitor, pos, _bar(close=1.618))
        assert decision.should_exit
        assert decision.exit_reason == "take_profit_1"
        assert decision.exit_portion == Decimal("0.50")
        assert decision.is_partial is True
        assert pos.take_profit_levels[0].triggered is True

    def test_second_tp_fires_independently(self):
        """After TP1 already triggered, TP2 fires at 2.618×."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0)
        pos.take_profit_levels[0].triggered = True
        decision = _eval(monitor, pos, _bar(close=2.618))
        assert decision.should_exit
        assert decision.exit_reason == "take_profit_2"
        assert decision.exit_portion == Decimal("0.30")
        assert decision.is_partial is True

    def test_last_tp_is_not_partial(self):
        """TP3 is the last level — should not be marked partial."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0)
        pos.take_profit_levels[0].triggered = True
        pos.take_profit_levels[1].triggered = True
        decision = _eval(monitor, pos, _bar(close=4.236))
        assert decision.should_exit
        assert decision.exit_reason == "take_profit_3"
        assert decision.exit_portion == Decimal("0.20")
        assert decision.is_partial is False  # last TP closes it fully

    def test_skips_already_triggered(self):
        """Already-triggered TP levels are not re-fired."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0)
        pos.take_profit_levels[0].triggered = True
        decision = _eval(monitor, pos, _bar(close=1.80))  # between TP1 and TP2
        assert not decision.should_exit or decision.exit_reason != "take_profit_1"


# ---------------------------------------------------------------------------
# Trailing stop (priority 4)
# ---------------------------------------------------------------------------

class TestTrailingStop:
    def test_not_active_below_1r(self):
        """Trailing stop doesn't activate until price reaches 1R above entry."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)  # risk = 0.10; 1R target = 1.10
        decision = _eval(monitor, pos, _bar(close=1.05, atr=0.02))  # below activation
        assert not decision.should_exit
        assert pos.stop_price == Decimal("0.90")  # stop not moved

    def test_ratchets_up_at_1r(self):
        """Once price hits 1R, trailing stop is set above initial stop."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)  # 1R activation at 1.10
        # At 1.15, unrealized_r = 1.5; t = 0.5; mult = 5.0; candidate = 1.15 - 0.10 = 1.05
        _eval(monitor, pos, _bar(close=1.15, atr=Decimal("0.02")))
        expected_stop = Decimal("1.15") - Decimal("0.02") * Decimal("5.0")
        assert pos.stop_price == expected_stop  # 1.05

    def test_stop_never_moves_down(self):
        """Trailing stop only ratchets up, never retraces."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)
        _eval(monitor, pos, _bar(close=1.15, atr=Decimal("0.02")))
        high_stop = pos.stop_price
        assert high_stop > Decimal("0.90")
        _eval(monitor, pos, _bar(close=1.12, atr=Decimal("0.02")))
        assert pos.stop_price == high_stop  # not lowered

    def test_fires_when_price_falls_through_trail(self):
        """After trailing stop is set, a price drop through it triggers exit."""
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.90)
        # Raise stop to 1.05 via bar at 1.15
        _eval(monitor, pos, _bar(close=1.15, atr=Decimal("0.02")))
        # Price drops below the trailing stop (initial_stop_price=0.90 < stop_price=1.05)
        decision = _eval(monitor, pos, _bar(close=1.04, atr=Decimal("0.02")))
        assert decision.should_exit
        assert decision.exit_reason == "trailing_stop"
        assert decision.is_partial is False

    def test_floor_prevents_stop_below_entry_minus_floor_pct(self):
        """Trailing stop floor: stop never goes below entry × (1 - floor_pct)."""
        monitor = PositionMonitor(_cfg(trailing_stop_floor_pct=Decimal("0.04")))
        pos = _pos(entry=1.0, stop=0.90)
        # Huge ATR would push trail far below, but floor catches it
        _eval(monitor, pos, _bar(close=1.15, atr=Decimal("0.50")))
        floor = Decimal("1.0") * (1 - Decimal("0.04"))  # 0.96
        assert pos.stop_price >= floor


# ---------------------------------------------------------------------------
# Time stop (priority 5)
# ---------------------------------------------------------------------------

class TestTimeStop:
    def test_fires_at_time_limit(self):
        monitor = PositionMonitor(_cfg(time_stop_minutes=90))
        pos = _pos(entry=1.0, stop=0.90, entry_minutes_ago=0)
        # now = _NOW + 91 minutes → elapsed = 91 min
        decision = monitor.evaluate(pos, _bar(close=1.05), now=_NOW + timedelta(minutes=91))
        assert decision.should_exit
        assert decision.exit_reason == "time_stop"
        assert decision.is_partial is False

    def test_does_not_fire_before_time_limit(self):
        monitor = PositionMonitor(_cfg(time_stop_minutes=90))
        pos = _pos(entry_minutes_ago=0)
        decision = monitor.evaluate(pos, _bar(close=1.05), now=_NOW + timedelta(minutes=45))
        assert not decision.should_exit

    def test_time_stop_exits_remaining_after_partial_tp(self):
        monitor = PositionMonitor(_cfg(time_stop_minutes=90))
        pos = _pos(entry=1.0, entry_minutes_ago=0)
        pos.take_profit_levels[0].triggered = True  # 50% already exited
        decision = monitor.evaluate(pos, _bar(close=1.05), now=_NOW + timedelta(minutes=95))
        assert decision.should_exit
        assert decision.exit_reason == "time_stop"
        assert decision.exit_portion == Decimal("0.50")  # remaining after 50% TP


# ---------------------------------------------------------------------------
# MAE / MFE tracking
# ---------------------------------------------------------------------------

class TestExcursionTracking:
    def test_mae_tracks_lowest_low(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0)
        _eval(monitor, pos, _bar(close=1.05, low=0.95))
        assert pos.mae == Decimal("0.95") - Decimal("1.0")  # -0.05

    def test_mfe_tracks_highest_high(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0)
        _eval(monitor, pos, _bar(close=1.05, high=1.20))
        assert pos.mfe == Decimal("1.20") - Decimal("1.0")  # 0.20

    def test_mae_accumulates_across_bars(self):
        monitor = PositionMonitor(_cfg())
        pos = _pos(entry=1.0, stop=0.50)  # wide stop so it doesn't trigger
        _eval(monitor, pos, _bar(close=1.05, low=0.96))
        _eval(monitor, pos, _bar(close=1.04, low=0.94))
        _eval(monitor, pos, _bar(close=1.06, low=0.97))
        assert pos.mae == Decimal("0.94") - Decimal("1.0")  # worst low
