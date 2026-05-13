"""Tests for the dynamic LTV-band controller.

Verifies the asymmetric band semantics:
- Upper boundary inverts the first-passage formula.
- Lower boundary applies the carry-vs-rebalance-cost trade-off.
- Combined band has the right shape: ``0 <= L_L <= target <= L_U <= L_liq``.
"""
from __future__ import annotations

import pytest

from pendle_pt_loop.risk.first_passage import (
    first_passage_probability_from_ltv,
)
from pendle_pt_loop.risk.ltv_controller import (
    LTVBand,
    LTVControllerConfig,
    compute_band,
    compute_lower_boundary,
    compute_upper_boundary,
)


def _default_config(**overrides) -> LTVControllerConfig:
    kwargs = dict(
        target_ltv=0.80,
        liquidation_ltv=0.86,
        liquidation_budget=1.0e-4,
        solvency_horizon_years=3.0 / (365.25 * 24.0),
        rebalance_horizon_years=1.0 / 365.25,
        rebalance_cost_usdc=0.0,
    )
    kwargs.update(overrides)
    return LTVControllerConfig(**kwargs)


# ----------------------------------------------------------------------
# LTVControllerConfig validation
# ----------------------------------------------------------------------


def test_target_above_lltv_rejected() -> None:
    with pytest.raises(ValueError, match="strictly less"):
        LTVControllerConfig(target_ltv=0.90, liquidation_ltv=0.86)


def test_invalid_budget_rejected() -> None:
    with pytest.raises(ValueError, match="liquidation_budget"):
        LTVControllerConfig(liquidation_budget=0.0)
    with pytest.raises(ValueError, match="liquidation_budget"):
        LTVControllerConfig(liquidation_budget=1.0)


def test_negative_rebalance_cost_rejected() -> None:
    with pytest.raises(ValueError, match="rebalance_cost_usdc"):
        LTVControllerConfig(rebalance_cost_usdc=-1.0)


# ----------------------------------------------------------------------
# Upper boundary
# ----------------------------------------------------------------------


def test_upper_boundary_satisfies_budget() -> None:
    """At the computed L_U the Π_liq is at most the configured budget."""
    cfg = _default_config()
    lu = compute_upper_boundary(drift=0.0, volatility=0.30, config=cfg)
    assert 0.0 < lu < cfg.liquidation_ltv
    p = first_passage_probability_from_ltv(
        initial_ltv=lu,
        liquidation_ltv=cfg.liquidation_ltv,
        horizon_years=cfg.solvency_horizon_years,
        drift=0.0,
        volatility=0.30,
    )
    # Solver returns the LARGEST L with P ≤ budget; tolerate small slack.
    assert p <= cfg.liquidation_budget * 1.05


def test_upper_boundary_monotone_in_volatility() -> None:
    """Higher σ → tighter (lower) L_U."""
    cfg = _default_config()
    lus = [
        compute_upper_boundary(drift=0.0, volatility=sigma, config=cfg)
        for sigma in [0.10, 0.30, 0.50, 1.00, 2.00]
    ]
    for a, b in zip(lus, lus[1:]):
        assert a >= b - 1e-9, lus


def test_upper_boundary_at_low_vol_close_to_lltv() -> None:
    """In a calm regime the upper band can sit very close to the LLTV cap."""
    cfg = _default_config()
    lu = compute_upper_boundary(drift=0.0, volatility=0.05, config=cfg)
    assert lu > 0.83  # Within 3 p.p. of 0.86 cap


def test_upper_boundary_at_extreme_vol_collapses() -> None:
    """At absurd vol, even a tiny LTV is risky."""
    cfg = _default_config()
    lu = compute_upper_boundary(drift=0.0, volatility=10.0, config=cfg)
    assert lu < cfg.target_ltv  # forces the controller to deleverage below target


# ----------------------------------------------------------------------
# Lower boundary
# ----------------------------------------------------------------------


