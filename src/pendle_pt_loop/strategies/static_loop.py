"""Static-LTV Pendle PT recursive-loop strategy — Session 4.

This strategy implements the classic looped-leverage trade against a
single Pendle PT epoch with Morpho as the borrow venue:

1. **Day 1 (first tick after warmup):** Execute an N-cycle loop.

   For each cycle k = 0, ..., N-1, starting from
   :math:`C_0 = \\mathrm{INITIAL\\_BALANCE}`:

   a. Buy PT with :math:`C_k` USDC.
   b. Move the PT face out of the PT entity and post it as Morpho
      collateral.
   c. Borrow :math:`C_{k+1} = L \\cdot \\mathrm{collateral\\_value}_k`
      USDC against the freshly-posted collateral, where :math:`L` is the
      target LTV (below the Morpho LLTV — safety buffer).
   d. The borrowed USDC lands back in our PT-entity cash bucket and is
      reused as the next cycle's input.

   Under an ideal pool (no fee, no slippage) one has
   :math:`\\mathrm{collateral\\_value}_k = C_k`, so the total collateral
   face posted across the N cycles is the geometric sum

   .. math::

       F_{\\mathrm{total}}
         = \\frac{1}{p_{PT}} \\sum_{k=0}^{N-1} L^{k} \\cdot
           \\mathrm{INITIAL}
         = \\frac{\\mathrm{INITIAL}}{p_{PT}} \\cdot \\frac{1 - L^{N}}{1 - L}.

   The realised effective leverage at the limit :math:`N \\to \\infty`
   is :math:`1/(1 - L)`; with :math:`L = 0.8` and :math:`N = 5`,
   :math:`(1 - L^5)/(1 - L) \\approx 3.36`.

2. **Idle middle:** The strategy returns ``[]`` per tick. Time
   evolution lives inside the entities themselves — ``MorphoEntity``
   accrues debt and ``PendlePTEntity`` updates pt_price drift in their
   own ``update_state`` methods (which the fractal engine invokes
   *before* :meth:`predict` on each step).

3. **Expiry day:** Unwind, materialise final equity as PT-entity cash,
   advance to the ``"unwound"`` terminal state.

State machine
-------------

::

    uninvested  --(warmup ticks elapsed, expiry not yet reached)-->  open
    open        --(seconds_to_expiry == 0)-->                        unwound
    unwound     --(anything)-->                                      unwound

Tech debt (intentionally accepted this session)
-----------------------------------------------

1. ``ActionToTake`` dispatches one entity action per call but the loop
   wants the PT face to travel atomically from the PT entity into
   Morpho collateral (and the borrowed USDC to travel back). Until the
   PT entity grows a ``action_release(amount_in_face)`` action and the
   Morpho entity exposes a ``action_credit(amount_in_notional)`` action
   for received-borrow USDC, we bridge with direct ``_internal_state``
   mutations inside :meth:`predict`.

   TODO(Session 5): refactor PT entity to expose
   ``action_release(amount_in_face)`` so the PT->Morpho transfer
   becomes a proper action sequence; same for crediting received
   borrow back into the PT entity's cash.

2. The expiry unwind is implemented as a "flash repay" — collateral
   is teleported back to the PT entity, redeemed 1:1 for cash, and
   the debt is subtracted from cash in the same predict() call. In
   reality the trader needs either a flash-loan (Maker-style) or a
   PT-Sell-and-Repay sequence (sell PT for USDC, repay debt, withdraw
   collateral). The flash-repay shortcut delivers the right *final*
   equity but skips the AMM slippage one would actually pay at
   unwind — fine for static analysis where the AMM is ideal.

   TODO(Session 5/6): replace flash unwind with explicit Morpho repay
   sequence + a flash-loan modelling helper.

All timestamps are UTC; rates are annualised decimals.
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
    MorphoConfig,
    MorphoEntity,
    PendlePTConfig,
    PendlePTEntity,
)
from pendle_pt_loop.observations import MORPHO_SLOT, PT_SLOT


@dataclass
class StaticLoopParams(BaseStrategyParams):
    """Hyperparameters for the static-LTV PT loop.

    Attributes:
        INITIAL_BALANCE: USDC notional injected into the PT entity at
            position open.
        TARGET_LTV: Loan-to-value (debt / collateral_value) targeted
            after each cycle's borrow. Must be strictly below the
            Morpho LLTV (default 0.86) to keep a safety buffer; the
            strategy will assert this at ``set_up`` against the
            configured lltv on the registered Morpho entity.
        N_CYCLES: Number of buy-deposit-borrow cycles to execute at
            open. Effective leverage at the limit is :math:`1/(1-L)`.
        WARMUP_OBSERVATIONS: Number of leading observations during
            which the strategy returns ``[]`` to let the entities
            absorb their first state (Morpho's first ``update_state``
            does no accrual because there is no prior timestamp; we
            still want it to populate global state cleanly before we
            commit capital).
    """

    INITIAL_BALANCE: float = 10_000.0
    TARGET_LTV: float = 0.80
    N_CYCLES: int = 5
    WARMUP_OBSERVATIONS: int = 1


class StaticLoopStrategy(BaseStrategy[StaticLoopParams]):
    """Fixed-LTV recursive PT loop with deterministic open / unwind."""

    # Both PT and Morpho global states arrive every tick — strict
    # observations is the correct setting; a missing state is a bug.
    STRICT_OBSERVATIONS: bool = True

    _state: Literal["uninvested", "open", "unwound"]
    _observations_seen: int

    def __init__(
        self,
        *,
        pt_config: PendlePTConfig | None = None,
        morpho_config: MorphoConfig | None = None,
        params: StaticLoopParams | dict | None = None,
        debug: bool = False,
    ) -> None:
        # Stash entity configs so ``set_up`` (called by super().__init__)
        # can construct them. We cannot rely on default-constructed
        # entities for real backtests — they hard-code default fees /
        # LLTV that may differ from the live market.
        self._pt_config = pt_config
        self._morpho_config = morpho_config
        super().__init__(params=params, debug=debug)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def set_up(self) -> None:
        """Register PT and Morpho entities under the slot names that
        :mod:`pendle_pt_loop.observations` writes into."""
        self._state = "uninvested"
        self._observations_seen = 0
        pt = PendlePTEntity(self._pt_config)
        morpho = MorphoEntity(self._morpho_config)
        # Sanity: the safety buffer must be real. If the user passed a
        # target LTV at or above the LLTV, the very first borrow on the
        # first cycle would raise inside the Morpho entity — fail fast.
        if self._params.TARGET_LTV >= morpho._config.lltv:
            raise ValueError(
                f"TARGET_LTV={self._params.TARGET_LTV} must be strictly "
                f"below Morpho lltv={morpho._config.lltv}"
            )
        self.register_entity(NamedEntity(entity_name=PT_SLOT, entity=pt))
        self.register_entity(NamedEntity(entity_name=MORPHO_SLOT, entity=morpho))

    # ------------------------------------------------------------------
    # Entity accessors (typed views over ``get_entity``).
    # ------------------------------------------------------------------

    @property
    def _pt(self) -> PendlePTEntity:
        # ``get_entity`` returns BaseEntity; cast for the type checker.
        return self.get_entity(PT_SLOT)  # type: ignore[return-value]

    @property
    def _morpho(self) -> MorphoEntity:
        return self.get_entity(MORPHO_SLOT)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Predict — state-machine dispatch.
    # ------------------------------------------------------------------

    def predict(self) -> list[ActionToTake]:
        """Dispatch on the strategy state.

        The fractal engine has already called ``update_state`` on both
        entities for the current tick when ``predict`` runs (see
        ``BaseStrategy.step``), so :attr:`PendlePTEntity.global_state`
        and :attr:`MorphoEntity.global_state` are current.
        """
        # Counter is 1-indexed for human readability: at the first
        # predict() call this is 1, etc. The strict less-or-equal
        # comparison below means ``WARMUP_OBSERVATIONS=1`` skips
        # exactly one observation (the standard case — Morpho's
        # first update_state populates state but cannot accrue,
        # so we wait one extra tick before committing capital).
        self._observations_seen += 1

        # Terminal — nothing left to do.
        if self._state == "unwound":
            return []

        # Cache once; both branches need it.
        pt_global = self._pt.global_state
        at_expiry = pt_global.seconds_to_expiry <= 0

        if self._state == "uninvested":
            # Honour warmup. Also: never open at/after expiry — there is
            # no carry left to capture; the strategy degenerates.
            if self._observations_seen <= self._params.WARMUP_OBSERVATIONS:
                return []
            if at_expiry:
                self._state = "unwound"
                return []
            return self._open_loop()

        # state == "open"
        if at_expiry:
            return self._unwind()

        # Open and pre-expiry: idle.
        return []

    # ------------------------------------------------------------------
    # Open: N cycles of buy-PT, transfer-to-collateral, borrow-back.
    # ------------------------------------------------------------------

    def _open_loop(self) -> list[ActionToTake]:
        """Execute all N cycles in one predict() call.

        Action ordering within the returned list matters: fractal
        executes them in order. The PT->Morpho face transfer is
        handled with a single up-front mutation that pre-debits the
        full sum of PT face we will buy across the N cycles (so that
        each cycle's buy_pt + Morpho-side deposit net to zero on the
        PT entity by the end of the engine pass). The borrow->PT cash
        leg is handled with explicit ``deposit`` actions, which gives
        the engine a chance to credit the wallet between actions and
        keeps ``action_buy_pt``'s cash sufficiency check happy.
        """
        pt_price = self._pt.global_state.pt_price
        face_per_cycle, borrow_per_cycle = self._plan_cycles(pt_price)
        self._pre_debit_pt_face(sum(face_per_cycle))

        actions: list[ActionToTake] = [self._deposit_action(self._params.INITIAL_BALANCE)]
        cycle_capital = self._params.INITIAL_BALANCE
        for face_received, borrow_amount in zip(face_per_cycle, borrow_per_cycle):
            actions.extend(self._cycle_actions(cycle_capital, face_received, borrow_amount))
            cycle_capital = borrow_amount

        self._state = "open"
        return actions

    def _plan_cycles(
        self, pt_price: float
    ) -> tuple[list[float], list[float]]:
        """Pre-compute (face_received, borrow_amount) for each cycle.

        Mirrors the geometric series exactly under ideal-pool config;
        under non-trivial AMM impact it remains exact because every
        cycle's parameters are deterministic once ``pt_price`` is known.
        """
        face_per_cycle: list[float] = []
        borrow_per_cycle: list[float] = []
        cycle_capital = self._params.INITIAL_BALANCE
        for _ in range(self._params.N_CYCLES):
            face_received = self._estimate_pt_face_received(
                cycle_capital, pt_price
            )
            collat_value_added = face_received * pt_price
            borrow_amount = self._params.TARGET_LTV * collat_value_added
            face_per_cycle.append(face_received)
            borrow_per_cycle.append(borrow_amount)
            cycle_capital = borrow_amount
        return face_per_cycle, borrow_per_cycle

    def _pre_debit_pt_face(self, total_face: float) -> None:
        """Pre-debit the sum of PT face we will buy across the loop.

        By end-of-engine-pass each cycle's ``buy_pt`` will have
        added F_k back, and each Morpho-side deposit will have consumed
        F_k (in the Morpho entity), so the PT entity ends with
        pt_face_amount == 0. The PT entity is allowed to hold a
        transiently negative pt_face mid-execution; no entity action
        consults the sign of ``pt_face_amount`` as a precondition other
        than ``action_sell_pt`` / ``action_redeem`` (we do not call
        either inside the open loop).

        TODO(Session 5): replace this hack with a proper
        ``pt.action_release(amount_in_face)`` action that explicitly
        debits PT face. The mutation is the simplest way to preserve
        total-equity conservation under the current entity API.
        """
        self._pt._internal_state.pt_face_amount -= total_face

    def _cycle_actions(
        self,
        cycle_capital: float,
        face_received: float,
        borrow_amount: float,
    ) -> list[ActionToTake]:
        """One cycle's four-step action sequence.

        Steps (in execution order):
            a. ``buy_pt(cycle_capital)`` — swap USDC -> PT.
            b. Morpho ``deposit(face_received)`` — post collateral.
            c. Morpho ``borrow(borrow_amount)`` — draw at TARGET_LTV.
            d. PT ``deposit(borrow_amount)`` — mirror the wallet credit
               from the borrow leg. TODO(Session 5): replace with a
               proper Morpho->wallet transfer once a wallet entity exists.
        """
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
            self._deposit_action(borrow_amount),
        ]

    @staticmethod
    def _deposit_action(amount: float) -> ActionToTake:
        """``ActionToTake`` for a PT-entity USDC deposit of ``amount``."""
        return ActionToTake(
            entity_name=PT_SLOT,
            action=Action("deposit", {"amount_in_notional": amount}),
        )

    def _estimate_pt_face_received(
        self, amount_in_notional: float, pt_price: float
    ) -> float:
        """Replicate ``PendlePTEntity.action_buy_pt`` math.

        The strategy needs to know how much PT face will land in the
        entity *now*, because the subsequent transfer-to-Morpho
        mutation and borrow sizing both depend on it. Keeping this in
        sync with the entity is fragile (see tech-debt note #1); the
        targeted Session-5 refactor (``action_release``) eliminates the
        coupling.
        """
        cfg = self._pt._config
        pool = self._pt.global_state.pool_liquidity
        effective_in = amount_in_notional * (1.0 - cfg.amm_fee_rate)
        slip = cfg.slippage_factor * amount_in_notional / pool
        effective_price = pt_price * (1.0 + slip)
        return effective_in / effective_price

    # ------------------------------------------------------------------
    # Unwind: flash-repay at expiry.
    # ------------------------------------------------------------------

    def _unwind(self) -> list[ActionToTake]:
        """Materialise final equity into PT-entity cash at expiry.

        At expiry the PT entity's update_state has snapped
        ``pt_price`` to 1.0, so ``collateral_value == collateral`` and
        the final equity is

        .. math::

            E_{\\mathrm{final}}
              = \\mathrm{collateral} - \\mathrm{debt} + \\mathrm{cash}_{PT}.

        We deliver this by:

        1. Teleporting all Morpho collateral back into the PT entity's
           ``pt_face_amount`` (tech-debt #2).
        2. Emitting a ``redeem`` action to convert the face into cash
           at par (PT entity's redeem at expiry is 1:1).
        3. Subtracting the debt from the PT entity's cash (tech-debt
           #2) and zeroing the Morpho debt to reflect repayment.

        We also handle the liquidated case: if the Morpho position was
        flagged liquidated earlier in the run, collateral is treated
        as seized and only the residual PT cash + any leftover
        un-redeemed face remains.
        """
        pt = self._pt
        morpho = self._morpho
        collat = morpho._internal_state.collateral
        debt = morpho._internal_state.debt

        if morpho.is_liquidated:
            # Liquidation: collateral is gone (in our simplified model),
            # debt is wiped against the seized collateral. Whatever
            # cash is in the PT entity is the realised equity.
            morpho._internal_state.collateral = 0.0
            morpho._internal_state.debt = 0.0
            self._state = "unwound"
            return []

        # Pull collateral home (tech-debt #2).
        pt._internal_state.pt_face_amount += collat
        morpho._internal_state.collateral = 0.0

        actions: list[ActionToTake] = []
        # Redeem 1:1 — works because update_state has set pt_price = 1
        # and seconds_to_expiry = 0 by the time predict() runs.
        actions.append(
            ActionToTake(
                entity_name=PT_SLOT,
                action=Action(
                    "redeem", {"amount_in_face": collat}
                ),
            )
        )

        # Pay debt out of PT cash (tech-debt #2). We mutate after
        # queueing the redeem because the redeem action runs *after*
        # predict returns — at that point pt_face_amount.collat will
        # have moved into pt.cash, making the subtraction valid in
        # accounting terms. The strict timing within predict() does
        # not matter because cash is a scalar; the engine treats
        # actions as a sequence.
        #
        # NOTE: we adjust cash here for clarity; an equivalent design
        # would queue a synthetic ``action_withdraw`` and have an
        # external "USDC wallet" entity. Session 5/6 cleanup.
        pt._internal_state.cash -= debt
        morpho._internal_state.debt = 0.0

        self._state = "unwound"
        return actions


__all__ = ["StaticLoopParams", "StaticLoopStrategy"]
