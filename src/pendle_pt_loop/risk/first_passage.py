"""First-passage probability for a leveraged PT position through liquidation.

Adapts the Vega Basis Trading paper (Krestenko et al. 2026) from the
spot-perpetual basis context to a Pendle PT / Morpho lending context.

Setup
-----
We hold ``N_PT`` units of PT face as collateral on Morpho against ``D``
USDC of debt. The current loan-to-value ratio is

.. math::

    \\text{LTV}(t) = \\frac{D}{N_{PT} \\cdot pt(t)}.

Liquidation occurs when :math:`\\text{LTV}(t) > L_{\\text{liq}}` (the
market's LLTV parameter, e.g. 0.86 or 0.915 for PT-sUSDE markets on
Morpho). Equivalently the PT mark price must fall below

.. math::

    pt^*(t) = pt(0) \\cdot \\frac{L_0}{L_{\\text{liq}}},

where :math:`L_0 = D / (N_{PT} \\cdot pt(0))` is the LTV at position
opening. This is a **lower** barrier on ``pt``, and the strategy is
liquidated the moment ``pt`` first crosses it.

Model
-----
We treat the log-return :math:`X_t = \\ln(pt(t) / pt(0))` as a Brownian
motion with constant drift and volatility,

.. math::

    X_t = \\mu \\, t + \\sigma \\, W_t,

where :math:`\\mu` and :math:`\\sigma` are estimated from historical
``pt_price`` data on a rolling window. The deterministic drift from
PT's pull toward 1 at expiry shows up in :math:`\\mu`; the stochastic
noise from implied-yield shocks shows up in :math:`\\sigma`.

Closed form
-----------
For a Brownian motion :math:`X_t = \\mu t + \\sigma W_t` started at 0
and a lower barrier :math:`b = \\ln(z) < 0`, the standard Bachelier-type
result gives the probability of first-passage within horizon ``h`` as:

.. math::

    \\Pi_{\\text{liq}}(z; h) = \\Phi\\!\\left(\\frac{b - \\mu h}{\\sigma \\sqrt{h}}\\right)
      + \\exp\\!\\left(\\frac{2 \\mu b}{\\sigma^2}\\right)
        \\Phi\\!\\left(\\frac{b + \\mu h}{\\sigma \\sqrt{h}}\\right),

with :math:`\\Phi` the standard normal CDF. This is the lower-barrier
version of the Vega paper's upper-barrier formula (their basis trade
liquidates on **rising** spot, ours on **falling** PT); the underlying
GBM machinery is identical, only the barrier direction is flipped.

Inputs in the LTV picture
-------------------------
The barrier in log-pt space is :math:`b = \\ln(L_0 / L_{\\text{liq}})`,
which is negative whenever the position is safely opened
(:math:`L_0 < L_{\\text{liq}}`). The function exposes both the
``log-pt`` form (used by tests / analytical work) and the
``LTV`` form (used by the controller in
``pendle_pt_loop.risk.ltv_controller``).

Edge cases
----------
* If the position is already underwater (:math:`L_0 \\geq L_{\\text{liq}}`)
  the function returns 1.0 — liquidation is certain (already happened).
* If :math:`\\sigma = 0` and the deterministic drift does not push us
  through the barrier within ``h``, the function returns 0.0.
* :math:`h = 0`: returns 1.0 if already through the barrier, else 0.0.
* Numerically safeguards the ``exp(2μb/σ²)`` term against overflow
  when :math:`\\mu` is large and :math:`\\sigma` is small.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import norm


def first_passage_probability_logbarrier(
    log_barrier: float,
    horizon_years: float,
    drift: float,
    volatility: float,
) -> float:
    """Probability of a Brownian motion with drift hitting a lower log-barrier.

    Args:
        log_barrier: ``b = ln(z)`` with ``z < 1`` (so ``b < 0``). For our
            LTV problem ``z = L_0 / L_liq`` and ``b = ln(L_0 / L_liq)``.
        horizon_years: ``h`` in years. Typical operational value: 3 hours
            = ``3 / (365.25 * 24)``.
        drift: ``μ`` of the log-process, in 1/year units.
        volatility: ``σ`` of the log-process, in 1/sqrt(year) units.

    Returns:
        ``Π_liq ∈ [0, 1]``.
    """
    if log_barrier >= 0.0:
        # Position already underwater — liquidation has happened.
        return 1.0
    if horizon_years <= 0.0:
        return 0.0
    if volatility <= 0.0:
        # No noise — pure drift. Liquidation iff drift pushes us through.
        if drift * horizon_years <= log_barrier:
            return 1.0
        return 0.0

    sigma_sqrt_h = volatility * math.sqrt(horizon_years)
    drift_h = drift * horizon_years

    arg1 = (log_barrier - drift_h) / sigma_sqrt_h
    arg2 = (log_barrier + drift_h) / sigma_sqrt_h
    # Reflection-formula exponent; clip to avoid overflow when drift is
    # large relative to vol — at that point Π_liq ≈ 0 anyway.
    raw_exponent = 2.0 * drift * log_barrier / (volatility * volatility)
    exponent = min(raw_exponent, 700.0)  # exp(700) ~ 1e304, safe in float64
    reflection = math.exp(exponent) if exponent > -700.0 else 0.0

    prob = norm.cdf(arg1) + reflection * norm.cdf(arg2)
    # Clamp to [0, 1] — numerical noise can occasionally push slightly out.
    return max(0.0, min(1.0, prob))


def first_passage_probability_from_ltv(
    initial_ltv: float,
    liquidation_ltv: float,
    horizon_years: float,
    drift: float,
    volatility: float,
) -> float:
    """LTV-form wrapper around :func:`first_passage_probability_logbarrier`.

    The barrier in log-pt space is ``ln(initial_ltv / liquidation_ltv)``
    — negative whenever the position is safely opened.

    Args:
        initial_ltv: Current LTV ``L_0 = D / (N_PT * pt(0))``.
        liquidation_ltv: Market LLTV (``MorphoConfig.lltv``).
        horizon_years: Risk horizon ``h``.
        drift: ``μ`` of ``ln(pt)``, in 1/year units.
        volatility: ``σ`` of ``ln(pt)``, in 1/sqrt(year) units.

    Returns:
        ``Π_liq ∈ [0, 1]``.

    Raises:
        ValueError: if ``initial_ltv`` or ``liquidation_ltv`` is not in
            (0, 1], or if liquidation_ltv is below initial_ltv (means
            we're already past the LLTV).
    """
    if not 0.0 < initial_ltv <= 1.0:
        raise ValueError(
            f"initial_ltv must be in (0, 1], got {initial_ltv}"
        )
    if not 0.0 < liquidation_ltv <= 1.0:
        raise ValueError(
            f"liquidation_ltv must be in (0, 1], got {liquidation_ltv}"
        )
    if initial_ltv >= liquidation_ltv:
        return 1.0
    log_barrier = math.log(initial_ltv / liquidation_ltv)
    return first_passage_probability_logbarrier(
        log_barrier=log_barrier,
        horizon_years=horizon_years,
        drift=drift,
        volatility=volatility,
    )


def estimate_log_drift_and_vol(
    pt_prices: pd.Series,
    annualization: float = 365.25 * 24.0,
) -> tuple[float, float]:
    """Estimate annualised drift and volatility of ``ln(pt_price)``.

    Uses simple sample mean and sample standard deviation of one-step
    log-returns. Assumes the input series is already at the cadence
    matching ``annualization`` (default: hourly → 365.25*24 steps/year).

    Args:
        pt_prices: Time-indexed series of positive PT prices.
        annualization: Number of steps per year for the input cadence.

    Returns:
        ``(drift, volatility)`` in (1/year, 1/sqrt(year)) units.

    Raises:
        ValueError: if fewer than two prices, or non-positive prices
            anywhere in the series.
    """
    if len(pt_prices) < 2:
        raise ValueError("need at least two prices to estimate returns")
    if (pt_prices <= 0).any():
        raise ValueError("pt_prices must be strictly positive")

    log_returns = np.log(pt_prices.to_numpy()[1:] / pt_prices.to_numpy()[:-1])
    if len(log_returns) == 0:
        raise ValueError("not enough log-returns after differencing")

    mu_per_step = float(np.mean(log_returns))
    sigma_per_step = float(np.std(log_returns, ddof=1))

    drift = mu_per_step * annualization
    volatility = sigma_per_step * math.sqrt(annualization)
    return drift, volatility


def solve_max_ltv_for_budget(
    liquidation_ltv: float,
    horizon_years: float,
    drift: float,
    volatility: float,
    liquidation_budget: float,
    tol: float = 1e-6,
    max_iter: int = 64,
) -> float:
    """Invert :math:`\\Pi_{\\text{liq}}(L; h) = \\varepsilon`: solve for the
    largest ``L`` whose probability of liquidation over ``h`` is at most
    ``liquidation_budget``.

    Bisection over ``L ∈ (0, liquidation_ltv)``. Monotonicity of
    ``Π_liq`` in ``L`` is guaranteed (higher initial LTV → barrier
    closer → larger Π_liq).

    Args:
        liquidation_ltv: Market LLTV.
        horizon_years: Risk horizon ``h``.
        drift, volatility: GBM parameters of ``ln(pt)``.
        liquidation_budget: Maximum acceptable ``Π_liq`` (e.g. 1e-4).
        tol: Bisection absolute tolerance on ``L``.
        max_iter: Bisection iteration cap.

    Returns:
        The optimal ``L_U`` (upper LTV cutoff for the dynamic controller).
        If even ``L = 0`` exceeds the budget, returns 0.0. If even
        ``L = liquidation_ltv - eps`` is under the budget, returns
        liquidation_ltv - eps.
    """
    if not 0.0 < liquidation_budget < 1.0:
        raise ValueError(
            f"liquidation_budget must be in (0, 1), got {liquidation_budget}"
        )

    lo, hi = 0.0, liquidation_ltv * (1.0 - 1e-12)

    # Edge: even the most conservative LTV breaks the budget — return 0.
    if first_passage_probability_from_ltv(
        max(lo, 1e-12), liquidation_ltv, horizon_years, drift, volatility
    ) > liquidation_budget:
        return 0.0
    # Edge: even the most aggressive LTV satisfies the budget — return cap.
    if first_passage_probability_from_ltv(
        hi, liquidation_ltv, horizon_years, drift, volatility
    ) <= liquidation_budget:
        return hi

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        p = first_passage_probability_from_ltv(
            mid, liquidation_ltv, horizon_years, drift, volatility
        )
        if p > liquidation_budget:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return lo  # the conservative side of the final bracket


__all__ = [
    "first_passage_probability_logbarrier",
    "first_passage_probability_from_ltv",
    "estimate_log_drift_and_vol",
    "solve_max_ltv_for_budget",
]