def test_lower_boundary_zero_with_no_rebalance_cost() -> None:
    """K_reb = 0 → no lower band (always cost-effective to re-lever)."""
    cfg = _default_config(rebalance_cost_usdc=0.0)
    ll = compute_lower_boundary(
        equity=10_000.0,
        pt_yield=0.14,
        borrow_rate=0.06,
        config=cfg,
    )
    assert ll == 0.0


def test_lower_boundary_zero_when_loop_unprofitable() -> None:
    """r_PT ≤ r_b → re-levering loses money; controller suppresses lower band."""
    cfg = _default_config(rebalance_cost_usdc=10.0)
    ll = compute_lower_boundary(
        equity=10_000.0,
        pt_yield=0.05,
        borrow_rate=0.06,
        config=cfg,
    )
    assert ll == 0.0


def test_lower_boundary_positive_in_normal_regime() -> None:
    """K_reb > 0 and positive carry → finite lower band below target.

    Calibration: $5 round-trip cost on $10k equity, 1-day amortisation,
    target LTV 0.80, 8% carry differential. Threshold δL works out to
    ≈9 p.p. → L_L ≈ 0.71.
    """
    cfg = _default_config(rebalance_cost_usdc=5.0)  # tiny round-trip cost
    ll = compute_lower_boundary(
        equity=10_000.0,
        pt_yield=0.14,
        borrow_rate=0.06,
        config=cfg,
    )
    assert 0.0 < ll < cfg.target_ltv


def test_lower_boundary_widens_with_higher_rebalance_cost() -> None:
    """More expensive rebalance → wider tolerance → lower L_L."""
    cfg_cheap = _default_config(rebalance_cost_usdc=1.0)
    cfg_expensive = _default_config(rebalance_cost_usdc=5.0)
    ll_cheap = compute_lower_boundary(
        equity=10_000.0, pt_yield=0.14, borrow_rate=0.06, config=cfg_cheap
    )
    ll_expensive = compute_lower_boundary(
        equity=10_000.0, pt_yield=0.14, borrow_rate=0.06, config=cfg_expensive
    )
    # Both must lie in (0, target); cheap has higher L_L (narrower tolerance).
    assert 0.0 < ll_expensive < ll_cheap < cfg_cheap.target_ltv


def test_lower_boundary_clamps_at_zero_when_threshold_exceeds_target() -> None:
    """If the formula would put L_L below 0, clamp to 0."""
    # Tiny equity, huge cost → δL exceeds target → clamp.
    cfg = _default_config(rebalance_cost_usdc=10_000.0)
    ll = compute_lower_boundary(
        equity=100.0,
        pt_yield=0.14,
        borrow_rate=0.06,
        config=cfg,
    )
    assert ll == 0.0


# ----------------------------------------------------------------------
# Full band
# ----------------------------------------------------------------------


def test_band_shape_lower_below_target_below_upper() -> None:
    """Sanity ordering of the three boundaries."""
    cfg = _default_config(rebalance_cost_usdc=50.0)
    band = compute_band(
        drift=0.0,
        volatility=0.30,
        equity=10_000.0,
        pt_yield=0.14,
        borrow_rate=0.06,
        config=cfg,
    )
    assert 0.0 <= band.lower <= band.target <= band.upper <= cfg.liquidation_ltv


def test_band_target_collapses_to_upper_when_vol_extreme() -> None:
    """When σ is so high that target itself violates the solvency budget,
    band.target should clamp down to the solvency-permitted maximum."""
    cfg = _default_config()
    band = compute_band(
        drift=0.0,
        volatility=10.0,  # absurd
        equity=10_000.0,
        pt_yield=0.14,
        borrow_rate=0.06,
        config=cfg,
    )
    assert band.target == band.upper
    assert band.target < cfg.target_ltv  # forced below the nominal target


def test_band_is_LTVBand_dataclass() -> None:
    cfg = _default_config()
    band = compute_band(
        drift=0.0,
        volatility=0.30,
        equity=10_000.0,
        pt_yield=0.14,
        borrow_rate=0.06,
        config=cfg,
    )
    assert isinstance(band, LTVBand)
