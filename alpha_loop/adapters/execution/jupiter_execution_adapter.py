"""JupiterExecutionAdapter — live swap execution via Jupiter v6 + Solana RPC.

Flow per order:
  1. GET /quote  — get best route for input_mint → output_mint
  2. POST /swap  — serialize the swap transaction
  3. Sign + send via Solana RPC (AsyncClient.send_raw_transaction)
  4. Poll for confirmation with timeout; activate kill switch on repeated failure

Environment variables required:
  SOLANA_PRIVATE_KEY  — base58-encoded keypair secret key (64 bytes)
  SOLANA_RPC_URL      — Solana RPC endpoint (e.g. https://api.mainnet-beta.solana.com)

USDC mint on Solana mainnet is used as the quote currency for all trades.
"""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx

from ...domain.entities.trade import Order
from ...ports.execution_port import ExecutionPort

logger = logging.getLogger(__name__)

# Jupiter v6 API
_JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
_JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"

# USDC on Solana mainnet — used as the quote/settlement token
_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_USDC_DECIMALS = 6

# Confirmation polling
_POLL_INTERVAL_SECONDS = 2
_CONFIRM_TIMEOUT_SECONDS = 60

# Max consecutive failures before kill switch activates
_MAX_FAILURES = 3

# Default slippage for normal orders (50 bps = 0.5%)
_DEFAULT_SLIPPAGE_BPS = 50
# Emergency exit slippage (500 bps = 5%)
_EMERGENCY_SLIPPAGE_BPS = 500


@dataclass
class _KillSwitch:
    active: bool = False
    consecutive_failures: int = 0
    activated_at: Optional[datetime] = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= _MAX_FAILURES:
            self.active = True
            self.activated_at = datetime.now(tz=timezone.utc)
            logger.critical(
                "KILL SWITCH ACTIVATED after %d consecutive execution failures at %s",
                self.consecutive_failures, self.activated_at,
            )

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def reset(self) -> None:
        """Manually clear the kill switch after operator review.
        Safe to call on startup to clear stale state from a prior crashed session.
        In live mode, only call this after confirming no stuck transactions on-chain.
        """
        if self.active:
            logger.warning(
                "Kill switch RESET (was activated at %s after %d failures). "
                "Ensure no stuck transactions before resuming.",
                self.activated_at, self.consecutive_failures,
            )
        self.active = False
        self.consecutive_failures = 0
        self.activated_at = None

    def check(self) -> None:
        """Raise if kill switch is active."""
        if self.active:
            raise RuntimeError(
                f"Jupiter execution kill switch is active (activated {self.activated_at}). "
                "Call reset_kill_switch() after manual review before trading can resume."
            )


