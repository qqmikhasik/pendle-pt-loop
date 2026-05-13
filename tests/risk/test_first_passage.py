"""Tests for the first-passage probability and its inverse.

Verifies the closed-form formula on known boundary cases (no noise,
infinite noise, zero horizon, already-underwater position) and against
Monte-Carlo simulation on a small sanity grid.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from pendle_pt_loop.risk.first_passage import (
    estimate_log_drift_and_vol,
    first_passage_probability_from_ltv,
    first_passage_probability_logbarrier,
    solve_max_ltv_for_budget,
)


# ----------------------------------------------------------------------
# Boundary / sanity cases
# ----------------------------------------------------------------------


def test_already_underwater_returns_one() -> None:
    """Log-barrier = 0 (or positive) means LTV is already at/over LLTV."""
    assert first_passage_probability_logbarrier(
        log_barrier=0.0, horizon_years=1.0, drift=0.0, volatility=0.1
    ) == 1.0


def test_zero_horizon_returns_zero_when_safe() -> None:
    """At h=0 with a strictly negative barrier we cannot have crossed yet."""
    assert first_passage_probability_logbarrier(
        log_barrier=-0.1, horizon_years=0.0, drift=0.0, volatility=0.5
    ) == 0.0


def test_zero_volatility_only_drift_matters() -> None:
    """σ = 0: liquidation iff μ·h pushes us below the barrier."""
    # Barrier at log(0.93) ≈ -0.0726, h = 1y, drift = -0.10 → -0.10 ≤ -0.0726 → 1.0
    prob_through = first_passage_probability_logbarrier(
        log_barrier=math.log(0.93),
        horizon_years=1.0,
        drift=-0.10,
        volatility=0.0,
    )
    assert prob_through == 1.0
    # Drift = -0.05: -0.05 > -0.0726 → stays above → 0.0
    prob_safe = first_passage_probability_logbarrier(
        log_barrier=math.log(0.93),
        horizon_years=1.0,
        drift=-0.05,
        volatility=0.0,
    )
    assert prob_safe == 0.0


def test_probability_in_unit_interval() -> None:
    """Random reasonable inputs always produce a probability."""
    for log_b in [-0.5, -0.2, -0.1, -0.05, -0.01]:
        for sigma in [0.1, 0.3, 0.5, 1.0]:
            for mu in [-0.2, 0.0, 0.2]:
                p = first_passage_probability_logbarrier(
                    log_barrier=log_b,
                    horizon_years=0.5,
                    drift=mu,
                    volatility=sigma,
                )
                assert 0.0 <= p <= 1.0, (log_b, sigma, mu, p)


def test_probability_monotone_in_volatility() -> None:
    """Higher σ → higher liquidation probability (barrier reached faster)."""
    log_b = math.log(0.93)  # ~7% below entry
    h = 1.0
    mu = 0.0
    probs = [
        first_passage_probability_logbarrier(log_b, h, mu, sigma)
        for sigma in [0.05, 0.10, 0.20, 0.50, 1.00]
    ]
    for a, b in zip(probs, probs[1:]):
        assert a <= b + 1e-9, probs


def test_probability_monotone_in_horizon() -> None:
    """Longer horizon → higher liquidation probability (more time to cross)."""
    log_b = math.log(0.93)
    sigma = 0.20
    mu = 0.0
    probs = [
        first_passage_probability_logbarrier(log_b, h, mu, sigma)
        for h in [0.01, 0.1, 0.5, 1.0, 2.0]
    ]
    for a, b in zip(probs, probs[1:]):
        assert a <= b + 1e-9, probs


def test_probability_monotone_in_initial_ltv() -> None:
    """Higher initial LTV → barrier closer → higher Π_liq."""
    lltv = 0.86
    h = 3 / (365.25 * 24.0)  # 3-hour operational horizon
    probs = [
        first_passage_probability_from_ltv(
            initial_ltv=L,
            liquidation_ltv=lltv,
            horizon_years=h,
            drift=0.0,
            volatility=0.30,
        )
        for L in [0.50, 0.60, 0.70, 0.75, 0.80, 0.84]
    ]
    for a, b in zip(probs, probs[1:]):
        assert a <= b + 1e-9, probs


# ----------------------------------------------------------------------
# Monte-Carlo sanity (the closed form vs simulated GBM hitting times)
# ----------------------------------------------------------------------


def _mc_first_passage(
    log_barrier: float,
    horizon_years: float,
    drift: float,
    volatility: float,
    n_paths: int = 20_000,
    n_steps: int = 240,
    seed: int = 7,
) -> float:
    """Monte-Carlo estimate of Π_liq for cross-check."""
    rng = np.random.default_rng(seed)
    dt = horizon_years / n_steps
    increments = rng.normal(
        loc=drift * dt,
        scale=volatility * math.sqrt(dt),
        size=(n_paths, n_steps),
    )
    paths = increments.cumsum(axis=1)
    # Track running minimum across the path.
    min_per_path = paths.min(axis=1)
    return float(np.mean(min_per_path <= log_barrier))


def test_closed_form_matches_monte_carlo_within_tolerance() -> None:
    """Sanity: the formula should agree with MC up to discretisation bias.

    MC with finite grid systematically UNDER-estimates Π_liq because it
    only observes the path at discrete times and can miss continuous-time
    excursions below the barrier between grid points. Bias is
    :math:`O(\\sigma \\sqrt{dt})` and shrinks as ``n_steps`` grows; we
    use a fine grid (n_steps=2400 → dt ≈ 0.0002 year) so the residual
    bias is below 2.5%.
    """
    log_b = math.log(0.93)
    h = 0.5
    mu, sigma = 0.0, 0.30
    p_closed = first_passage_probability_logbarrier(log_b, h, mu, sigma)
    p_mc = _mc_first_passage(log_b, h, mu, sigma, n_paths=30_000, n_steps=2_400)
    # Closed form is the truth; MC is biased low. Tolerate up to 2.5%
    # absolute discrepancy (residual discretisation + ~0.3% MC SE).
    assert p_closed - p_mc < 0.025, (p_closed, p_mc)
    # MC should also be a lower bound up to MC noise.
    assert p_mc <= p_closed + 0.01, (p_closed, p_mc)


# ----------------------------------------------------------------------
# Drift / volatility estimation
# ----------------------------------------------------------------------


def test_estimate_recovers_volatility_on_synthetic_series() -> None:
    """Sample volatility converges quickly; drift converges slowly.

    Recovering drift to high precision needs many years of data; SE of
    the drift estimate is ``σ / √T_years``. For T=1 year and σ=0.30
    that's ±0.30 — useless. SE of vol estimate is much smaller. This
    test asserts only the vol estimate (the input the LTV controller
    actually relies on most heavily); drift is treated as a noisy
    nuisance parameter in the controller.
    """
    rng = np.random.default_rng(123)
    true_drift = 0.10  # 10% per year
    true_vol = 0.30  # 30% annualised
    n = 10_000
    steps_per_year = 365.25 * 24.0
    dt = 1.0 / steps_per_year
    log_returns = rng.normal(
        loc=true_drift * dt, scale=true_vol * math.sqrt(dt), size=n
    )
    log_prices = np.log(0.93) + np.cumsum(log_returns)
    series = pd.Series(np.exp(log_prices))

    drift, vol = estimate_log_drift_and_vol(series, annualization=steps_per_year)
    # Vol estimate has SE ≈ σ·√(2/(N-1)) ≈ 0.30·√(2/9999) ≈ 0.004.
    # Drift estimate has SE ≈ σ/√T_years ≈ 0.30/√1.14 ≈ 0.28. Cannot
    # assert it tightly — just sanity-check it's not wildly far.
    assert vol == pytest.approx(true_vol, abs=0.02)
    assert abs(drift - true_drift) < 1.0  # liberal — drift is noisy


def test_estimate_rejects_short_series() -> None:
    with pytest.raises(ValueError, match="at least two prices"):
        estimate_log_drift_and_vol(pd.Series([0.93]))


def test_estimate_rejects_nonpositive_prices() -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        estimate_log_drift_and_vol(pd.Series([0.93, 0.0, 0.94]))


# ----------------------------------------------------------------------
# Inverse: solve for max LTV under a budget
# ----------------------------------------------------------------------


def test_solve_inversion_round_trip() -> None:
    """L_U from solver should produce Π_liq ≈ budget when fed back."""
    lltv = 0.86
    h = 3 / (365.25 * 24.0)  # 3 hours
    drift = 0.0
    vol = 0.30
    budget = 1e-4

    lu = solve_max_ltv_for_budget(
        liquidation_ltv=lltv,
        horizon_years=h,
        drift=drift,
        volatility=vol,
        liquidation_budget=budget,
    )
    assert 0.0 < lu < lltv

    p = first_passage_probability_from_ltv(
        initial_ltv=lu,
        liquidation_ltv=lltv,
        horizon_years=h,
        drift=drift,
        volatility=vol,
    )
    # Solver returns the LARGEST L with P ≤ budget; tolerate small slack.
    assert p <= budget * 1.05


def test_solver_monotone_in_budget() -> None:
    """Looser budget → solver allows a higher LTV."""
    lltv = 0.86
    h = 3.0 / (365.25 * 24.0)
    drift = 0.0
    vol = 0.30
    lus = [
        solve_max_ltv_for_budget(
            liquidation_ltv=lltv,
            horizon_years=h,
            drift=drift,
            volatility=vol,
            liquidation_budget=eps,
        )
        for eps in [1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1]
    ]
    for a, b in zip(lus, lus[1:]):
        assert a <= b + 1e-9, lus


def test_solve_returns_zero_when_budget_too_tight() -> None:
    """Insanely tight budget with high noise → no LTV satisfies it."""
    lu = solve_max_ltv_for_budget(
        liquidation_ltv=0.86,
        horizon_years=10.0,
        drift=0.0,
        volatility=2.0,
        liquidation_budget=1e-30,
    )
    assert lu == 0.0
