"""Session 2 unit tests for the Pendle PT entity.

These tests pin down:
    * The two pricing modes (`linear`, `exponential`) of
      :func:`pendle_pt_loop.entities.pendle_pt.compute_pt_price`.
    * ``update_state`` semantics for the real-data path
      (``derive_pt_price=False``) and the scenario path
      (``derive_pt_price=True``).
    * AMM swap mechanics with fee + slippage — round-trip lossiness,
      monotone slippage, zero-liquidity rejection.
    * Expiry path: redeem returns face 1:1, pre-expiry redeem rejected.
"""

from __future__ import annotations

import math

import pytest

from pendle_pt_loop.entities.pendle_pt import (
    SECONDS_PER_YEAR,
    PendlePTConfig,
    PendlePTEntity,
    PendlePTGlobalState,
    compute_pt_price,
)
from fractal.core.base.entity import EntityException


# Convenience for tests: half-year of seconds.
HALF_YEAR_SECONDS: float = 0.5 * SECONDS_PER_YEAR


# ----------------------------------------------------------------------
# compute_pt_price
# ----------------------------------------------------------------------

def test_pt_price_at_expiry_is_one() -> None:
    """Both modes must return exactly 1.0 once tau <= 0."""
    for mode in ("linear", "exponential"):
        assert compute_pt_price(0.14, 0.0, mode) == 1.0
        assert compute_pt_price(0.14, -1.0, mode) == 1.0
        # Zero yield, post-expiry — still 1.0.
        assert compute_pt_price(0.0, 0.0, mode) == 1.0


def test_pt_price_linear_matches_formula() -> None:
    """Linear: P = 1 - r * tau. 14% APY @ 6 months -> 0.93."""
    price = compute_pt_price(0.14, HALF_YEAR_SECONDS, "linear")
    assert price == pytest.approx(0.93, rel=1e-12)


def test_pt_price_exponential_matches_formula() -> None:
    """Exponential: P = exp(-r * tau). 14% APY @ 6 months."""
    price = compute_pt_price(0.14, HALF_YEAR_SECONDS, "exponential")
    expected = math.exp(-0.14 * 0.5)
    assert price == pytest.approx(expected, rel=1e-12)
    # Sanity: ~ 0.9324.
    assert price == pytest.approx(0.9324, rel=1e-3)


def test_pricing_modes_agree_at_small_yield_x_tau() -> None:
    """Taylor: 1 - x and exp(-x) agree to O(x^2). At x = 0.01,
    difference is ~ x^2 / 2 = 5e-5, well under 1e-4."""
    # Choose r and tau so r * tau = 0.01.
    r = 0.01
    seconds = 1.0 * SECONDS_PER_YEAR  # tau = 1 year, so r*tau = 0.01.
    linear = compute_pt_price(r, seconds, "linear")
    exp = compute_pt_price(r, seconds, "exponential")
    assert abs(linear - exp) < 1e-4


def test_compute_pt_price_unknown_mode_raises() -> None:
    """Defensive: bad mode is an EntityException, not a silent fallthrough."""
    with pytest.raises(EntityException, match="unsupported mode"):
        compute_pt_price(0.14, HALF_YEAR_SECONDS, "quadratic")


# ----------------------------------------------------------------------
# update_state
# ----------------------------------------------------------------------

def test_update_state_preserves_passed_pt_price_when_derive_false() -> None:
    """Real-data path: caller's pt_price wins."""
    entity = PendlePTEntity(PendlePTConfig(derive_pt_price=False))
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.5,  # deliberately inconsistent with implied_yield
            implied_yield=0.14,
            seconds_to_expiry=HALF_YEAR_SECONDS,
            pool_liquidity=1e7,
        )
    )
    assert entity._global_state.pt_price == pytest.approx(0.5)


def test_update_state_overwrites_pt_price_when_derive_true() -> None:
    """Scenario path: pt_price is recomputed from implied_yield."""
    entity = PendlePTEntity(
        PendlePTConfig(derive_pt_price=True, pricing_mode="linear")
    )
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.5,  # caller's value is discarded
            implied_yield=0.14,
            seconds_to_expiry=HALF_YEAR_SECONDS,
            pool_liquidity=1e7,
        )
    )
    assert entity._global_state.pt_price == pytest.approx(0.93, rel=1e-12)


def test_update_state_snaps_pt_price_to_one_at_expiry() -> None:
    """Once seconds_to_expiry <= 0, pt_price snaps to 1.0 and the
    stored seconds_to_expiry is normalised to 0.0 — regardless of
    derive_pt_price and regardless of what the caller passed."""
    entity = PendlePTEntity(PendlePTConfig(derive_pt_price=False))
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.42,  # nonsense at expiry; should be snapped
            implied_yield=0.14,
            seconds_to_expiry=-3600.0,
            pool_liquidity=1e7,
        )
    )
    assert entity._global_state.pt_price == 1.0
    assert entity._global_state.seconds_to_expiry == 0.0


