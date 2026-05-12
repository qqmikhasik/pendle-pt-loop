"""Unit tests for :mod:`pendle_pt_loop.entities.morpho` — Session 2.

Covers:
    * borrow-rate accrual under :meth:`MorphoEntity.update_state` (single
      step, multi-step compounding, first-call special case);
    * LLTV enforcement on ``action_borrow`` and ``action_withdraw`` with
      state-revert on failure;
    * liquidation flag latching and the rejection of all actions once
      the entity is flagged;
    * monotonicity of the health factor in debt and in collateral price,
      plus the zero-debt sentinel ``+inf``.
"""

from __future__ import annotations

import math

import pytest

from fractal.core.base.entity import EntityException
from pendle_pt_loop.entities.morpho import (
    SECONDS_PER_YEAR,
    MorphoConfig,
    MorphoEntity,
    MorphoGlobalState,
)


# A fixed Unix epoch ("2024-06-01 00:00:00 UTC" — arbitrary but realistic).
T0: float = 1_717_200_000.0


def _state(
    *,
    collateral_price: float = 1.0,
    debt_price: float = 1.0,
    borrowing_rate: float = 0.0,
    timestamp_seconds: float = T0,
) -> MorphoGlobalState:
    """Construct a ``MorphoGlobalState`` with named overrides only.

    Keeps the tests free of positional-argument noise.
    """
    return MorphoGlobalState(
        collateral_price=collateral_price,
        debt_price=debt_price,
        borrowing_rate=borrowing_rate,
        timestamp_seconds=timestamp_seconds,
    )


# ----------------------------------------------------------------------
# Instantiation and action surface
# ----------------------------------------------------------------------

def test_morpho_instantiates_and_actions_listed() -> None:
    entity = MorphoEntity()
    actions = entity.get_available_actions()
    for name in ("deposit", "withdraw", "borrow", "repay"):
        assert name in actions, f"missing action {name!r}: {actions}"
    assert entity.balance == pytest.approx(0.0)
    assert entity.is_liquidated is False


# ----------------------------------------------------------------------
# Borrow-rate accrual
# ----------------------------------------------------------------------

def test_borrow_rate_accrual_one_year_at_six_percent() -> None:
    """Single step of exactly one year at 6% APY ⇒ debt × 1.06."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    # First observation: rate is 6%, position is opened. No accrual fires
    # because last_timestamp was None on entry.
    entity.update_state(
        _state(collateral_price=1.0, borrowing_rate=0.06, timestamp_seconds=T0)
    )
    entity.action_deposit(10_000.0)
    entity.action_borrow(5_000.0)
    # Second observation one year later — rate still 6%.
    entity.update_state(
        _state(
            collateral_price=1.0,
            borrowing_rate=0.06,
            timestamp_seconds=T0 + SECONDS_PER_YEAR,
        )
    )
    assert entity.internal_state.debt == pytest.approx(5_000.0 * 1.06, abs=1e-6)


def test_borrow_rate_accrual_compounds_across_steps() -> None:
    """One year split into 12 monthly steps ⇒ debt × (1 + r/12)^12.

    With the linear per-step formula ``debt *= 1 + r*dt``, 12 equal
    monthly steps multiply the debt by ``(1 + r/12)**12``, which differs
    from the single-step ``(1 + r)`` by O(r²). We assert the expected
    compounded value, not equality with the one-step path.
    """
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    rate = 0.06
    entity.update_state(
        _state(collateral_price=1.0, borrowing_rate=rate, timestamp_seconds=T0)
    )
    entity.action_deposit(10_000.0)
    entity.action_borrow(5_000.0)

    n_steps = 12
    for i in range(1, n_steps + 1):
        entity.update_state(
            _state(
                collateral_price=1.0,
                borrowing_rate=rate,
                timestamp_seconds=T0 + i * SECONDS_PER_YEAR / n_steps,
            )
        )
    expected = 5_000.0 * (1.0 + rate / n_steps) ** n_steps
    assert entity.internal_state.debt == pytest.approx(expected, rel=1e-12)


def test_first_update_state_just_stores_timestamp() -> None:
    """The first ``update_state`` must not accrue (no prior timestamp)."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.action_deposit(10_000.0)
    # Pre-load some debt by hand to make accrual observable if it fired.
    entity._internal_state.debt = 5_000.0  # noqa: SLF001 — test-only hack
    entity.update_state(
        _state(collateral_price=1.0, borrowing_rate=0.99, timestamp_seconds=T0)
    )
    assert entity.internal_state.debt == pytest.approx(5_000.0)
    assert entity.internal_state.last_timestamp == pytest.approx(T0)


# ----------------------------------------------------------------------
# LLTV enforcement
# ----------------------------------------------------------------------

