"""Asymmetric LTV band controller for the dynamic PT loop.

Adapts the asymmetric :math:`\\alpha`-band from Krestenko's Vega Basis
Trading paper (2026) to the LTV (loan-to-value) ratio that governs our
Pendle/Morpho leveraged carry strategy.

Geometry
--------
The strategy targets a static LTV :math:`L^\\dagger \\in (0, L_{\\text{liq}})`
that the operator believes to be a good operating point (e.g. 0.80 with
:math:`L_{\\text{liq}} = 0.86` on Morpho PT-sUSDE markets). As PT
price drifts during the holding period, the realised LTV deviates from
:math:`L^\\dagger`. The controller defines an asymmetric band
:math:`[L_L, L_U]` around the target; when the realised LTV exits the
band, we rebalance back to :math:`L^\\dagger`.

* **Upper boundary** :math:`L_U` — *solvency-driven, structural*. This
  is the maximum LTV at which the probability of liquidation over a
  short operational horizon :math:`h_{\\text{liq}}` does not exceed a
  budget :math:`\\varepsilon_{\\text{liq}}`. Computed by inverting the
  first-passage formula in
  :mod:`pendle_pt_loop.risk.first_passage`. If realised LTV climbs
  above :math:`L_U`, we deleverage immediately.

  Default budget: :math:`\\varepsilon_{\\text{liq}} = 10^{-4}` over
  :math:`h_{\\text{liq}} = 3` hours (matches the documented Arbitrum
  sequencer-outage tail and Pendle Oracle TWAP latency).

* **Lower boundary** :math:`L_L` — *economic, may vanish*. As the LTV
  drifts below the target the position carries fewer dollars of PT per
  dollar of capital and the strategy gives up carry. Re-levering back
  to :math:`L^\\dagger` is worthwhile only if the recovered carry over
  the rebalance horizon :math:`h_{\\text{reb}}` exceeds the rebalance
  cost :math:`K_{\\text{reb}}` (gas + slippage on the swap+borrow+swap
  sequence). At first order the carry recovered by re-levering from
  :math:`L` back to :math:`L^\\dagger` over a horizon :math:`h` is

  .. math::

      \\Delta \\mathrm{carry} \\;\\approx\\; X \\;
        \\frac{r_{PT} - r_b}{(1 - L^\\dagger)^2} \\;
        (L^\\dagger - L) \\; h,

  where :math:`X` is the equity capital and :math:`r_{PT} - r_b` is
  the per-dollar-of-collateral carry differential (PT yield minus
  Morpho borrow rate). Setting this equal to :math:`K_{\\text{reb}}`
  gives

  .. math::

      L^\\dagger - L_L \\;=\\;
        \\frac{K_{\\text{reb}} \\, (1 - L^\\dagger)^2}
             {X \\, (r_{PT} - r_b) \\, h_{\\text{reb}}}.

  When the right-hand side exceeds :math:`L^\\dagger` (rebalancing
  never pays off at the current cost), :math:`L_L` is clamped to 0
  — equivalent to "never re-lever, always trade only when forced by
  the upper boundary".

Both boundaries depend on parameters that change over time (volatility
estimate, carry rate, gas cost), so the controller is **recomputed at
every observation** rather than once at open.
"""
from __future__ import annotations

from dataclasses import dataclass

from pendle_pt_loop.risk.first_passage import solve_max_ltv_for_budget


@dataclass
class LTVControllerConfig:
    """Static configuration of the dynamic LTV-band controller.

    Attributes:
        target_ltv: :math:`L^\\dagger` — the LTV the controller drives
            the position back to whenever it exits the band.
        liquidation_ltv: :math:`L_{\\text{liq}}` — Morpho market LLTV
            (e.g. 0.86 or 0.915 for PT-sUSDE).
        liquidation_budget: :math:`\\varepsilon_{\\text{liq}}` — maximum
            probability of liquidation over ``solvency_horizon_years``.
        solvency_horizon_years: :math:`h_{\\text{liq}}` — risk horizon
            for the upper-boundary calculation. Defaults to 3 hours.
        rebalance_horizon_years: :math:`h_{\\text{reb}}` — horizon over
            which the lower-boundary economic calculation amortises
            the rebalance cost. Defaults to 1 day.
        rebalance_cost_usdc: :math:`K_{\\text{reb}}` — fixed-cost of one
            rebalance in USDC (gas + slippage round-trip). Defaults to
            0.0 (we model fee/slippage inside the entities; set this to
            a positive value to add an extrinsic gas cost).
    """

    target_ltv: float = 0.80
    liquidation_ltv: float = 0.86
    liquidation_budget: float = 1.0e-4
    solvency_horizon_years: float = 3.0 / (365.25 * 24.0)
    rebalance_horizon_years: float = 1.0 / 365.25
    rebalance_cost_usdc: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 < self.target_ltv < 1.0:
            raise ValueError(
                f"target_ltv must be in (0, 1), got {self.target_ltv}"
            )
        if not 0.0 < self.liquidation_ltv < 1.0:
            raise ValueError(
                f"liquidation_ltv must be in (0, 1), got {self.liquidation_ltv}"
            )
        if self.target_ltv >= self.liquidation_ltv:
            raise ValueError(
                f"target_ltv ({self.target_ltv}) must be strictly less "
                f"than liquidation_ltv ({self.liquidation_ltv})"
            )
        if not 0.0 < self.liquidation_budget < 1.0:
            raise ValueError(
                f"liquidation_budget must be in (0, 1), got {self.liquidation_budget}"
            )
        if self.solvency_horizon_years <= 0.0:
            raise ValueError("solvency_horizon_years must be positive")
        if self.rebalance_horizon_years <= 0.0:
            raise ValueError("rebalance_horizon_years must be positive")
        if self.rebalance_cost_usdc < 0.0:
            raise ValueError("rebalance_cost_usdc must be non-negative")


