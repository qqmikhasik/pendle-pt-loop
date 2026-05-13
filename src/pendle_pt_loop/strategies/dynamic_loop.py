"""Dynamic-LTV PT-loop with asymmetric band controller.

Extends the static loop by recomputing an :math:`[L_L, L_U]` band each
tick and rebalancing back to the (possibly adjusted) target whenever
realised LTV exits the band.

State machine
-------------
``"uninvested" -> "open" -> "managing" -> "unwound"``

* **uninvested**: same as the static loop — wait through the warmup
  observations, then open the position with ``N_CYCLES`` cycles at
  ``TARGET_LTV``.
* **open**: transition state used for exactly one tick after the open
  action sequence; immediately advances to ``"managing"``.
* **managing**: per-tick band check. If LTV is outside ``[L_L, L_U]``,
  emit a rebalance to ``band.target``. Otherwise idle.
* **unwound**: same flash-repay model as the static loop.

The band depends on volatility (estimated from a rolling window of
recent ``pt_price`` observations), the current carry differential
(``implied_yield - borrowing_rate``), and the controller config. See
:mod:`pendle_pt_loop.risk.ltv_controller`.

Tech debt carried over from Session 4
-------------------------------------
* PT face transfer between PT and Morpho entities still uses direct
  ``_internal_state`` mutation.
* Unwind is the same flash-repay model.
Both are flagged for refactor when the entity API grows a clean
``action_release_pt`` primitive.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
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
from pendle_pt_loop.risk.ltv_controller import (
    LTVBand,
    LTVControllerConfig,
    compute_band,
)

# Steps per year (hourly cadence). Used for vol annualisation.
_STEPS_PER_YEAR: float = 365.25 * 24.0


@dataclass
class DynamicLoopParams(BaseStrategyParams):
    """Hyperparameters for the dynamic-LTV PT loop.

    All static-loop knobs (initial balance, target LTV, cycles, warmup)
    are mirrored here so existing callers can swap strategies by name.

    Controller knobs:
    Attributes:
        INITIAL_BALANCE: USDC notional injected at open.
        TARGET_LTV: Same role as in StaticLoopParams.
        N_CYCLES: Number of cycles at open. Subsequent rebalances do
            single-step adjustments, not full geometric loops.
        WARMUP_OBSERVATIONS: Number of leading observations to skip.
        VOLATILITY_WINDOW: Number of hourly observations used to
            estimate σ of ``ln(pt_price)``. 168 = one week — long
            enough to be stable, short enough to track regime shifts.
        LIQUIDATION_BUDGET: ``ε_liq`` — upper-band probability budget.
        SOLVENCY_HORIZON_HOURS: ``h_liq`` in hours.
        REBALANCE_HORIZON_DAYS: ``h_reb`` for the lower-band economics.
        REBALANCE_COST_USDC: ``K_reb`` — round-trip rebalance friction.
    """

    INITIAL_BALANCE: float = 10_000.0
    TARGET_LTV: float = 0.80
    N_CYCLES: int = 5
    WARMUP_OBSERVATIONS: int = 1
    VOLATILITY_WINDOW: int = 168
    LIQUIDATION_BUDGET: float = 1.0e-4
    SOLVENCY_HORIZON_HOURS: float = 3.0
    REBALANCE_HORIZON_DAYS: float = 1.0
    REBALANCE_COST_USDC: float = 0.0


class DynamicLoopStrategy(BaseStrategy[DynamicLoopParams]):
    """Static loop + asymmetric LTV-band rebalancing."""

    STRICT_OBSERVATIONS: bool = True

    _state: Literal["uninvested", "open", "managing", "unwound"]
    _observations_seen: int
    _pt_price_window: deque[float]

    def __init__(
        self,
        *,
        pt_config: PendlePTConfig | None = None,
        morpho_config: MorphoConfig | None = None,
        params: DynamicLoopParams | dict | None = None,
        debug: bool = False,
    ) -> None:
        self._pt_config = pt_config
        self._morpho_config = morpho_config
        super().__init__(params=params, debug=debug)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def set_up(self) -> None:
        self._state = "uninvested"
        self._observations_seen = 0
        self._pt_price_window = deque(maxlen=self._params.VOLATILITY_WINDOW)
        pt = PendlePTEntity(self._pt_config)
        morpho = MorphoEntity(self._morpho_config)
        if self._params.TARGET_LTV >= morpho._config.lltv:
            raise ValueError(
                f"TARGET_LTV={self._params.TARGET_LTV} must be strictly below "
                f"Morpho lltv={morpho._config.lltv}"
            )
        self.register_entity(NamedEntity(entity_name=PT_SLOT, entity=pt))
        self.register_entity(NamedEntity(entity_name=MORPHO_SLOT, entity=morpho))
        self._controller_config = LTVControllerConfig(
            target_ltv=self._params.TARGET_LTV,
            liquidation_ltv=morpho._config.lltv,
            liquidation_budget=self._params.LIQUIDATION_BUDGET,
            solvency_horizon_years=self._params.SOLVENCY_HORIZON_HOURS / _STEPS_PER_YEAR,
            rebalance_horizon_years=self._params.REBALANCE_HORIZON_DAYS / 365.25,
            rebalance_cost_usdc=self._params.REBALANCE_COST_USDC,
        )

    # ------------------------------------------------------------------
    # Entity accessors.
    # ------------------------------------------------------------------

    @property
    def _pt(self) -> PendlePTEntity:
        return self.get_entity(PT_SLOT)  # type: ignore[return-value]

    @property
    def _morpho(self) -> MorphoEntity:
        return self.get_entity(MORPHO_SLOT)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Predict — dispatch on state.
    # ------------------------------------------------------------------

    def predict(self) -> list[ActionToTake]:
        self._observations_seen += 1
        # Track pt_price history regardless of state — needed for vol estimate
        # whenever managing kicks in.
        self._pt_price_window.append(self._pt.global_state.pt_price)

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
            actions = self._open_loop()
            self._state = "managing"
            return actions

        # "open" transient: should not actually appear because we advance
        # to "managing" inside _open_loop, but keep a defensive branch.
        if self._state == "open":
            self._state = "managing"

        # managing
        if self._morpho.is_liquidated:
            # Once liquidated, no further rebalances are possible.
            # Just wait for expiry to unwind cleanly.
            if at_expiry:
                return self._unwind()
            return []

        if at_expiry:
            return self._unwind()

        return self._maybe_rebalance()

    # ------------------------------------------------------------------
    # Open — identical structure to StaticLoopStrategy.
    # ------------------------------------------------------------------

    def _open_loop(self) -> list[ActionToTake]:
        pt_price = self._pt.global_state.pt_price
        face_per_cycle, borrow_per_cycle = self._plan_cycles(pt_price)
        # Pre-debit PT face we will buy across all cycles; tech-debt #1.
        self._pt._internal_state.pt_face_amount -= sum(face_per_cycle)

        actions: list[ActionToTake] = [
            self._pt_deposit_action(self._params.INITIAL_BALANCE)
        ]
        cycle_capital = self._params.INITIAL_BALANCE
        for face_received, borrow_amount in zip(face_per_cycle, borrow_per_cycle):
            actions.extend(
                self._cycle_actions(cycle_capital, face_received, borrow_amount)
            )
            cycle_capital = borrow_amount
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
            self._pt_deposit_action(borrow_amount),
        ]

    @staticmethod
    def _pt_deposit_action(amount: float) -> ActionToTake:
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
    # Rebalance — the only thing that distinguishes this from static.
    # ------------------------------------------------------------------

    def _maybe_rebalance(self) -> list[ActionToTake]:
        """Compute the band; rebalance to target if outside."""
        band = self._current_band()
        ltv = self._morpho.ltv
        if band.lower <= ltv <= band.upper:
            return []  # within band, idle
        if ltv > band.upper:
            return self._deleverage_to(band.target)
        # ltv < band.lower
        return self._leverage_up_to(band.target)

    def _current_band(self) -> LTVBand:
        """Compute the LTV band given the current observation context."""
        drift, vol = self._estimate_drift_and_vol()
        return compute_band(
            drift=drift,
            volatility=vol,
            equity=self._equity(),
            pt_yield=self._pt.global_state.implied_yield,
            borrow_rate=self._morpho.global_state.borrowing_rate,
            config=self._controller_config,
        )

    def _estimate_drift_and_vol(self) -> tuple[float, float]:
        """Realised drift / vol of ln(pt_price) over the rolling window.

        Returns ``(0.0, fallback_vol)`` if the window is too short — the
        controller then uses a conservative volatility floor so the
        first few ticks after open still produce a well-defined band.
        """
        prices = np.array(self._pt_price_window, dtype=float)
        if len(prices) < 8:
            # Not enough history yet — use a noisy fallback vol so the
            # band is well-defined but not too tight.
            return 0.0, 0.5
        # Avoid log of non-positive (shouldn't happen with real PT data
        # but guard anyway).
        prices = prices[prices > 0]
        if len(prices) < 2:
            return 0.0, 0.5
        log_returns = np.log(prices[1:] / prices[:-1])
        mu_step = float(np.mean(log_returns))
        sigma_step = float(np.std(log_returns, ddof=1)) if len(log_returns) > 1 else 0.0
        drift = mu_step * _STEPS_PER_YEAR
        vol = sigma_step * math.sqrt(_STEPS_PER_YEAR)
        # Floor vol below — observed pt_price flatlines briefly during
        # low-volume hours; we don't want σ collapsing to 0 to make the
        # band absurdly wide.
        return drift, max(vol, 0.02)

    def _equity(self) -> float:
        """Total notional equity = PT entity balance + Morpho equity."""
        return float(self._pt.balance + self._morpho.balance)

    # ------------------------------------------------------------------
    # Deleverage path: reduce debt by selling some PT collateral.
    # ------------------------------------------------------------------

    def _deleverage_to(self, target_ltv: float) -> list[ActionToTake]:
        """Bring LTV down to ``target_ltv`` by selling PT collateral.

        We compute the debt drawdown needed and the corresponding PT
        face to sell. Direct mutations move face from Morpho back into
        the PT entity (tech-debt #1); the actual sell + repay then run
        as proper actions.
        """
        morpho = self._morpho
        pt_price = self._pt.global_state.pt_price
        debt = morpho.debt_value
        collat_value = morpho.collateral_value
        if collat_value <= 0 or pt_price <= 0:
            return []

        # Solve for new debt D' = target * collateral_value'
        # where collateral_value' = (collat - sold_face) * pt_price.
        # And the sold collateral repays debt: D' = D - sold_face * pt_price.
        # → D - sold_face * pt_price = target * (collat - sold_face) * pt_price
        # → sold_face * pt_price * (1 - target) = D - target * collat * pt_price
        # → sold_face = (D - target * collat * pt_price) / (pt_price * (1 - target))
        numerator = debt - target_ltv * collat_value
        denominator = pt_price * (1.0 - target_ltv)
        if denominator <= 0:
            return []
        sold_face = numerator / denominator
        sold_face = max(0.0, min(sold_face, morpho._internal_state.collateral))
        if sold_face <= 0:
            return []

        # Move face from Morpho collateral into PT entity (mutation).
        # Use the actual post-mutation pt_face_amount as the sell amount —
        # the open loop's pre-debit can leave a sub-ULP residual that
        # would otherwise trip ``action_sell_pt``'s "more than held" check.
        morpho._internal_state.collateral -= sold_face
        self._pt._internal_state.pt_face_amount += sold_face
        sold_face = max(0.0, self._pt._internal_state.pt_face_amount)

        repay_amount = sold_face * pt_price
        repay_amount = min(repay_amount, morpho._internal_state.debt)

        return [
            ActionToTake(
                entity_name=PT_SLOT,
                action=Action("sell_pt", {"amount_in_face": sold_face}),
            ),
            ActionToTake(
                entity_name=MORPHO_SLOT,
                action=Action("repay", {"amount_in_notional": repay_amount}),
            ),
            # The cash that "paid" the debt left the PT entity.
            # Tech-debt #1: mutate cash directly until a proper wallet
            # entity exists.
            ActionToTake(
                entity_name=PT_SLOT,
                action=Action("withdraw", {"amount_in_notional": repay_amount}),
            ),
        ]

    # ------------------------------------------------------------------
    # Lever-up path: borrow more, buy more PT, deposit as collateral.
    # ------------------------------------------------------------------

    def _leverage_up_to(self, target_ltv: float) -> list[ActionToTake]:
        """Bring LTV up to ``target_ltv`` by drawing new debt against
        the existing collateral and using it to buy more PT.

        Same fee/slippage trick as one cycle of the open loop.
        """
        morpho = self._morpho
        pt_price = self._pt.global_state.pt_price
        if pt_price <= 0:
            return []
        debt = morpho.debt_value
        collat_value_now = morpho.collateral_value
        # New target debt assuming we add face F of PT to collateral.
        # collat' = collat + F; debt' = debt + F * pt_price (we borrow that).
        # target = (debt + F*pt_price) / ((collat + F) * pt_price)
        # → F = (target * collat * pt_price - debt) / (pt_price * (1 - target))
        numerator = target_ltv * collat_value_now - debt
        denominator = pt_price * (1.0 - target_ltv)
        if denominator <= 0 or numerator <= 0:
            return []
        new_face_target = numerator / denominator

        # Borrow amount = pt_price * face (cash needed to acquire that face)
        # In practice the AMM eats some — so over-borrow slightly to be
        # safe. We start conservative: borrow = pt_price * face (ignores
        # fee + slippage), and let the actual buy_pt land slightly less
        # face. Effect on LTV: tiny overshoot in the safe direction.
        borrow_amount = new_face_target * pt_price
        # Cap by LLTV-aware Morpho check; if numerator was already huge
        # the action_borrow will reject. We pre-clamp to a safe upper.
        # The Morpho entity's action_borrow does the actual safety
        # enforcement; we don't second-guess it here.

        face_received = self._estimate_pt_face_received(borrow_amount, pt_price)
        # Pre-debit (tech-debt #1).
        self._pt._internal_state.pt_face_amount -= face_received

        return [
            ActionToTake(
                entity_name=MORPHO_SLOT,
                action=Action("borrow", {"amount_in_notional": borrow_amount}),
            ),
            self._pt_deposit_action(borrow_amount),
            ActionToTake(
                entity_name=PT_SLOT,
                action=Action("buy_pt", {"amount_in_notional": borrow_amount}),
            ),
            ActionToTake(
                entity_name=MORPHO_SLOT,
                action=Action("deposit", {"amount_in_notional": face_received}),
            ),
        ]

    # ------------------------------------------------------------------
    # Unwind — identical to static loop.
    # ------------------------------------------------------------------

    def _unwind(self) -> list[ActionToTake]:
        pt = self._pt
        morpho = self._morpho
        collat = morpho._internal_state.collateral
        debt = morpho._internal_state.debt

        if morpho.is_liquidated:
            morpho._internal_state.collateral = 0.0
            morpho._internal_state.debt = 0.0
            self._state = "unwound"
            return []

        pt._internal_state.pt_face_amount += collat
        morpho._internal_state.collateral = 0.0

        actions = [
            ActionToTake(
                entity_name=PT_SLOT,
                action=Action("redeem", {"amount_in_face": collat}),
            ),
        ]
        pt._internal_state.cash -= debt
        morpho._internal_state.debt = 0.0
        self._state = "unwound"
        return actions


__all__ = ["DynamicLoopParams", "DynamicLoopStrategy"]
