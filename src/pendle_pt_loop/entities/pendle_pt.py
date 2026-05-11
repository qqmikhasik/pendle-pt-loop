"""Pendle Principal Token (PT) entity — Session 1 stub.

A PT is a transferable claim on one unit of the underlying SY
(standardized yield token, e.g. sUSDe) redeemable 1:1 at expiry.
Before expiry, PT trades at a discount on Pendle's bespoke AMM; the
discount is the *implied yield* the market is currently pricing.

This entity tracks one PT position on a single (market, expiry) pair.

* ``GlobalState`` — current PT mark price, implied yield, seconds-to-expiry.
* ``InternalState`` — PT face amount held, USDC cash leftover.
* Actions:
  - ``action_deposit`` — accept USDC into cash.
  - ``action_buy_pt`` — swap USDC → PT through Pendle AMM.
  - ``action_sell_pt`` — swap PT → USDC (used for early exit / unwind).
  - ``action_redeem`` — redeem PT 1:1 for SY at expiry.
  - ``action_withdraw`` — return USDC cash to the caller.

Session 1 status:
    Signatures fixed, ``update_state`` is identity, action bodies are
    placeholders. Session 2 will plug in:
      - PT price decay :math:`P_{PT}(t) = e^{-r_{impl}(T-t)}` (continuous)
        with Pendle's actual AMM curve.
      - Slippage model for non-trivial swap sizes.
      - Expiry handling and the 1:1 redeem path.

Storage / inheritance note:
    We use ``fractal.core.base.entity.BaseEntity`` directly because no
    existing fractal-defi base captures "fixed-yield discount bond" —
    the existing spot/LP/lending/perp bases assume different mechanics.
    Session 2 may extract a ``BaseDiscountBondEntity`` once we see how
    much the YT entity (variant 3) overlaps.
"""

from __future__ import annotations

from dataclasses import dataclass

from fractal.core.base.entity import (
    BaseEntity,
    EntityException,
    GlobalState,
    InternalState,
)


@dataclass
class PendlePTGlobalState(GlobalState):
    """Market context for a single PT/SY market on a given epoch.

    Attributes:
        pt_price: Current market price of 1 PT in USDC.
            Pre-expiry: in :math:`(0, 1]`, monotonically approaching 1.
            At/after expiry: exactly 1 (constant).
        implied_yield: Annualized fixed yield baked into ``pt_price``
            at the moment of observation, in decimal (0.14 = 14% APY).
            Convenience only — derivable from ``pt_price`` and
            ``seconds_to_expiry``; we cache it to avoid recomputing on
            every step.
        seconds_to_expiry: Wall-clock seconds remaining until PT redeem
            unlocks. Drops to 0 at expiry, then stays 0.
        pool_liquidity: Total liquidity in the PT/SY Pendle pool, in
            USDC equivalent. Used to estimate slippage in Session 2.
    """

    pt_price: float = 1.0
    implied_yield: float = 0.0
    seconds_to_expiry: float = 0.0
    pool_liquidity: float = 0.0


@dataclass
class PendlePTInternalState(InternalState):
    """Position state inside the PT entity.

    Attributes:
        pt_face_amount: Quantity of PT held, in face units (1 face = 1 USDC at expiry).
        cash: Free USDC sitting in the entity (not yet deployed to PT).
    """

    pt_face_amount: float = 0.0
    cash: float = 0.0


@dataclass
class PendlePTConfig:
    """Configuration for a Pendle PT entity.

    Attributes:
        market_address: The 20-byte address of the Pendle market.
            Informational at backtest level; used by loaders to scope
            historical data.
        amm_fee_rate: Fee charged by Pendle AMM on PT/SY swaps.
            Default 0.001 (10 basis points) — Pendle's typical pool fee.
    """

    market_address: str = "0x0000000000000000000000000000000000000000"
    amm_fee_rate: float = 0.001