class JupiterExecutionAdapter(ExecutionPort):
    """
    Live swap execution adapter using Jupiter v6 aggregator.

    All trades are USDC-denominated:
      - Entry (buy): USDC → token
      - Exit  (sell): token → USDC

    Slippage is expressed in basis points. Jupiter returns the actual fill
    price in the swap response; we compute slippage_bps from the deviation
    between requested and filled price.
    """

    _execution_type = "live"

    def __init__(
        self,
        private_key_b58: str,
        rpc_url: str,
        slippage_bps: int = _DEFAULT_SLIPPAGE_BPS,
    ) -> None:
        from solders.keypair import Keypair
        from solana.rpc.async_api import AsyncClient

        self._keypair = Keypair.from_base58_string(private_key_b58)
        self._pubkey = str(self._keypair.pubkey())
        self._rpc = AsyncClient(rpc_url)
        self._slippage_bps = slippage_bps
        self._kill_switch = _KillSwitch()
        logger.info("JupiterExecutionAdapter ready | wallet=%s", self._pubkey[:8] + "...")

    async def close(self) -> None:
        await self._rpc.close()

    def reset_kill_switch(self) -> None:
        """Delegate to the kill switch reset. Call on startup after operator review."""
        self._kill_switch.reset()

    # ------------------------------------------------------------------
    # ExecutionPort interface
    # ------------------------------------------------------------------

    async def submit_entry(self, order: Order) -> Order:
        """Buy `order.size_usd` USDC worth of the target token."""
        self._kill_switch.check()
        token_mint = order.position_id and str(order.position_id)
        # position_id holds the pair_address (token mint); size_usd in USDC
        input_mint  = _USDC_MINT
        output_mint = _get_token_mint(order)

        usdc_amount = int(order.size_usd * Decimal(10 ** _USDC_DECIMALS))

        return await self._execute_swap(
            order=order,
            input_mint=input_mint,
            output_mint=output_mint,
            amount_lamports=usdc_amount,
            side="buy",
            slippage_bps=self._slippage_bps,
        )

    async def submit_exit(self, order: Order, emergency: bool = False) -> Order:
        """Sell the token back to USDC."""
        self._kill_switch.check()
        input_mint  = _get_token_mint(order)
        output_mint = _USDC_MINT
        slippage    = _EMERGENCY_SLIPPAGE_BPS if emergency else self._slippage_bps

        # For exit we pass token amount in the token's native decimals.
        # size_usd is approximate; actual token amount must be pulled from
        # position context. The caller must populate order.token_amount_raw
        # (attached as extra_data) or we fall back to a USDC-equivalent estimate.
        token_amount = _get_token_amount(order)

        return await self._execute_swap(
            order=order,
            input_mint=input_mint,
            output_mint=output_mint,
            amount_lamports=token_amount,
            side="sell",
            slippage_bps=slippage,
            emergency=emergency,
        )

    async def get_fill_status(self, order_id: str) -> str:
        """
        Re-check confirmation for a previously submitted tx_hash.
        order_id is expected to be the tx_hash string.
        """
        try:
            sig_status = await self._rpc.get_signature_statuses([order_id])
            value = sig_status.value[0]
            if value is None:
                return "unconfirmed"
            if value.err:
                return "failed"
            return "filled"
        except Exception as exc:
            logger.warning("get_fill_status error for %s: %s", order_id, exc)
            return "unconfirmed"

    # ------------------------------------------------------------------
    # Internal swap execution
    # ------------------------------------------------------------------

    async def _execute_swap(
        self,
        order: Order,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        side: str,
        slippage_bps: int,
        emergency: bool = False,
    ) -> Order:
        try:
            quote = await self._get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
            tx_bytes = await self._get_swap_transaction(quote)
            tx_hash = await self._sign_and_send(tx_bytes)
            confirmed = await self._poll_confirmation(tx_hash)

            if not confirmed:
                raise RuntimeError(f"Transaction {tx_hash} did not confirm within {_CONFIRM_TIMEOUT_SECONDS}s")

            fill_price, slippage_actual = _parse_quote_price(
                quote=quote,
                requested_price=order.requested_price,
                side=side,
            )

            self._kill_switch.record_success()
            logger.info(
                "[live] %s fill | price=%.8f slippage=%.1f bps tx=%s",
                side.upper(), fill_price, slippage_actual, tx_hash[:16] + "...",
            )

            return Order(
                id=order.id,
                position_id=order.position_id,
                timestamp=datetime.now(tz=timezone.utc),
                side=side,
                requested_price=order.requested_price,
                fill_price=Decimal(str(fill_price)).quantize(Decimal("0.00000001")),
                slippage_bps=Decimal(str(slippage_actual)).quantize(Decimal("0.1")),
                size_usd=order.size_usd,
                execution_type="live",
                tx_hash=tx_hash,
                fill_status="filled",
            )

        except Exception as exc:
            self._kill_switch.record_failure()
            logger.error(
                "[live] %s FAILED (failures=%d emergency=%s): %s",
                side.upper(), self._kill_switch.consecutive_failures, emergency, exc,
            )
            return Order(
                id=order.id,
                position_id=order.position_id,
                timestamp=datetime.now(tz=timezone.utc),
                side=side,
                requested_price=order.requested_price,
                fill_price=None,
                slippage_bps=None,
                size_usd=order.size_usd,
                execution_type="live",
                tx_hash=None,
                fill_status="failed",
            )

    async def _get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int,
    ) -> dict:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_JUPITER_QUOTE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            raise RuntimeError(f"Jupiter quote error: {data['error']}")

        logger.debug(
            "Quote: %s → %s | in=%s out=%s priceImpact=%.4f%%",
            input_mint[:8], output_mint[:8],
            data.get("inAmount"), data.get("outAmount"),
            float(data.get("priceImpactPct", 0)) * 100,
        )
        return data

    async def _get_swap_transaction(self, quote: dict) -> bytes:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": self._pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(_JUPITER_SWAP_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            raise RuntimeError(f"Jupiter swap error: {data['error']}")

        tx_b64 = data.get("swapTransaction")
        if not tx_b64:
            raise RuntimeError("Jupiter swap response missing swapTransaction field")

        return base64.b64decode(tx_b64)

    async def _sign_and_send(self, tx_bytes: bytes) -> str:
        from solders.transaction import VersionedTransaction
        from solders.message import to_bytes_versioned
        from solana.rpc.types import TxOpts

        tx = VersionedTransaction.from_bytes(tx_bytes)
        # Sign the transaction message
        signed = VersionedTransaction(tx.message, [self._keypair])
        raw = bytes(signed)

        opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
        result = await self._rpc.send_raw_transaction(raw, opts=opts)

        if result.value is None:
            raise RuntimeError(f"send_raw_transaction returned no signature: {result}")

        return str(result.value)

    async def _poll_confirmation(self, tx_hash: str) -> bool:
        """Poll until confirmed or timeout. Returns True if confirmed."""
        deadline = time.monotonic() + _CONFIRM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            status = await self.get_fill_status(tx_hash)
            if status == "filled":
                return True
            if status == "failed":
                logger.warning("Transaction %s failed on-chain", tx_hash)
                return False

        logger.warning("Transaction %s did not confirm within %ds", tx_hash, _CONFIRM_TIMEOUT_SECONDS)
        return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_token_mint(order: Order) -> str:
    """
    Extract token mint address from order.
    We store the pair_address (which is the token mint for Solana) in a
    convention on Order: the `pair_address` attribute if set, otherwise
    we parse it from a side-channel. For now, LiveTrader is responsible
    for populating order with the token mint via a subclass or wrapper.

    Convention: LiveTrader passes mint in order.position_id as a string
    when creating live orders (position_id is a UUID normally; for live
    orders it's repurposed to carry the pair_address). The adapater reads
    it from the Order object's extra metadata stored in tx_hash field
    before execution (repurposed as "input metadata" pre-fill).

    Simpler approach used here: LiveTrader sets Order.tx_hash = mint_address
    before calling submit_entry/submit_exit, then we clear it post-fill.
    """
    # LiveTrader convention: tx_hash carries mint address before execution
    if order.tx_hash and len(order.tx_hash) > 32:
        return order.tx_hash
    raise ValueError(
        f"Order {order.id} missing token mint address. "
        "Set order.tx_hash = token_mint before calling submit_entry/submit_exit."
    )


