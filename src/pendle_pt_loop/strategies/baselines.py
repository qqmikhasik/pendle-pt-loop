"""Baseline strategies for the PT-loop research.

Three "do-nothing-clever" comparison strategies that the loop variants
must beat (risk-adjusted) for the project to claim anything. Each
registers a single :class:`PendlePTEntity` under the ``"PT"`` slot
expected by :mod:`pendle_pt_loop.observations`.

Strategies
----------
:class:`HoldUSDCStrategy`
    Pure cash baseline. Deposits ``INITIAL_BALANCE`` USDC into the PT
    entity on the first tick, then never trades. The PT entity's
    ``cash`` field carries the balance forward unchanged. Equity stays
    flat at ``INITIAL_BALANCE`` modulo zero — it is the literal floor.

:class:`HoldSUSDeStrategy`
    "What if we just held sUSDe?" proxy. We do not pull a separate
    sUSDe APY feed; instead we treat the PT's quoted **implied yield**
    as the proxy for realised sUSDe yield. Rationale: in an efficient
    Pendle market, the implied yield baked into the PT discount equals
    the market's central expectation of realised SY yield over the
    remaining tenor. Any deviation is exactly the alpha the *real*
    loop strategies aim to exploit; the baseline is "what a market-
    consensus holder gets". Mechanically, on every tick we credit
    ``cash * implied_yield * dt`` to the PT entity via a deposit
    action, where ``dt = (prev_seconds_to_expiry - current_seconds_to_expiry)
    / SECONDS_PER_YEAR``. The PT entity is therefore (ab)used as a
    glorified yield-bearing cash account in this strategy — no PT is
    ever bought.

:class:`HoldPTNoLeverageStrategy`
    Straight "buy PT and hold to expiry" baseline. On the first tick
    deposits ``INITIAL_BALANCE`` and buys PT with the entire amount;
    then idles; redeems 1:1 at expiry. Captures the AMM friction (fee
    + slippage on the one buy) but not borrowing costs. This is the
    natural "fixed-yield product" benchmark.

State tracking is via small private flags on each strategy
(``_opened``, ``_redeemed``, ``_last_seconds_to_expiry``) — no entity
mutation outside the documented action surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from fractal.core.base import (
    Action,
    ActionToTake,
    BaseStrategy,
    BaseStrategyParams,
    NamedEntity,
)

from pendle_pt_loop.entities import PendlePTEntity
from pendle_pt_loop.entities.pendle_pt import SECONDS_PER_YEAR
from pendle_pt_loop.observations import PT_SLOT


@dataclass
class BaselineParams(BaseStrategyParams):
    """Hyperparameters shared by all baseline strategies.

    Attributes:
        INITIAL_BALANCE: USDC notional deposited into the PT entity on
            the very first tick. Used as the strategy's starting equity
            in all three baselines.
    """

    INITIAL_BALANCE: float = 10_000.0


# ----------------------------------------------------------------------
# HoldUSDC
# ----------------------------------------------------------------------


class HoldUSDCStrategy(BaseStrategy[BaselineParams]):
    """Pure cash baseline — equity stays at ``INITIAL_BALANCE``.

    The PT entity is registered solely to give us a place to park
    USDC. No PT is ever bought; ``balance`` therefore equals ``cash``
    forever and is constant at ``INITIAL_BALANCE``.
    """

    def set_up(self) -> None:
        self._opened: bool = False
        self.register_entity(NamedEntity(PT_SLOT, PendlePTEntity()))

    def predict(self) -> list[ActionToTake]:
        if self._opened:
            return []
        self._opened = True
        return [
            ActionToTake(
                PT_SLOT,
                Action("deposit", {"amount_in_notional": self._params.INITIAL_BALANCE}),
            )
        ]


# ----------------------------------------------------------------------
# HoldSUSDe
# ----------------------------------------------------------------------


class HoldSUSDeStrategy(BaseStrategy[BaselineParams]):
    """sUSDe-proxy baseline — accrue implied yield on parked cash.

    Implementation note: the PT entity has no first-class "yield
    accrual" action, so we credit the per-tick accrual by issuing a
    ``deposit`` of ``cash * implied_yield * dt`` USDC. This is a
    *modelling trick* — the entity does not know its cash is
    "earning"; the strategy is responsible for the bookkeeping. ``dt``
    is derived from the change in ``seconds_to_expiry`` between
    consecutive observations, which is well-defined (monotonically
    decreasing) and avoids needing the wall-clock timestamp inside
    ``predict``.
    """

    def set_up(self) -> None:
        self._opened: bool = False
        self._last_seconds_to_expiry: float | None = None
        self.register_entity(NamedEntity(PT_SLOT, PendlePTEntity()))

    def predict(self) -> list[ActionToTake]:
        pt = self.get_entity(PT_SLOT)
        actions: list[ActionToTake] = []
        if not self._opened:
            self._opened = True
            actions.append(
                ActionToTake(
                    PT_SLOT,
                    Action(
                        "deposit",
                        {"amount_in_notional": self._params.INITIAL_BALANCE},
                    ),
                )
            )
        accrual = self._compute_accrual(pt)
        if accrual > 0:
            actions.append(
                ActionToTake(
                    PT_SLOT,
                    Action("deposit", {"amount_in_notional": accrual}),
                )
            )
        self._last_seconds_to_expiry = pt.global_state.seconds_to_expiry
        return actions

    def _compute_accrual(self, pt: PendlePTEntity) -> float:
        """USDC to credit this tick: ``cash * implied_yield * dt``."""
        gs = pt.global_state
        if self._last_seconds_to_expiry is None:
            return 0.0
        dt_seconds = self._last_seconds_to_expiry - gs.seconds_to_expiry
        if dt_seconds <= 0:
            return 0.0
        dt_years = dt_seconds / SECONDS_PER_YEAR
        # On the very first tick the deposit has not yet executed, so
        # ``pt.internal_state.cash`` is still 0 — but we have already set
        # ``self._opened = True``. The first non-zero accrual therefore
        # fires on the second tick, which is the correct semantics
        # (no yield until cash is actually in the account).
        cash = pt.internal_state.cash
        if cash <= 0:
            return 0.0
        return cash * gs.implied_yield * dt_years


# ----------------------------------------------------------------------
# HoldPTNoLeverage
# ----------------------------------------------------------------------


class HoldPTNoLeverageStrategy(BaseStrategy[BaselineParams]):
    """Buy PT once at entry, redeem at expiry — no leverage.

    Lifecycle flags:

    * ``_opened`` — has the initial deposit+buy been executed?
    * ``_redeemed`` — has the redeem at expiry been executed?
    """

    def set_up(self) -> None:
        self._opened: bool = False
        self._redeemed: bool = False
        self.register_entity(NamedEntity(PT_SLOT, PendlePTEntity()))

    def predict(self) -> list[ActionToTake]:
        pt = self.get_entity(PT_SLOT)
        if not self._opened:
            return self._open_position()
        if not self._redeemed and pt.global_state.seconds_to_expiry <= 0:
            return self._close_position(pt)
        return []

    def _open_position(self) -> list[ActionToTake]:
        """Deposit ``INITIAL_BALANCE`` and immediately spend it on PT."""
        self._opened = True
        amount = self._params.INITIAL_BALANCE
        return [
            ActionToTake(
                PT_SLOT, Action("deposit", {"amount_in_notional": amount})
            ),
            ActionToTake(
                PT_SLOT, Action("buy_pt", {"amount_in_notional": amount})
            ),
        ]

    def _close_position(self, pt: PendlePTEntity) -> list[ActionToTake]:
        """Redeem the full PT face position once expiry has hit."""
        self._redeemed = True
        face = pt.internal_state.pt_face_amount
        if face <= 0:
            return []
        return [
            ActionToTake(
                PT_SLOT, Action("redeem", {"amount_in_face": face})
            )
        ]


__all__ = [
    "BaselineParams",
    "HoldUSDCStrategy",
    "HoldSUSDeStrategy",
    "HoldPTNoLeverageStrategy",
]
