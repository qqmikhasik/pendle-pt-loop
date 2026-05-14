"""Unit tests for :mod:`pendle_pt_loop.entities.funding_hedge` — Session 6.

Covers:
    * instantiation with defaults and exposed action surface;
    * the first ``update_state`` is timestamp-only (no accrual);
    * one-year accrual at a constant funding rate produces the exact
      analytical PnL ``s * notional * r * t`` (no compounding — funding
      is linear in dt within a step);
    * multi-step accrual sums linearly when notional is held constant,
      so 12 monthly steps equal one annual step within rel=1e-12;
    * ``direction="short"`` flips the sign of the accrued PnL;
    * ``action_withdraw`` reduces notional but preserves ``accrued_pnl``;
    * :attr:`FundingHedgeEntity.balance` tracks ``accrued_pnl``;
    * ``action_withdraw`` rejects negative amounts and over-withdraw.
"""

from __future__ import annotations

import pytest

from fractal.core.base.entity import EntityException
from pendle_pt_loop.entities.funding_hedge import (
    SECONDS_PER_YEAR,
    FundingHedgeConfig,
    FundingHedgeEntity,
    FundingHedgeGlobalState,
)


# A fixed Unix epoch ("2024-06-01 00:00:00 UTC" — same anchor as the
# Morpho tests so cross-entity log diffs line up if we ever interleave).
T0: float = 1_717_200_000.0


def _state(
    *,
    funding_rate: float = 0.0,
    timestamp_seconds: float = T0,
) -> FundingHedgeGlobalState:
    """Construct a ``FundingHedgeGlobalState`` with named overrides only."""
    return FundingHedgeGlobalState(
        funding_rate=funding_rate,
        timestamp_seconds=timestamp_seconds,
    )


# ----------------------------------------------------------------------
# Instantiation and action surface
# ----------------------------------------------------------------------

def test_funding_hedge_instantiates_with_defaults() -> None:
    """Default construction yields a zero-notional, zero-PnL entity."""
    entity = FundingHedgeEntity()
    assert entity.internal_state.notional == pytest.approx(0.0)
    assert entity.internal_state.accrued_pnl == pytest.approx(0.0)
    assert entity.internal_state.last_timestamp is None
    assert entity.balance == pytest.approx(0.0)


def test_available_actions_listed() -> None:
    """``deposit`` and ``withdraw`` are the only action verbs exposed."""
    entity = FundingHedgeEntity()
    actions = entity.get_available_actions()
    for name in ("deposit", "withdraw"):
        assert name in actions, f"missing action {name!r}: {actions}"


# ----------------------------------------------------------------------
# Accrual mechanics
# ----------------------------------------------------------------------

def test_first_update_state_does_not_accrue() -> None:
    """The first ``update_state`` must only stamp the timestamp."""
    entity = FundingHedgeEntity()
    entity.action_deposit(10_000.0)
    # Pre-load PnL by hand so a spurious accrual would be observable.
    entity._internal_state.accrued_pnl = 42.0  # noqa: SLF001 — test-only hack
    entity.update_state(_state(funding_rate=0.99, timestamp_seconds=T0))
    assert entity.internal_state.accrued_pnl == pytest.approx(42.0)
    assert entity.internal_state.last_timestamp == pytest.approx(T0)


def test_one_year_accrual_at_ten_percent_funding() -> None:
    """One year at constant 10% APR ⇒ PnL = notional * 0.10.

    Funding is linear within a step (``debt += notional * r * dt``),
    not compounded — there is no balance to compound on. So one
    full-year step at 10% produces exactly 10% of notional, full stop.
    """
    entity = FundingHedgeEntity()
    entity.update_state(_state(funding_rate=0.10, timestamp_seconds=T0))
    entity.action_deposit(10_000.0)
    entity.update_state(
        _state(funding_rate=0.10, timestamp_seconds=T0 + SECONDS_PER_YEAR)
    )
    assert entity.internal_state.accrued_pnl == pytest.approx(1_000.0, rel=1e-9)


def test_multi_step_accrual_sums_linearly() -> None:
    """12 monthly steps == 1 annual step when notional is held constant.

    Linear (not compounded) accrual means
    ``sum_i N * r * (t_i - t_{i-1}) = N * r * (t_n - t_0)``
    regardless of step count, exactly. We assert at rel=1e-12 to flag
    any accidental compounding (which would introduce O(r²) drift).
    """
    entity_monthly = FundingHedgeEntity()
    entity_annual = FundingHedgeEntity()
    rate = 0.10
    # Both entities open identically at T0.
    for e in (entity_monthly, entity_annual):
        e.update_state(_state(funding_rate=rate, timestamp_seconds=T0))
        e.action_deposit(10_000.0)
    # Monthly path: 12 equal steps.
    n_steps = 12
    for i in range(1, n_steps + 1):
        entity_monthly.update_state(
            _state(
                funding_rate=rate,
                timestamp_seconds=T0 + i * SECONDS_PER_YEAR / n_steps,
            )
        )
    # Annual path: 1 step.
    entity_annual.update_state(
        _state(funding_rate=rate, timestamp_seconds=T0 + SECONDS_PER_YEAR)
    )
    assert entity_monthly.internal_state.accrued_pnl == pytest.approx(
        entity_annual.internal_state.accrued_pnl, rel=1e-12
    )
    # And both equal the analytical answer.
    assert entity_monthly.internal_state.accrued_pnl == pytest.approx(
        1_000.0, rel=1e-12
    )


