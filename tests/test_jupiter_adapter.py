"""Unit tests for JupiterExecutionAdapter helpers.

All tests are offline (no network calls). The network-dependent methods
(_get_quote, _get_swap_transaction, _sign_and_send) are tested via mocking
in integration tests. Here we focus on:
  - _parse_quote_price: fill price + slippage calculation
  - _get_token_mint: mint extraction from order.tx_hash
  - _get_token_amount: token lamport estimation
  - _KillSwitch: activation and guard logic
"""

import pytest
from decimal import Decimal
from uuid import uuid4

from alpha_loop.adapters.execution.jupiter_execution_adapter import (
    _KillSwitch,
    _MAX_FAILURES,
    _USDC_DECIMALS,
    _parse_quote_price,
    _get_token_mint,
    _get_token_amount,
)
from alpha_loop.domain.entities.trade import Order


# ---------------------------------------------------------------------------
# _parse_quote_price
# ---------------------------------------------------------------------------

class TestParseQuotePrice:
    def _quote(self, in_amount: int, out_amount: int) -> dict:
        return {
            "inAmount": str(in_amount),
            "outAmount": str(out_amount),
            "priceImpactPct": "0.001",
        }

    def test_buy_price_calculated_correctly(self):
        # 10 USDC in (10_000_000 lamports) → 1_000_000 token lamports (1 token)
        # expected fill_price = 10 USDC / 1 token = 10.0
        quote = self._quote(in_amount=10_000_000, out_amount=1_000_000)
        price, slippage = _parse_quote_price(
            quote, requested_price=Decimal("10.0"), side="buy"
        )
        assert abs(price - 10.0) < 0.001

    def test_sell_price_calculated_correctly(self):
        # 1_000_000 token lamports in → 9_500_000 USDC lamports out
        # expected fill_price = 9.5 USDC / 1 token = 9.5
        quote = self._quote(in_amount=1_000_000, out_amount=9_500_000)
        price, slippage = _parse_quote_price(
            quote, requested_price=Decimal("10.0"), side="sell"
        )
        assert abs(price - 9.5) < 0.001

    def test_slippage_bps_computed_from_price_deviation(self):
        # requested = 10.0, filled = 9.5 → deviation = 0.5/10 = 5% = 500 bps
        quote = self._quote(in_amount=1_000_000, out_amount=9_500_000)
        price, slippage = _parse_quote_price(
            quote, requested_price=Decimal("10.0"), side="sell"
        )
        assert abs(slippage - 500.0) < 1.0

    def test_zero_amounts_return_requested_price(self):
        quote = self._quote(in_amount=0, out_amount=0)
        price, slippage = _parse_quote_price(
            quote, requested_price=Decimal("5.0"), side="buy"
        )
        assert price == 5.0
        assert slippage == 0.0

    def test_no_requested_price_falls_back_to_price_impact(self):
        quote = self._quote(in_amount=1_000_000, out_amount=9_500_000)
        quote["priceImpactPct"] = "0.01"   # 1% = 100 bps
        price, slippage = _parse_quote_price(
            quote, requested_price=None, side="sell"
        )
        # Slippage should come from priceImpactPct when no requested_price
        assert abs(slippage - 100.0) < 1.0


# ---------------------------------------------------------------------------
# _get_token_mint
# ---------------------------------------------------------------------------

class TestGetTokenMint:
    def _order(self, tx_hash=None):
        return Order(
            id=uuid4(),
            position_id=uuid4(),
            side="buy",
            requested_price=Decimal("1.0"),
            size_usd=Decimal("100"),
            tx_hash=tx_hash,
        )

    def test_returns_tx_hash_as_mint_when_set(self):
        mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        order = self._order(tx_hash=mint)
        assert _get_token_mint(order) == mint

    def test_raises_when_no_mint(self):
        order = self._order(tx_hash=None)
        with pytest.raises(ValueError, match="missing token mint address"):
            _get_token_mint(order)

    def test_raises_for_short_tx_hash(self):
        # A real tx signature is 87 chars; a mint is 44 chars; we require > 32
        order = self._order(tx_hash="short")
        with pytest.raises(ValueError, match="missing token mint address"):
            _get_token_mint(order)


# ---------------------------------------------------------------------------
# _get_token_amount
# ---------------------------------------------------------------------------

class TestGetTokenAmount:
    def _order(self, size_usd, price):
        return Order(
            id=uuid4(),
            side="sell",
            requested_price=Decimal(str(price)),
            size_usd=Decimal(str(size_usd)),
        )

    def test_computes_token_lamports(self):
        # $100 position, price = $10/token → 10 tokens → 10_000_000 lamports (6 dec)
        order = self._order(size_usd=100, price=10)
        lamports = _get_token_amount(order)
        assert lamports == 10_000_000

    def test_sub_dollar_token(self):
        # $50 position, price = $0.001/token → 50_000 tokens → 50_000_000_000 lamports
        order = self._order(size_usd=50, price=0.001)
        lamports = _get_token_amount(order)
        assert lamports == 50_000_000_000

    def test_raises_when_no_price(self):
        order = Order(
            id=uuid4(),
            side="sell",
            requested_price=None,
            size_usd=Decimal("100"),
        )
        with pytest.raises(ValueError, match="cannot compute token amount"):
            _get_token_amount(order)

    def test_raises_when_zero_price(self):
        order = self._order(size_usd=100, price=0)
        with pytest.raises(ValueError, match="cannot compute token amount"):
            _get_token_amount(order)


# ---------------------------------------------------------------------------
# _KillSwitch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_not_active_initially(self):
        ks = _KillSwitch()
        assert not ks.active
        ks.check()  # should not raise

    def test_activates_after_max_failures(self):
        ks = _KillSwitch()
        for _ in range(_MAX_FAILURES - 1):
            ks.record_failure()
            assert not ks.active

        ks.record_failure()
        assert ks.active
        assert ks.activated_at is not None

    def test_raises_on_check_when_active(self):
        ks = _KillSwitch()
        ks.active = True
        with pytest.raises(RuntimeError, match="kill switch is active"):
            ks.check()

    def test_success_resets_failure_count(self):
        ks = _KillSwitch()
        for _ in range(_MAX_FAILURES - 1):
            ks.record_failure()
        ks.record_success()
        assert ks.consecutive_failures == 0
        # Another round of failures should require full _MAX_FAILURES to activate
        for _ in range(_MAX_FAILURES - 1):
            ks.record_failure()
        assert not ks.active

    def test_success_does_not_reset_if_already_active(self):
        ks = _KillSwitch()
        for _ in range(_MAX_FAILURES):
            ks.record_failure()
        assert ks.active
        ks.record_success()
        # Still active — must be manually cleared via reset()
        assert ks.active

    def test_reset_clears_active_kill_switch(self):
        ks = _KillSwitch()
        for _ in range(_MAX_FAILURES):
            ks.record_failure()
        assert ks.active
        ks.reset()
        assert not ks.active
        assert ks.consecutive_failures == 0
        assert ks.activated_at is None

    def test_reset_on_inactive_switch_is_noop(self):
        ks = _KillSwitch()
        ks.reset()   # should not raise
        assert not ks.active

    def test_check_passes_after_reset(self):
        ks = _KillSwitch()
        for _ in range(_MAX_FAILURES):
            ks.record_failure()
        ks.reset()
        ks.check()   # should not raise