def test_lltv_blocks_overborrow() -> None:
    """Borrow that would push ltv above lltv must raise and not mutate."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(_state(collateral_price=1.0))
    entity.action_deposit(10_000.0)
    # 8.7k borrow on 10k collateral ⇒ ltv = 0.87 > 0.86.
    with pytest.raises(EntityException, match="ltv"):
        entity.action_borrow(8_700.0)
    # State is fully reverted.
    assert entity.internal_state.debt == pytest.approx(0.0)
    # A safe borrow at the boundary still succeeds.
    entity.action_borrow(8_600.0)
    assert entity.internal_state.debt == pytest.approx(8_600.0)


def test_lltv_blocks_withdraw_pushing_overcollateral() -> None:
    """Withdraw that would push ltv above lltv must raise and not mutate."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(_state(collateral_price=1.0))
    entity.action_deposit(10_000.0)
    entity.action_borrow(8_000.0)  # ltv = 0.80, safe.
    # Withdrawing 1k collateral would leave 9k collateral, ltv = 8000/9000 ≈ 0.889 > 0.86.
    with pytest.raises(EntityException, match="ltv"):
        entity.action_withdraw(1_000.0)
    # State is fully reverted.
    assert entity.internal_state.collateral == pytest.approx(10_000.0)
    # A tiny safe withdraw still works (e.g. 100 → ltv = 8000/9900 ≈ 0.808).
    entity.action_withdraw(100.0)
    assert entity.internal_state.collateral == pytest.approx(9_900.0)


# ----------------------------------------------------------------------
# Liquidation flag
# ----------------------------------------------------------------------

def test_liquidation_flag_set_when_collateral_price_drops() -> None:
    """A price-driven drop pushes ltv above lltv ⇒ flag latches."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(_state(collateral_price=1.0, timestamp_seconds=T0))
    entity.action_deposit(10_000.0)
    entity.action_borrow(8_000.0)  # ltv = 0.80, safe.
    assert entity.is_liquidated is False
    # Price drops to 0.90: collateral_value = 9000, ltv = 8000/9000 ≈ 0.889 > 0.86.
    entity.update_state(
        _state(collateral_price=0.90, timestamp_seconds=T0 + 86_400.0)
    )
    assert entity.is_liquidated is True
    assert entity.ltv > 0.86


def test_liquidated_entity_rejects_actions() -> None:
    """Once flagged, every action raises with the canonical message."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(_state(collateral_price=1.0, timestamp_seconds=T0))
    entity.action_deposit(10_000.0)
    entity.action_borrow(8_000.0)
    # Force liquidation.
    entity.update_state(
        _state(collateral_price=0.80, timestamp_seconds=T0 + 86_400.0)
    )
    assert entity.is_liquidated is True

    for action, args in [
        ("action_deposit", (1.0,)),
        ("action_withdraw", (1.0,)),
        ("action_borrow", (1.0,)),
        ("action_repay", (1.0,)),
    ]:
        with pytest.raises(EntityException, match="liquidated"):
            getattr(entity, action)(*args)


# ----------------------------------------------------------------------
# Health-factor monotonicity
# ----------------------------------------------------------------------

def test_health_factor_monotonic_in_debt() -> None:
    """Larger debt ⇒ lower health factor (with collateral fixed)."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(_state(collateral_price=1.0))
    entity.action_deposit(10_000.0)
    entity.action_borrow(2_000.0)
    hf_low_debt = entity.health_factor
    entity.action_borrow(4_000.0)
    hf_high_debt = entity.health_factor
    assert hf_high_debt < hf_low_debt


def test_health_factor_monotonic_in_collateral_price() -> None:
    """Higher collateral price ⇒ higher health factor (debt fixed)."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(_state(collateral_price=1.0, timestamp_seconds=T0))
    entity.action_deposit(10_000.0)
    entity.action_borrow(5_000.0)  # ltv = 0.50, hf = 0.86 / 0.50 = 1.72.
    hf_low_price = entity.health_factor
    # Bump the collateral price; stay below lltv so no liquidation fires.
    entity.update_state(
        _state(collateral_price=1.20, timestamp_seconds=T0 + 86_400.0)
    )
    hf_high_price = entity.health_factor
    assert hf_high_price > hf_low_price


def test_zero_debt_has_infinite_health_factor() -> None:
    """No debt ⇒ health factor is exactly ``+inf`` (sentinel preserved)."""
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(_state(collateral_price=1.0))
    entity.action_deposit(10_000.0)
    assert entity.internal_state.debt == pytest.approx(0.0)
    assert math.isinf(entity.health_factor)
    assert entity.health_factor > 0  # +inf, not -inf.
