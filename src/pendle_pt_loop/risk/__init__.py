"""Risk / control utilities for the dynamic PT loop.

* ``first_passage`` — closed-form :math:`\\Pi_{\\text{liq}}(L; h)` for a
  Brownian-motion underlying through an absorbing lower barrier,
  adapted from Krestenko *et al.* (2026) by flipping the barrier
  direction (Pendle PT falls on rising implied yield; spot rises on
  falling implied yield in the basis paper). Plus a bisection solver
  that inverts the formula to give the maximum LTV satisfying a
  liquidation budget.

* ``ltv_controller`` — asymmetric :math:`[L_L, L_U]` band around a
  target LTV :math:`L^\\dagger`:
    - upper boundary from a solvency probability constraint;
    - lower boundary from a carry-vs-rebalance-cost trade-off.
"""

from pendle_pt_loop.risk.first_passage import (
    estimate_log_drift_and_vol,
    first_passage_probability_from_ltv,
    first_passage_probability_logbarrier,
    solve_max_ltv_for_budget,
)
from pendle_pt_loop.risk.ltv_controller import (
    LTVBand,
    LTVControllerConfig,
    compute_band,
    compute_lower_boundary,
    compute_upper_boundary,
)

__all__ = [
    "LTVBand",
    "LTVControllerConfig",
    "compute_band",
    "compute_lower_boundary",
    "compute_upper_boundary",
    "estimate_log_drift_and_vol",
    "first_passage_probability_from_ltv",
    "first_passage_probability_logbarrier",
    "solve_max_ltv_for_budget",
]
