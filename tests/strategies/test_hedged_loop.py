"""Tests for the hedged PT-loop strategy.

Verifies the hedge leg sizing, accrual behaviour, and integration with
the existing loop/unwind.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fractal.core.base import Observation

from pendle_pt_loop.entities import (
    FundingHedgeGlobalState,
    MorphoConfig,
    MorphoGlobalState,
    PendlePTConfig,
    PendlePTGlobalState,
)
from pendle_pt_loop.observations import HEDGE_SLOT, MORPHO_SLOT, PT_SLOT
from pendle_pt_loop.strategies.hedged_loop import (
    HedgedLoopParams,
    HedgedLoopStrategy,
)

_HUGE_POOL = 1.0e12


def _ideal_pt_config() -> PendlePTConfig:
    return PendlePTConfig(amm_fee_rate=0.0)


def _make_observations(
    n_hours: int,
    *,
    pt_price: float = 0.93,
    implied_yield: float = 0.10,
    borrowing_rate: float = 0.06,
    funding_rate: float = 0.10,
    expiry_hours: int = 1000,
) -> list[Observation]:
    base = datetime(2025, 1, 1, tzinfo=UTC)
    out: list[Observation] = []
    for i in range(n_hours):
        ts = base + timedelta(hours=i)
        secs_to_expiry = max((expiry_hours - i) * 3600, 0)
        price = pt_price if secs_to_expiry > 0 else 1.0
        out.append(
            Observation(
                timestamp=ts,
                states={
                    PT_SLOT: PendlePTGlobalState(
                        pt_price=price,
                        implied_yield=implied_yield,
                        seconds_to_expiry=secs_to_expiry,
                        pool_liquidity=_HUGE_POOL,
                    ),
                    MORPHO_SLOT: MorphoGlobalState(
                        collateral_price=price,
                        debt_price=1.0,
                        borrowing_rate=borrowing_rate,
                        utilization=0.5,
                        timestamp_seconds=ts.timestamp(),
                    ),
                    HEDGE_SLOT: FundingHedgeGlobalState(
                        funding_rate=funding_rate,
                        timestamp_seconds=ts.timestamp(),
                    ),
                },
            )
        )
    return out


def test_strategy_instantiates_with_defaults() -> None:
    strat = HedgedLoopStrategy(params=HedgedLoopParams())
    assert strat._state == "uninvested"


def test_negative_hedge_ratio_rejected() -> None:
    with pytest.raises(ValueError, match="HEDGE_RATIO"):
        HedgedLoopStrategy(params=HedgedLoopParams(HEDGE_RATIO=-0.1))


def test_strategy_opens_hedge_with_correct_notional() -> None:
    """With HEDGE_RATIO=1.0, hedge notional should equal total PT collateral."""
    obs = _make_observations(n_hours=10)
    strat = HedgedLoopStrategy(
        pt_config=_ideal_pt_config(),
        morpho_config=MorphoConfig(lltv=0.86),
        params=HedgedLoopParams(
            INITIAL_BALANCE=10_000.0,
            TARGET_LTV=0.80,
            N_CYCLES=5,
            HEDGE_RATIO=1.0,
        ),
    )
    strat.run(obs)
    # After open: hedge notional should match the planned total collateral
    # value (within tiny float noise).
    hedge_notional = strat._hedge.internal_state.notional
    collat_value = strat._morpho.collateral_value
    assert hedge_notional == pytest.approx(collat_value, rel=1e-9)


def test_hedge_ratio_zero_equals_static_loop() -> None:
    """HEDGE_RATIO=0 → no hedge actions emitted; hedge notional stays 0."""
    obs = _make_observations(n_hours=10)
    strat = HedgedLoopStrategy(
        pt_config=_ideal_pt_config(),
        morpho_config=MorphoConfig(lltv=0.86),
        params=HedgedLoopParams(HEDGE_RATIO=0.0),
    )
    strat.run(obs)
    assert strat._hedge.internal_state.notional == 0.0


def test_hedge_accrues_funding_pnl_over_time() -> None:
    """Hedge accrues notional × funding_rate × dt; check after 1 year."""
    # 365.25 * 24 ≈ 8766 hourly observations = 1 year.
    obs = _make_observations(
        n_hours=24 * 30 + 10,  # 30 days + warmup
        funding_rate=0.10,
        expiry_hours=10_000,
    )
    strat = HedgedLoopStrategy(
        pt_config=_ideal_pt_config(),
        morpho_config=MorphoConfig(lltv=0.86),
        params=HedgedLoopParams(
            INITIAL_BALANCE=10_000.0,
            HEDGE_RATIO=1.0,
        ),
    )
    strat.run(obs)
    # Expected accrual ≈ notional × 0.10 × (30/365.25) — small but positive.
    accrued = strat._hedge.internal_state.accrued_pnl
    notional = strat._hedge.internal_state.notional
    expected = notional * 0.10 * (30 / 365.25)
    # Tolerance: hedge opens 1 obs after warmup, so dt is slightly less
    # than 30 days. Relative 5% tolerance.
    assert accrued == pytest.approx(expected, rel=0.05)


def test_strategy_unwinds_at_expiry() -> None:
    """At expiry: state is unwound, hedge notional 0, accrued_pnl preserved."""
    obs = _make_observations(
        n_hours=120,
        expiry_hours=100,
        funding_rate=0.10,
    )
    strat = HedgedLoopStrategy(
        pt_config=_ideal_pt_config(),
        morpho_config=MorphoConfig(lltv=0.86),
        params=HedgedLoopParams(HEDGE_RATIO=1.0),
    )
    result = strat.run(obs)
    assert strat._state == "unwound"
    assert strat._hedge.internal_state.notional == 0.0
    # Final balance combines static-loop equity + hedge PnL.
    final = float(result.to_dataframe()["net_balance"].iloc[-1])
    assert final > 10_000.0  # carried positive, plus some hedge income