# ----------------------------------------------------------------------
# AMM swap mechanics
# ----------------------------------------------------------------------

def _fresh_entity(
    *,
    fee: float = 0.001,
    slip: float = 0.5,
    pt_price: float = 0.93,
    pool: float = 1e7,
) -> PendlePTEntity:
    """Standard setup: 6-month PT @ 0.93, 10M USDC pool."""
    entity = PendlePTEntity(
        PendlePTConfig(amm_fee_rate=fee, slippage_factor=slip)
    )
    entity.update_state(
        PendlePTGlobalState(
            pt_price=pt_price,
            implied_yield=0.14,
            seconds_to_expiry=HALF_YEAR_SECONDS,
            pool_liquidity=pool,
        )
    )
    return entity


def test_buy_then_sell_round_trip_loses_to_fee_and_slippage() -> None:
    """Deposit 10k, buy_pt 10k, sell all PT — final cash must be < 10k."""
    entity = _fresh_entity()
    entity.action_deposit(10_000.0)
    entity.action_buy_pt(10_000.0)
    held = entity._internal_state.pt_face_amount
    assert held > 0
    entity.action_sell_pt(held)
    final_cash = entity._internal_state.cash
    # Strict less-than: fees and slippage are both non-zero.
    assert final_cash < 10_000.0
    # And not catastrophically less — 10bps fee + small slippage on
    # 10k against a 10M pool should leave us well north of 9.9k.
    assert final_cash > 9_900.0


def test_buy_slippage_monotonic_in_size() -> None:
    """A larger USDC trade buys *fewer* PT per USDC."""
    small = _fresh_entity()
    small.action_deposit(1_000.0)
    small.action_buy_pt(1_000.0)
    small_pt_per_usdc = small._internal_state.pt_face_amount / 1_000.0

    big = _fresh_entity()
    big.action_deposit(500_000.0)
    big.action_buy_pt(500_000.0)
    big_pt_per_usdc = big._internal_state.pt_face_amount / 500_000.0

    assert big_pt_per_usdc < small_pt_per_usdc


def test_buy_fails_on_zero_pool_liquidity() -> None:
    """Pool liquidity must be strictly positive."""
    entity = PendlePTEntity(PendlePTConfig())
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.93,
            implied_yield=0.14,
            seconds_to_expiry=HALF_YEAR_SECONDS,
            pool_liquidity=0.0,
        )
    )
    entity.action_deposit(10_000.0)
    with pytest.raises(EntityException, match="pool_liquidity"):
        entity.action_buy_pt(10_000.0)


def test_sell_fails_on_zero_pool_liquidity() -> None:
    """Symmetric to buy: selling also needs a live pool."""
    # First build a position via a healthy pool…
    entity = _fresh_entity()
    entity.action_deposit(10_000.0)
    entity.action_buy_pt(10_000.0)
    held = entity._internal_state.pt_face_amount
    # …then drain pool liquidity and attempt to sell.
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.93,
            implied_yield=0.14,
            seconds_to_expiry=HALF_YEAR_SECONDS,
            pool_liquidity=0.0,
        )
    )
    with pytest.raises(EntityException, match="pool_liquidity"):
        entity.action_sell_pt(held)


def test_buy_zero_fee_zero_slip_is_identity() -> None:
    """Limiting case: fee=0, slippage=0 -> pt_face = amount / pt_price."""
    entity = _fresh_entity(fee=0.0, slip=0.0)
    entity.action_deposit(10_000.0)
    entity.action_buy_pt(10_000.0)
    assert entity._internal_state.pt_face_amount == pytest.approx(
        10_000.0 / 0.93, rel=1e-12
    )
    assert entity._internal_state.cash == pytest.approx(0.0)


# ----------------------------------------------------------------------
# Redeem path
# ----------------------------------------------------------------------

def test_action_redeem_at_expiry_returns_face() -> None:
    """At expiry, PT face moves into cash 1:1."""
    entity = _fresh_entity()
    entity.action_deposit(10_000.0)
    entity.action_buy_pt(10_000.0)
    face = entity._internal_state.pt_face_amount
    # Roll to expiry via update_state. pt_price will be snapped to 1.0
    # by update_state regardless of what we pass for it here.
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.93,  # snapped to 1.0 because seconds_to_expiry == 0
            implied_yield=0.14,
            seconds_to_expiry=0.0,
            pool_liquidity=1e7,
        )
    )
    assert entity._global_state.pt_price == 1.0
    entity.action_redeem(face)
    assert entity._internal_state.pt_face_amount == pytest.approx(0.0)
    assert entity._internal_state.cash == pytest.approx(face)


def test_action_redeem_pre_expiry_rejected() -> None:
    """Pre-existing invariant: redeem before expiry is an error."""
    entity = _fresh_entity()
    entity.action_deposit(10_000.0)
    entity.action_buy_pt(10_000.0)
    with pytest.raises(EntityException, match="not at expiry"):
        entity.action_redeem(100.0)
