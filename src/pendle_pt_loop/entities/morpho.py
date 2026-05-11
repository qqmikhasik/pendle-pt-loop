"""Morpho isolated-market lending entity — Session 1 stub.

Morpho Blue exposes one isolated market per (collateral, loan) pair.
For our strategy the relevant market is **PT-sUSDe → USDC**:
deposit PT as collateral, borrow USDC against it, with a fixed
liquidation loan-to-value (LLTV) parameter set per market (typically
0.86 or 0.915 for PT collateral).

This entity is intentionally close in shape to ``fractal.core.entities.protocols.aave.AaveEntity``
(borrowed style, dropped the ``collateral_is_volatile`` knob since our
collateral is PT and direction is fixed: collateral = PT, debt = USDC).

* ``GlobalState`` — collateral price (PT mark in USDC), debt price (USDC = 1),
  borrowing rate, utilization.
* ``InternalState`` — collateral amount (PT face), debt amount (USDC).
* Actions:
  - ``action_deposit`` — add PT collateral (in face units).
  - ``action_withdraw`` — remove PT collateral.
  - ``action_borrow`` — draw USDC against collateral.
  - ``action_repay`` — pay back USDC debt.

Session 1 status:
    Action bodies validate and update state but do not yet enforce
    health-factor constraints; Session 2 plugs in liquidation logic
    and the borrow-rate accrual inside ``update_state``.
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
class MorphoGlobalState(GlobalState):
    """Market context for a single Morpho isolated market.

    Field naming mirrors AaveGlobalState so a strategy can swap one
    lending backend for another without rewriting predict logic.

    Attributes:
        collateral_price: USDC value of one unit of collateral (= PT mark price).
        debt_price: USDC value of one unit of debt asset. For USDC debt: 1.0.
        lending_rate: Per-step interest credited to collateral. Zero for
            Morpho's PT markets — PT does not earn supplier yield.
        borrowing_rate: Per-step interest charged on debt. Derived from
            Morpho IRM (interest-rate model) on each step.
        utilization: Fraction of supplied debt asset currently borrowed.
            Drives ``borrowing_rate`` through the IRM; we expose it for
            risk monitoring (rate spikes occur at high utilization).
    """

    collateral_price: float = 0.0
    debt_price: float = 1.0
    lending_rate: float = 0.0
    borrowing_rate: float = 0.0
    utilization: float = 0.0


@dataclass
class MorphoInternalState(InternalState):
    """Position state inside the Morpho market.

    Attributes:
        collateral: PT collateral amount in face units.
        debt: USDC debt amount.
    """

    collateral: float = 0.0
    debt: float = 0.0


@dataclass
class MorphoConfig:
    """Configuration for a Morpho lending market.

    Attributes:
        market_id: Morpho's 32-byte market identifier (hex string).
            Informational at backtest level.
        lltv: Liquidation loan-to-value threshold for this market.
            When ``ltv > lltv`` the position is liquidatable.
            Typical Morpho PT markets: 0.86 or 0.915.
        liquidation_penalty: Bonus paid to liquidators on seized collateral.
            Typical Morpho: 0.05–0.075. Used in Session 2 for liquidation
            modeling.
    """

    market_id: str = "0x" + "00" * 32
    lltv: float = 0.86
    liquidation_penalty: float = 0.05


class MorphoEntity(BaseEntity[MorphoGlobalState, MorphoInternalState]):
    """Morpho isolated market: PT-collateral → USDC-debt.

    Notional unit: USDC. The entity holds collateral on the protocol
    side and tracks accrued debt; the user-visible "equity" is
    ``collateral_value - debt`` in USDC.
    """

    _internal_state: MorphoInternalState
    _global_state: MorphoGlobalState

    def __init__(self, config: MorphoConfig | None = None) -> None:
        self._config = config or MorphoConfig()
        super().__init__()

    def _initialize_states(self) -> None:
        self._global_state = MorphoGlobalState()
        self._internal_state = MorphoInternalState()

    def update_state(self, state: MorphoGlobalState) -> None:
        """Apply new market context.

        Session 1 stub: just store the new state. Session 2 will:
          - accrue debt at ``borrowing_rate * dt``;
          - flag positions whose ``ltv > lltv`` for liquidation in the
            strategy's pre-action sweep.
        """
        self._global_state = state

    # ------------------------------------------------------------------
    # Derived quantities. Available immediately (no Session 2 dependency).
    # ------------------------------------------------------------------

    @property
    def collateral_value(self) -> float:
        """Collateral mark-to-market in USDC."""
        return (
            self._internal_state.collateral * self._global_state.collateral_price
        )

    @property
    def debt_value(self) -> float:
        """Debt mark-to-market in USDC. For USDC debt this equals ``debt``."""
        return self._internal_state.debt * self._global_state.debt_price

    @property
    def ltv(self) -> float:
        """Current loan-to-value ratio.

        Returns 0.0 when collateral is zero (debt should also be zero by
        invariant; if not, the position is already insolvent — Session 2
        will treat that case as ``ltv = +inf`` via a separate check).
        """
        cv = self.collateral_value
        if cv <= 0:
            return 0.0
        return self.debt_value / cv

    @property
    def health_factor(self) -> float:
        """Distance to liquidation. >1 = safe, ≤1 = liquidatable.

        Defined as ``lltv / ltv`` to match the standard DeFi convention
        (Aave health factor formula, modulo small differences in how
        each protocol expresses the liquidation threshold).
        """
        cur_ltv = self.ltv
        if cur_ltv == 0:
            return float("inf")
        return self._config.lltv / cur_ltv

    @property
    def balance(self) -> float:
        """Equity in USDC = collateral_value - debt_value."""
        return self.collateral_value - self.debt_value

    # ------------------------------------------------------------------
    # Action methods. Session 1: validate + update state. Session 2 adds
    # LLTV enforcement and borrow-rate accrual.
    # ------------------------------------------------------------------

    def action_deposit(self, amount_in_notional: float) -> None:
        """Add PT collateral (notional amount is in PT face units)."""
        if amount_in_notional < 0:
            raise EntityException(
                f"action_deposit: amount must be non-negative, got {amount_in_notional}"
            )
        self._internal_state.collateral += amount_in_notional

    def action_withdraw(self, amount_in_notional: float) -> None:
        """Remove PT collateral (in face units).

        Session 1: validates against current collateral only. Session 2:
        also enforces post-withdraw ``ltv <= lltv``.
        """
        if amount_in_notional < 0:
            raise EntityException(
                f"action_withdraw: amount must be non-negative, got {amount_in_notional}"
            )
        if amount_in_notional > self._internal_state.collateral:
            raise EntityException(
                f"action_withdraw: requested {amount_in_notional} but only "
                f"{self._internal_state.collateral} collateral held"
            )
        # TODO(Session 2): assert post-withdraw ltv <= lltv.
        self._internal_state.collateral -= amount_in_notional

    def action_borrow(self, amount_in_notional: float) -> None:
        """Draw USDC against collateral.

        Session 1: validates non-negativity. Session 2: enforces
        post-borrow ``ltv <= lltv`` and tracks the borrow against
        market-wide utilization (which feeds back into ``borrowing_rate``
        on the next ``update_state``).
        """
        if amount_in_notional < 0:
            raise EntityException(
                f"action_borrow: amount must be non-negative, got {amount_in_notional}"
            )
        # TODO(Session 2): assert post-borrow ltv <= lltv.
        self._internal_state.debt += amount_in_notional

    def action_repay(self, amount_in_notional: float) -> None:
        """Pay back USDC debt."""
        if amount_in_notional < 0:
            raise EntityException(
                f"action_repay: amount must be non-negative, got {amount_in_notional}"
            )
        if amount_in_notional > self._internal_state.debt:
            raise EntityException(
                f"action_repay: requested {amount_in_notional} but only "
                f"{self._internal_state.debt} debt outstanding"
            )
        self._internal_state.debt -= amount_in_notional