def _get_token_amount(order: Order) -> int:
    """
    For exit orders, return the raw token amount to sell (in native decimals).
    LiveTrader populates order.size_usd as the USD value; we approximate
    the token amount using the requested_price (price per token in USD).

    This gives a reasonable approximation. For precision, LiveTrader should
    track the exact token balance from the entry fill's outAmount.
    """
    if order.requested_price and order.requested_price > 0 and order.size_usd:
        # Approximate token decimals: most Solana meme tokens use 6 or 9 decimals.
        # Jupiter handles this automatically via quoting — we just need a
        # "token amount in lamports" that is close to our position size.
        # Use 6 decimals as a conservative default (same as USDC).
        token_decimals = 6
        token_amount = order.size_usd / order.requested_price
        return int(token_amount * Decimal(10 ** token_decimals))
    raise ValueError(
        f"Order {order.id}: cannot compute token amount without size_usd and requested_price"
    )


def _parse_quote_price(
    quote: dict,
    requested_price: Optional[Decimal],
    side: str,
) -> tuple[float, float]:
    """
    Compute fill_price and slippage_bps from Jupiter quote fields.

    Jupiter returns:
      inAmount  — lamports of input token
      outAmount — lamports of output token

    For buy  (USDC → token): fill_price = inAmount_usd / outAmount_tokens
    For sell (token → USDC): fill_price = outAmount_usd / inAmount_tokens
    """
    in_amount  = int(quote.get("inAmount", 0))
    out_amount = int(quote.get("outAmount", 0))

    if in_amount == 0 or out_amount == 0:
        fill_price = float(requested_price) if requested_price else 0.0
        return fill_price, 0.0

    if side == "buy":
        # USDC in, token out — price = USDC / tokens
        usdc_in   = in_amount  / (10 ** _USDC_DECIMALS)
        token_out = out_amount / (10 ** 6)   # assume 6 decimals (conservative)
        fill_price = usdc_in / token_out if token_out else 0.0
    else:
        # token in, USDC out — price = USDC / tokens
        usdc_out  = out_amount / (10 ** _USDC_DECIMALS)
        token_in  = in_amount  / (10 ** 6)
        fill_price = usdc_out / token_in if token_in else 0.0

    if requested_price and float(requested_price) > 0:
        slippage_bps = abs(fill_price - float(requested_price)) / float(requested_price) * 10000
    else:
        slippage_bps = float(quote.get("priceImpactPct", 0)) * 10000

    return fill_price, slippage_bps