def test_short_direction_inverts_sign() -> None:
    """``direction="short"`` flips the PnL sign vs the default long side."""
    long_entity = FundingHedgeEntity(FundingHedgeConfig(direction="long"))
    short_entity = FundingHedgeEntity(FundingHedgeConfig(direction="short"))
    rate = 0.10
    for e in (long_entity, short_entity):
        e.update_state(_state(funding_rate=rate, timestamp_seconds=T0))
        e.action_deposit(10_000.0)
        e.update_state(
            _state(funding_rate=rate, timestamp_seconds=T0 + SECONDS_PER_YEAR)
        )
    assert long_entity.internal_state.accrued_pnl == pytest.approx(
        +1_000.0, rel=1e-9
    )
    assert short_entity.internal_state.accrued_pnl == pytest.approx(
        -1_000.0, rel=1e-9
    )
    # Symmetric to numerical precision: same magnitude, opposite sign.
    assert long_entity.internal_state.accrued_pnl == pytest.approx(
        -short_entity.internal_state.accrued_pnl, rel=1e-12
    )


def test_negative_funding_rate_drives_long_into_loss() -> None:
    """Long position under negative funding accrues *negative* PnL.

    This is the regime the hedge is most useful in (and also when it
    bleeds): the test pins down that ``accrued_pnl`` is signed and
    happily goes below zero.
    """
    entity = FundingHedgeEntity()
    entity.update_state(_state(funding_rate=-0.05, timestamp_seconds=T0))
    entity.action_deposit(10_000.0)
    entity.update_state(
        _state(funding_rate=-0.05, timestamp_seconds=T0 + SECONDS_PER_YEAR)
    )
    assert entity.internal_state.accrued_pnl == pytest.approx(-500.0, rel=1e-9)


# ----------------------------------------------------------------------
# Withdraw semantics
# ----------------------------------------------------------------------

def test_withdraw_reduces_notional_preserves_accrued_pnl() -> None:
    """Closing notional must not touch realised PnL."""
    entity = FundingHedgeEntity()
    entity.update_state(_state(funding_rate=0.10, timestamp_seconds=T0))
    entity.action_deposit(10_000.0)
    entity.update_state(
        _state(funding_rate=0.10, timestamp_seconds=T0 + SECONDS_PER_YEAR)
    )
    pnl_before = entity.internal_state.accrued_pnl
    assert pnl_before == pytest.approx(1_000.0, rel=1e-9)
    entity.action_withdraw(4_000.0)
    assert entity.internal_state.notional == pytest.approx(6_000.0)
    assert entity.internal_state.accrued_pnl == pytest.approx(pnl_before)


def test_withdraw_rejects_negative_amount() -> None:
    """Negative withdrawals are nonsense; entity refuses them."""
    entity = FundingHedgeEntity()
    entity.action_deposit(10_000.0)
    with pytest.raises(EntityException, match="non-negative"):
        entity.action_withdraw(-1.0)
    # Notional unchanged after the rejected call.
    assert entity.internal_state.notional == pytest.approx(10_000.0)


def test_withdraw_rejects_over_withdraw() -> None:
    """Withdrawals beyond the held notional are rejected and not mutated."""
    entity = FundingHedgeEntity()
    entity.action_deposit(1_000.0)
    with pytest.raises(EntityException, match="requested"):
        entity.action_withdraw(2_000.0)
    assert entity.internal_state.notional == pytest.approx(1_000.0)


def test_deposit_rejects_negative_amount() -> None:
    """Deposits must be non-negative — same hygiene as the other entities."""
    entity = FundingHedgeEntity()
    with pytest.raises(EntityException, match="non-negative"):
        entity.action_deposit(-1.0)
    assert entity.internal_state.notional == pytest.approx(0.0)


# ----------------------------------------------------------------------
# Balance property
# ----------------------------------------------------------------------

def test_balance_equals_accrued_pnl() -> None:
    """``balance`` is exactly ``accrued_pnl`` — the entity holds no cash."""
    entity = FundingHedgeEntity()
    entity.update_state(_state(funding_rate=0.10, timestamp_seconds=T0))
    entity.action_deposit(10_000.0)
    entity.update_state(
        _state(funding_rate=0.10, timestamp_seconds=T0 + SECONDS_PER_YEAR / 2)
    )
    # Half-year at 10% on 10k notional ⇒ 500 USDC accrued.
    assert entity.balance == pytest.approx(500.0, rel=1e-9)
    assert entity.balance == pytest.approx(entity.internal_state.accrued_pnl)