@dataclass
class LTVBand:
    """Result of one controller computation.

    Attributes:
        lower: :math:`L_L` (may be 0 when re-levering does not pay off).
        upper: :math:`L_U` (≤ liquidation_ltv).
        target: :math:`L^\\dagger` (the LTV to return to on either exit).
    """

    lower: float
    upper: float
    target: float


def compute_upper_boundary(
    *,
    drift: float,
    volatility: float,
    config: LTVControllerConfig,
) -> float:
    """Solve :math:`\\Pi_{\\text{liq}}(L_U; h_{\\text{liq}}) = \\varepsilon_{\\text{liq}}`.

    Wraps :func:`pendle_pt_loop.risk.first_passage.solve_max_ltv_for_budget`
    with the controller's configured horizon and budget.

    Args:
        drift: ``μ`` of ``ln(pt_price)``, annualised. Pass 0 if no
            estimate is available (the position is then evaluated in
            the noise-only regime, slightly conservative).
        volatility: ``σ`` of ``ln(pt_price)``, annualised.
        config: Controller configuration.

    Returns:
        :math:`L_U \\in (0, L_{\\text{liq}})` — the highest LTV the
        controller will tolerate before forcing a deleverage.
    """
    return solve_max_ltv_for_budget(
        liquidation_ltv=config.liquidation_ltv,
        horizon_years=config.solvency_horizon_years,
        drift=drift,
        volatility=volatility,
        liquidation_budget=config.liquidation_budget,
    )


def compute_lower_boundary(
    *,
    equity: float,
    pt_yield: float,
    borrow_rate: float,
    config: LTVControllerConfig,
) -> float:
    """Carry-vs-rebalance-cost lower boundary.

    Uses the linearised first-order formula

    .. math::

        L^\\dagger - L_L \\;=\\;
          \\frac{K_{\\text{reb}} (1 - L^\\dagger)^2}
               {X (r_{PT} - r_b) h_{\\text{reb}}}.

    When :math:`r_{PT} \\leq r_b` (loop is unprofitable on a per-dollar
    basis), there's no economic case for re-levering and we return 0
    (controller will let the position drift indefinitely on the lower
    side, only acting on the upper boundary).

    Args:
        equity: Current equity capital :math:`X` in USDC.
        pt_yield: Current PT implied yield :math:`r_{PT}` (annualised).
        borrow_rate: Current Morpho borrow rate :math:`r_b` (annualised).
        config: Controller configuration.

    Returns:
        :math:`L_L \\in [0, L^\\dagger)`. Equals 0 when carry differential
        is non-positive, when ``K_{reb}`` is zero (no friction → always
        re-lever, but we still pin to 0 for an unambiguous "no lower
        bound" semantic), or when the implied threshold exceeds
        :math:`L^\\dagger`.
    """
    if config.rebalance_cost_usdc <= 0.0:
        return 0.0  # no friction → no lower band needed
    if pt_yield <= borrow_rate:
        return 0.0  # loop unprofitable, don't re-lever
    if equity <= 0.0:
        return 0.0

    denom = equity * (pt_yield - borrow_rate) * config.rebalance_horizon_years
    numerator = (
        config.rebalance_cost_usdc * (1.0 - config.target_ltv) ** 2
    )
    delta_l = numerator / denom

    lower = config.target_ltv - delta_l
    if lower < 0.0:
        return 0.0
    return lower


def compute_band(
    *,
    drift: float,
    volatility: float,
    equity: float,
    pt_yield: float,
    borrow_rate: float,
    config: LTVControllerConfig,
) -> LTVBand:
    """Compute the full ``[L_L, L_U]`` band for the current observation.

    Convenience wrapper combining :func:`compute_upper_boundary` and
    :func:`compute_lower_boundary`.

    Returns:
        :class:`LTVBand` with ``lower``, ``upper``, ``target`` fields.
    """
    upper = compute_upper_boundary(
        drift=drift, volatility=volatility, config=config
    )
    lower = compute_lower_boundary(
        equity=equity,
        pt_yield=pt_yield,
        borrow_rate=borrow_rate,
        config=config,
    )
    # If the volatility regime is so wild that even the target itself
    # would exceed the solvency budget, force the controller to flatten
    # to whatever the solvency engine permits.
    effective_target = min(config.target_ltv, upper)
    return LTVBand(lower=lower, upper=upper, target=effective_target)


__all__ = [
    "LTVControllerConfig",
    "LTVBand",
    "compute_upper_boundary",
    "compute_lower_boundary",
    "compute_band",
]
