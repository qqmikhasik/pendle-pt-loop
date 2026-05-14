"""Hedged PT-loop — static loop + long funding-rate hedge.

Variant 3 of the Project 2 strategies. The static loop carries the
risk that implied PT yield rises (PT price falls, LTV climbs toward
LLTV) — the dominant tail-risk of the leveraged carry trade. We add
a **long funding-rate hedge** sized as a fraction of the PT collateral
notional. Conceptual basis:

* sUSDe yield is driven by Ethena's funding-rate income on the short
  perpetual book (mainly ETH).
* Pendle's PT-sUSDE implied yield is the market's expectation of that
  same yield stream.
* So rising perpetual funding rates (ETH-USDC on Hyperliquid being a
  clean proxy) tend to push PT-sUSDE implied yield up, which pushes
  PT price down, which pushes our LTV up.
* A long position on the funding rate (Hyperliquid ETH perp short
  ↔ "receive funding when positive") pays out exactly when this
  adverse move happens, offsetting the PT-side mark-down.

In production this hedge would route through **Pendle Boros**, which
tokenises perpetual funding rates. At the time of this backtest the
Boros API surface was not exposed for historical research; we hedge
with the underlying Hyperliquid funding stream directly — the same
economic exposure Boros wraps.

Sizing
------
``HEDGE_RATIO`` (default 1.0) scales the hedge notional relative to
the PT-collateral notional after the open loop. Setting it below 1
gives a partial hedge; setting it above 1 over-hedges (useful for a
short-PT-yield-only directional bet on funding-rate compression).

State machine
-------------
Same as :class:`StaticLoopStrategy`: ``"uninvested" → "open" → "unwound"``.
After the loop sequence emits, an additional ``FundingHedgeEntity``
deposit fires; at expiry the hedge is closed alongside the unwind.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fractal.core.base import (
    Action,
    ActionToTake,
    BaseStrategy,
    BaseStrategyParams,
    NamedEntity,
)

from pendle_pt_loop.entities import (
    FundingHedgeConfig,
    FundingHedgeEntity,
    MorphoConfig,
    MorphoEntity,
    PendlePTConfig,
    PendlePTEntity,
)
from pendle_pt_loop.observations import HEDGE_SLOT, MORPHO_SLOT, PT_SLOT


@dataclass
class HedgedLoopParams(BaseStrategyParams):
    """Hyperparameters for the hedged loop.

    Attributes:
        INITIAL_BALANCE: USDC notional injected at open.
        TARGET_LTV: Same role as in StaticLoopParams; must sit below LLTV.
        N_CYCLES: Number of loop cycles at open.
        WARMUP_OBSERVATIONS: Skip this many leading observations before
            opening (lets entities populate their state cleanly).
        HEDGE_RATIO: Notional of the funding-rate hedge as a multiple
            of total PT collateral after the loop opens. 1.0 = exact
            notional match; 0.5 = half-hedge; 0.0 disables the hedge
            (reduces to the static loop). Negative values not allowed.
    """

    INITIAL_BALANCE: float = 10_000.0
    TARGET_LTV: float = 0.80
    N_CYCLES: int = 5
    WARMUP_OBSERVATIONS: int = 1
    HEDGE_RATIO: float = 1.0


class HedgedLoopStrategy(BaseStrategy[HedgedLoopParams]):
    """Static-LTV PT loop with a long funding-rate hedge leg."""

    STRICT_OBSERVATIONS: bool = True

    _state: Literal["uninvested", "open", "unwound"]
    _observations_seen: int

    def __init__(
        self,
        *,
        pt_config: PendlePTConfig | None = None,
        morpho_config: MorphoConfig | None = None,
        hedge_config: FundingHedgeConfig | None = None,
        params: HedgedLoopParams | dict | None = None,
        debug: bool = False,
    ) -> None:
        self._pt_config = pt_config
        self._morpho_config = morpho_config
        self._hedge_config = hedge_config
        super().__init__(params=params, debug=debug)

    def set_up(self) -> None:
        self._state = "uninvested"
        self._observations_seen = 0
        pt = PendlePTEntity(self._pt_config)
        morpho = MorphoEntity(self._morpho_config)
        hedge = FundingHedgeEntity(self._hedge_config)
        if self._params.TARGET_LTV >= morpho._config.lltv:
            raise ValueError(
                f"TARGET_LTV={self._params.TARGET_LTV} must be strictly "
                f"below Morpho lltv={morpho._config.lltv}"
            )
        if self._params.HEDGE_RATIO < 0:
            raise ValueError(
                f"HEDGE_RATIO must be non-negative, got {self._params.HEDGE_RATIO}"
            )
        self.register_entity(NamedEntity(entity_name=PT_SLOT, entity=pt))
        self.register_entity(NamedEntity(entity_name=MORPHO_SLOT, entity=morpho))
        self.register_entity(NamedEntity(entity_name=HEDGE_SLOT, entity=hedge))

    @property
    def _pt(self) -> PendlePTEntity:
        return self.get_entity(PT_SLOT)  # type: ignore[return-value]

    @property
    def _morpho(self) -> MorphoEntity:
        return self.get_entity(MORPHO_SLOT)  # type: ignore[return-value]

    @property
    def _hedge(self) -> FundingHedgeEntity:
        return self.get_entity(HEDGE_SLOT)  # type: ignore[return-value]

    def predict(self) -> list[ActionToTake]:
        self._observations_seen += 1
        if self._state == "unwound":
            return []

        pt_global = self._pt.global_state
        at_expiry = pt_global.seconds_to_expiry <= 0

        if self._state == "uninvested":
            if self._observations_seen <= self._params.WARMUP_OBSERVATIONS:
                return []
            if at_expiry:
                self._state = "unwound"
                return []
            actions = self._open_loop_with_hedge()
            self._state = "open"
            return actions

        # open
        if at_expiry:
            return self._unwind_with_hedge()
        return []

    # ------------------------------------------------------------------
    # Open — same loop as static, then open hedge with hedge_ratio × collateral.
    # ------------------------------------------------------------------

    def _open_loop_with_hedge(self) -> list[ActionToTake]:
        pt_price = self._pt.global_state.pt_price
        face_per_cycle, borrow_per_cycle = self._plan_cycles(pt_price)
        total_face = sum(face_per_cycle)
        # Pre-debit PT face we will buy across all cycles (tech-debt #1).
        self._pt._internal_state.pt_face_amount -= total_face

        actions: list[ActionToTake] = [
            self._pt_deposit(self._params.INITIAL_BALANCE)
        ]
        cycle_capital = self._params.INITIAL_BALANCE
        for face_received, borrow_amount in zip(face_per_cycle, borrow_per_cycle):
            actions.extend(
                self._cycle_actions(cycle_capital, face_received, borrow_amount)
            )
            cycle_capital = borrow_amount

        # Hedge notional sized off the planned collateral value, not the
        # post-cycle realised value (which the engine has not produced yet).
        total_collat_value = total_face * pt_price
        hedge_notional = self._params.HEDGE_RATIO * total_collat_value
        if hedge_notional > 0:
            actions.append(
                ActionToTake(
                    entity_name=HEDGE_SLOT,
                    action=Action(
                        "deposit", {"amount_in_notional": hedge_notional}
                    ),
                )
            )
        return actions

    def _plan_cycles(
        self, pt_price: float
    ) -> tuple[list[float], list[float]]:
        face_per_cycle: list[float] = []
        borrow_per_cycle: list[float] = []
        cycle_capital = self._params.INITIAL_BALANCE
        for _ in range(self._params.N_CYCLES):
            face = self._estimate_pt_face_received(cycle_capital, pt_price)
            collat_value = face * pt_price
            borrow = self._params.TARGET_LTV * collat_value
            face_per_cycle.append(face)
            borrow_per_cycle.append(borrow)
            cycle_capital = borrow
        return face_per_cycle, borrow_per_cycle

    def _cycle_actions(
        self,
        cycle_capital: float,
        face_received: float,
        borrow_amount: float,
    ) -> list[ActionToTake]:
        return [
            ActionToTake(
                entity_name=PT_SLOT,
                action=Action("buy_pt", {"amount_in_notional": cycle_capital}),
            ),
            ActionToTake(
                entity_name=MORPHO_SLOT,
                action=Action("deposit", {"amount_in_notional": face_received}),
            ),
            ActionToTake(
                entity_name=MORPHO_SLOT,
                action=Action("borrow", {"amount_in_notional": borrow_amount}),
            ),
            self._pt_deposit(borrow_amount),
        ]

    @staticmethod
    def _pt_deposit(amount: float) -> ActionToTake:
        return ActionToTake(
            entity_name=PT_SLOT,
            action=Action("deposit", {"amount_in_notional": amount}),
        )

    def _estimate_pt_face_received(
        self, amount_in_notional: float, pt_price: float
    ) -> float:
        cfg = self._pt._config
        pool = self._pt.global_state.pool_liquidity
        effective_in = amount_in_notional * (1.0 - cfg.amm_fee_rate)
        slip = cfg.slippage_factor * amount_in_notional / pool
        effective_price = pt_price * (1.0 + slip)
        return effective_in / effective_price

    # ------------------------------------------------------------------
    # Unwind — same as static loop, plus close hedge.
    # ------------------------------------------------------------------

    def _unwind_with_hedge(self) -> list[ActionToTake]:
        pt = self._pt
        morpho = self._morpho
        hedge = self._hedge
        collat = morpho._internal_state.collateral
        debt = morpho._internal_state.debt

        if morpho.is_liquidated:
            morpho._internal_state.collateral = 0.0
            morpho._internal_state.debt = 0.0
            self._state = "unwound"
            # Close hedge — accrued_pnl stays on the entity, withdrawing
            # zeroes the notional cleanly.
            return [self._hedge_close()]

        pt._internal_state.pt_face_amount += collat
        morpho._internal_state.collateral = 0.0

        actions: list[ActionToTake] = [
            ActionToTake(
                entity_name=PT_SLOT,
                action=Action("redeem", {"amount_in_face": collat}),
            ),
        ]
        pt._internal_state.cash -= debt
        morpho._internal_state.debt = 0.0

        # Close hedge — withdraw notional. Realised PnL stays on
        # hedge.accrued_pnl, contributing to total equity via balance.
        actions.append(self._hedge_close())

        self._state = "unwound"
        return actions

    def _hedge_close(self) -> ActionToTake:
        notional = self._hedge.internal_state.notional
        return ActionToTake(
            entity_name=HEDGE_SLOT,
            action=Action("withdraw", {"amount_in_notional": notional}),
        )


__all__ = ["HedgedLoopParams", "HedgedLoopStrategy"]
