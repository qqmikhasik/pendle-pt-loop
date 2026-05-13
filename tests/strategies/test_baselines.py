"""Unit + integration tests for the three baseline strategies.

We build synthetic ``Observation`` streams (no HTTP) and run each
strategy end-to-end against them. The integration sanity test
(`test_hold_pt_realized_apy_matches_implied_within_bps`) is the most
important: it confirms entity math + strategy logic + observation
shape all line up to within tens of bps of friction (AMM fee +
slippage on the single buy).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Callable

import pytest
from fractal.core.base import Observation

from pendle_pt_loop.entities.pendle_pt import (
    SECONDS_PER_YEAR,
    PendlePTGlobalState,
)
from pendle_pt_loop.observations import PT_SLOT
from pendle_pt_loop.strategies.baselines import (
    BaselineParams,
    HoldPTNoLeverageStrategy,
    HoldSUSDeStrategy,
    HoldUSDCStrategy,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_observations(
    n_hours: int,
    expiry_in_hours: int,
    implied_yield: float,
    pt_price_func: Callable[[int], float] | None = None,
    pool_liquidity: float = 1e12,
    start: datetime | None = None,
) -> list[Observation]:
    """Hand-built hourly observation stream for baseline tests.

    Args:
        n_hours: Number of hourly observations.
        expiry_in_hours: Hours from the first observation until expiry;
            ``seconds_to_expiry`` counts down to 0 at exactly that mark
            and stays clamped at 0 afterwards.
        implied_yield: Constant implied yield in decimal (annualised).
        pt_price_func: Optional function from hour-index to ``pt_price``.
            Defaults to the linear-form price implied by ``implied_yield``
            and the remaining tenor; if a row sits past expiry the
            function output is overridden to 1.0 by the entity anyway.
        pool_liquidity: Constant pool liquidity (USDC). Default 1e12 so
            slippage on 10k-USDC trades is negligible.
        start: Start datetime in UTC; defaults to 2024-06-01 UTC.

    Returns:
        A list of ``Observation`` objects, one per hour.
    """
    base = start or datetime(2024, 6, 1, tzinfo=UTC)
    expiry_ts = base + timedelta(hours=expiry_in_hours)

    def _default_price(i: int) -> float:
        tau_seconds = max((expiry_ts - (base + timedelta(hours=i))).total_seconds(), 0.0)
        tau_years = tau_seconds / SECONDS_PER_YEAR
        return max(1.0 - implied_yield * tau_years, 0.0)

    price_fn = pt_price_func or _default_price
    obs: list[Observation] = []
    for i in range(n_hours):
        ts = base + timedelta(hours=i)
        seconds_to_expiry = max((expiry_ts - ts).total_seconds(), 0.0)
        state = PendlePTGlobalState(
            pt_price=price_fn(i),
            implied_yield=implied_yield,
            seconds_to_expiry=seconds_to_expiry,
            pool_liquidity=pool_liquidity,
        )
        obs.append(Observation(timestamp=ts, states={PT_SLOT: state}))
    return obs


# ----------------------------------------------------------------------
# HoldUSDC
# ----------------------------------------------------------------------


def test_hold_usdc_keeps_equity_flat() -> None:
    """Cash-only baseline: equity equals INITIAL_BALANCE for every tick."""
    obs = _make_observations(
        n_hours=100,
        expiry_in_hours=180 * 24,
        implied_yield=0.14,
        # Vary pt_price wildly to prove cash-only baseline is unaffected.
        pt_price_func=lambda i: 0.5 + 0.005 * (i % 20),
    )
    strategy = HoldUSDCStrategy(params=BaselineParams(INITIAL_BALANCE=10_000.0))
    result = strategy.run(obs)
    final = result.balances[-1][PT_SLOT]
    assert final == pytest.approx(10_000.0, rel=1e-12)
    # Internal: no PT bought, all cash.
    pt = strategy.get_entity(PT_SLOT)
    assert pt.internal_state.pt_face_amount == 0.0
    assert pt.internal_state.cash == pytest.approx(10_000.0, rel=1e-12)


# ----------------------------------------------------------------------
# HoldSUSDe
# ----------------------------------------------------------------------


def test_hold_susde_accrues_implied_yield() -> None:
    """365*24 hourly ticks at 10% implied yield → ≈ 10_000 × e^{0.10}.

    We accrue cash * r * dt per tick where dt is the change in
    seconds_to_expiry. The result is *continuous compounding* on the
    cash balance: final ≈ 10_000 × e^{0.10} ≈ 11_051.7, not the
    simple-interest 11_000. Both are within 1% of the "≈ 10_000 × 1.10"
    spec target, so we assert at rel=0.01.
    """
    obs = _make_observations(
        n_hours=365 * 24,
        # Expiry is far past the window so the strategy never sees it.
        expiry_in_hours=10 * 365 * 24,
        implied_yield=0.10,
    )
    strategy = HoldSUSDeStrategy(params=BaselineParams(INITIAL_BALANCE=10_000.0))
    result = strategy.run(obs)
    final = result.balances[-1][PT_SLOT]
    # Permissive: anywhere between simple 11_000 and continuous 11_052.
    assert final == pytest.approx(11_000.0, rel=0.01)
    # Tighter: confirm we are on the continuous-compounding side, not
    # accidentally producing simple interest or doubling-up.
    assert final == pytest.approx(10_000.0 * math.exp(0.10), rel=5e-3)


def test_hold_susde_first_tick_does_not_accrue() -> None:
    """On the very first observation no time has elapsed -- we deposit
    INITIAL_BALANCE and credit zero yield. After one tick balance is
    exactly INITIAL_BALANCE."""
    obs = _make_observations(
        n_hours=1, expiry_in_hours=365 * 24, implied_yield=0.10
    )
    strategy = HoldSUSDeStrategy(params=BaselineParams(INITIAL_BALANCE=10_000.0))
    result = strategy.run(obs)
    assert result.balances[-1][PT_SLOT] == pytest.approx(10_000.0, rel=1e-12)


# ----------------------------------------------------------------------
# HoldPTNoLeverage
# ----------------------------------------------------------------------


def test_hold_pt_no_leverage_redeem_at_expiry() -> None:
    """Buy at t0, redeem at expiry → final balance ≈ INITIAL/pt_price_0
    minus the tiny AMM fee+slippage on the one buy.

    With pt_price_at_entry ≈ 1 - 0.10 * 0.5 = 0.95, the no-friction
    cap is 10_000 / 0.95 ≈ 10_526. Fee (10 bps) + slippage on a 10k
    trade against a 1e12 pool (~5e-9) drop the effective face by
    ~ 10 bps. We assert at rel=2e-3.
    """
    expiry_h = 180 * 24  # 6 months
    n_h = expiry_h + 1  # one observation past expiry to allow redeem
    obs = _make_observations(
        n_hours=n_h, expiry_in_hours=expiry_h, implied_yield=0.10
    )
    strategy = HoldPTNoLeverageStrategy(
        params=BaselineParams(INITIAL_BALANCE=10_000.0)
    )
    result = strategy.run(obs)
    pt = strategy.get_entity(PT_SLOT)
    # All PT redeemed, no PT held at the end.
    assert pt.internal_state.pt_face_amount == 0.0
    # Final cash ≈ initial / pt_price_at_entry (i.e. the face redeemed
    # at par), shaved by AMM friction on the buy.
    pt_price_t0 = obs[0].states[PT_SLOT].pt_price
    no_friction_face = 10_000.0 / pt_price_t0
    final = result.balances[-1][PT_SLOT]
    assert final == pytest.approx(no_friction_face, rel=2e-3)
    # Sanity: final must be strictly less than the no-friction cap
    # (we paid a fee) but strictly more than 10_000 (we made positive
    # return).
    assert 10_000.0 < final < no_friction_face


def _present_value_pricer(implied: float, expiry_seconds: float) -> Callable[[int], float]:
    """Return a pricing function ``pt_price = 1 / (1 + r * tau_remaining)``.

    With this pricing the simple-annualised realised return on a
    hold-PT-to-expiry trade equals ``r`` exactly in the no-friction
    limit — the cleanest test target for the integration sanity check.
    """

    def _price(i: int) -> float:
        seconds_remaining = max(expiry_seconds - i * 3600.0, 0.0)
        tau_remaining = seconds_remaining / SECONDS_PER_YEAR
        return 1.0 / (1.0 + implied * tau_remaining)

    return _price


def test_hold_pt_realized_apy_matches_implied_within_bps() -> None:
    """Integration sanity: realised APY on hold-PT ≈ implied yield at
    entry within ±10 bps after accounting for mock AMM friction.

    We use the present-value pricer (see helper above) so that
    no-friction realised APY equals ``r`` exactly. The AMM fee (10
    bps on the single buy) is then the only drift — closed-form
    deficit is ``fee/tau + fee*r = 11`` bps over a 1-year tau.
    """
    tau_years = 1.0
    expiry_h = int(round(tau_years * 365.25 * 24))
    n_h = expiry_h + 1
    implied = 0.10
    obs = _make_observations(
        n_hours=n_h,
        expiry_in_hours=expiry_h,
        implied_yield=implied,
        pt_price_func=_present_value_pricer(implied, expiry_h * 3600.0),
    )
    strategy = HoldPTNoLeverageStrategy(
        params=BaselineParams(INITIAL_BALANCE=10_000.0)
    )
    result = strategy.run(obs)
    final = result.balances[-1][PT_SLOT]
    actual_tau_years = (
        obs[0].states[PT_SLOT].seconds_to_expiry / SECONDS_PER_YEAR
    )
    realised_apy = (final / 10_000.0 - 1.0) / actual_tau_years
    # 12 bps tolerance: 11 bps closed-form deficit + 1 bp discretisation.
    assert realised_apy == pytest.approx(implied, abs=12e-4)


def test_hold_pt_no_leverage_idles_between_open_and_expiry() -> None:
    """Sanity: after opening, no further trades happen until expiry.

    Confirmed by inspecting the PT face amount: it must be set on tick
    0 and remain unchanged through the last pre-expiry tick.
    """
    expiry_h = 100
    obs = _make_observations(
        n_hours=expiry_h, expiry_in_hours=expiry_h + 50, implied_yield=0.10
    )
    strategy = HoldPTNoLeverageStrategy(
        params=BaselineParams(INITIAL_BALANCE=10_000.0)
    )
    result = strategy.run(obs)
    face_first = result.internal_states[0][PT_SLOT].pt_face_amount
    face_last = result.internal_states[-1][PT_SLOT].pt_face_amount
    assert face_first > 0
    assert face_first == pytest.approx(face_last, rel=1e-12)


# ----------------------------------------------------------------------
# Slot-name contract
# ----------------------------------------------------------------------


def test_strategy_uses_pt_slot_name() -> None:
    """All three baselines register their PT entity under the exact
    string ``"PT"`` (matching ``pendle_pt_loop.observations.PT_SLOT``)."""
    assert PT_SLOT == "PT"
    for cls in (HoldUSDCStrategy, HoldSUSDeStrategy, HoldPTNoLeverageStrategy):
        strategy = cls(params=BaselineParams())
        registered = list(strategy.get_all_available_entities().keys())
        assert registered == [PT_SLOT], (
            f"{cls.__name__} registered {registered!r}, expected ['PT']"
        )