class PendlePTEntity(BaseEntity[PendlePTGlobalState, PendlePTInternalState]):
    """Pendle Principal Token position.

    Notional unit: USDC. A PT with face amount ``N`` is currently worth
    ``N * pt_price`` USDC and will redeem for ``N`` USDC at expiry.
    """

    _internal_state: PendlePTInternalState
    _global_state: PendlePTGlobalState

    def __init__(self, config: PendlePTConfig | None = None) -> None:
        self._config = config or PendlePTConfig()
        super().__init__()

    def _initialize_states(self) -> None:
        self._global_state = PendlePTGlobalState()
        self._internal_state = PendlePTInternalState()

    def update_state(self, state: PendlePTGlobalState) -> None:
        """Apply new market context. No accrual on the entity side —
        PT carries its yield in its price, not as a cash stream."""
        self._global_state = state

    @property
    def balance(self) -> float:
        """Mark-to-market equity of the entity in USDC."""
        return (
            self._internal_state.cash
            + self._internal_state.pt_face_amount * self._global_state.pt_price
        )

    # ------------------------------------------------------------------
    # Action methods. Session 1 placeholders — math arrives in Session 2.
    # ------------------------------------------------------------------

    def action_deposit(self, amount_in_notional: float) -> None:
        """Add USDC cash to the entity."""
        if amount_in_notional < 0:
            raise EntityException(
                f"action_deposit: amount must be non-negative, got {amount_in_notional}"
            )
        self._internal_state.cash += amount_in_notional

    def action_withdraw(self, amount_in_notional: float) -> None:
        """Remove USDC cash from the entity."""
        if amount_in_notional < 0:
            raise EntityException(
                f"action_withdraw: amount must be non-negative, got {amount_in_notional}"
            )
        if amount_in_notional > self._internal_state.cash:
            raise EntityException(
                f"action_withdraw: requested {amount_in_notional} but only "
                f"{self._internal_state.cash} cash available"
            )
        self._internal_state.cash -= amount_in_notional

    def action_buy_pt(self, amount_in_notional: float) -> None:
        """Swap ``amount_in_notional`` USDC for PT through the Pendle AMM.

        Session 1 stub: assumes zero slippage and zero fee.
        ``pt_face_received = amount / pt_price``. Session 2 swaps this
        out for the real Pendle AMM curve.
        """
        if amount_in_notional < 0:
            raise EntityException(
                f"action_buy_pt: amount must be non-negative, got {amount_in_notional}"
            )
        if amount_in_notional > self._internal_state.cash:
            raise EntityException(
                f"action_buy_pt: requested {amount_in_notional} but only "
                f"{self._internal_state.cash} cash available"
            )
        pt_price = self._global_state.pt_price
        if pt_price <= 0:
            raise EntityException(f"action_buy_pt: invalid pt_price {pt_price}")

        # TODO(Session 2): real Pendle AMM curve with fee + slippage.
        pt_received = amount_in_notional / pt_price
        self._internal_state.cash -= amount_in_notional
        self._internal_state.pt_face_amount += pt_received

    def action_sell_pt(self, amount_in_face: float) -> None:
        """Swap ``amount_in_face`` PT (face units) for USDC.

        Session 1 stub: zero slippage. Session 2: real curve + fee.
        """
        if amount_in_face < 0:
            raise EntityException(
                f"action_sell_pt: amount must be non-negative, got {amount_in_face}"
            )
        if amount_in_face > self._internal_state.pt_face_amount:
            raise EntityException(
                f"action_sell_pt: requested {amount_in_face} but only "
                f"{self._internal_state.pt_face_amount} PT held"
            )

        # TODO(Session 2): real Pendle AMM curve with fee + slippage.
        usdc_received = amount_in_face * self._global_state.pt_price
        self._internal_state.pt_face_amount -= amount_in_face
        self._internal_state.cash += usdc_received

    def action_redeem(self, amount_in_face: float) -> None:
        """Redeem PT 1:1 for the underlying at/after expiry.

        Session 1 stub: requires ``seconds_to_expiry == 0`` and just
        converts at par. Session 2 will model the SY → USDC conversion
        properly (sUSDe may not be exactly 1 USDC at expiry).
        """
        if amount_in_face < 0:
            raise EntityException(
                f"action_redeem: amount must be non-negative, got {amount_in_face}"
            )
        if self._global_state.seconds_to_expiry > 0:
            raise EntityException(
                f"action_redeem: PT not at expiry yet "
                f"(seconds_to_expiry={self._global_state.seconds_to_expiry})"
            )
        if amount_in_face > self._internal_state.pt_face_amount:
            raise EntityException(
                f"action_redeem: requested {amount_in_face} but only "
                f"{self._internal_state.pt_face_amount} PT held"
            )

        # At expiry PT mints 1 SY which is 1 USDC equivalent in this stub.
        # TODO(Session 2): account for SY ↔ USDC conversion (sUSDe → USDC).
        self._internal_state.pt_face_amount -= amount_in_face
        self._internal_state.cash += amount_in_face
