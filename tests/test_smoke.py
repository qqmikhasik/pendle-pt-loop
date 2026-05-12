"""Cross-entity smoke tests.

Verifies that the package imports cleanly, entities instantiate, accept
``update_state`` + ``action_*`` calls, and report sensible balances at
the composition level. The Session-2 AMM and LLTV mechanics are tested
exhaustively in ``tests/entities/test_pendle_pt.py`` and
``tests/entities/test_morpho.py`` respectively; here we configure pools
to behave as ideal swap venues (huge liquidity + zero fee) so the
conservation checks read clearly.
"""

from __future__ import annotations

import pytest

from pendle_pt_loop.entities import (
    MorphoConfig,
    MorphoEntity,
    MorphoGlobalState,
    PendlePTConfig,
    PendlePTEntity,
    PendlePTGlobalState,
)


# Pool size large enough that ``slippage_factor * trade/pool`` is < 1e-9
# for any trade size we exercise in these smoke tests. This makes the
# AMM behave as an identity swap, which is exactly what the smoke tests
# want to assert against (slippage / fee behaviour is covered separately
# in tests/entities/test_pendle_pt.py).
_INFINITE_POOL: float = 1.0e12


def _ideal_pt_config() -> PendlePTConfig:
    """Zero-fee config; combined with a 1e12 pool gives identity swaps."""
    return PendlePTConfig(amm_fee_rate=0.0)


# ----------------------------------------------------------------------
# Pendle PT entity
# ----------------------------------------------------------------------

def test_pendle_pt_instantiates_with_defaults() -> None:
    entity = PendlePTEntity()
    assert entity.balance == pytest.approx(0.0)
    assert "buy_pt" in entity.get_available_actions()
    assert "sell_pt" in entity.get_available_actions()
    assert "redeem" in entity.get_available_actions()
    assert "deposit" in entity.get_available_actions()
    assert "withdraw" in entity.get_available_actions()


def test_pendle_pt_deposit_buy_and_mark() -> None:
    entity = PendlePTEntity(_ideal_pt_config())
    # PT trading at 0.93 USDC (≈ 14% APY @ 6mo to expiry).
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.93,
            implied_yield=0.14,
            seconds_to_expiry=180 * 24 * 3600,
            pool_liquidity=_INFINITE_POOL,
        )
    )
    entity.action_deposit(amount_in_notional=10_000.0)
    assert entity.balance == pytest.approx(10_000.0)
    # Buy PT with all cash. Ideal-pool config: no fee, negligible slippage.
    entity.action_buy_pt(amount_in_notional=10_000.0)
    expected_face = 10_000.0 / 0.93
    assert entity.internal_state.pt_face_amount == pytest.approx(expected_face)
    assert entity.internal_state.cash == pytest.approx(0.0)
    # Mark-to-market still equals 10k (we paid 10k for 10k of PT at mark).
    assert entity.balance == pytest.approx(10_000.0)


def test_pendle_pt_redeem_blocked_before_expiry() -> None:
    entity = PendlePTEntity(_ideal_pt_config())
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.93,
            implied_yield=0.14,
            seconds_to_expiry=3600.0,
            pool_liquidity=_INFINITE_POOL,
        )
    )
    entity.action_deposit(10_000.0)
    entity.action_buy_pt(10_000.0)
    with pytest.raises(Exception, match="not at expiry"):
        entity.action_redeem(amount_in_face=100.0)


def test_pendle_pt_redeem_at_expiry_pars_value() -> None:
    entity = PendlePTEntity(_ideal_pt_config())
    # Open while PT is at discount.
    entity.update_state(
        PendlePTGlobalState(
            pt_price=0.93,
            seconds_to_expiry=3600.0,
            pool_liquidity=_INFINITE_POOL,
        )
    )
    entity.action_deposit(10_000.0)
    entity.action_buy_pt(10_000.0)
    face_held = entity.internal_state.pt_face_amount
    # Roll to expiry: pt_price snaps to 1.0, seconds_to_expiry to 0.
    entity.update_state(PendlePTGlobalState(pt_price=1.0, seconds_to_expiry=0.0))
    entity.action_redeem(amount_in_face=face_held)
    assert entity.internal_state.pt_face_amount == pytest.approx(0.0)
    assert entity.internal_state.cash == pytest.approx(face_held)
    # Final balance equals face redeemed (≈ 10k / 0.93 ≈ 10,752).
    assert entity.balance == pytest.approx(face_held)


# ----------------------------------------------------------------------
# Morpho entity
# ----------------------------------------------------------------------

def test_morpho_instantiates_with_defaults() -> None:
    entity = MorphoEntity()
    assert entity.balance == pytest.approx(0.0)
    assert "deposit" in entity.get_available_actions()
    assert "withdraw" in entity.get_available_actions()
    assert "borrow" in entity.get_available_actions()
    assert "repay" in entity.get_available_actions()


def test_morpho_health_factor_infinite_at_zero_debt() -> None:
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(MorphoGlobalState(collateral_price=0.93, debt_price=1.0))
    entity.action_deposit(amount_in_notional=10_000.0)  # 10k PT face
    assert entity.collateral_value == pytest.approx(9_300.0)
    assert entity.debt_value == pytest.approx(0.0)
    assert entity.ltv == pytest.approx(0.0)
    assert entity.health_factor == float("inf")
    assert entity.balance == pytest.approx(9_300.0)


def test_morpho_ltv_and_health_factor_after_borrow() -> None:
    entity = MorphoEntity(MorphoConfig(lltv=0.86))
    entity.update_state(MorphoGlobalState(collateral_price=0.93, debt_price=1.0))
    entity.action_deposit(amount_in_notional=10_000.0)  # 9300 USDC collateral
    entity.action_borrow(amount_in_notional=7_440.0)  # 80% LTV on 9300
    assert entity.ltv == pytest.approx(0.80, rel=1e-6)
    assert entity.health_factor == pytest.approx(0.86 / 0.80, rel=1e-6)
    assert entity.balance == pytest.approx(9_300.0 - 7_440.0)


def test_morpho_repay_reduces_debt() -> None:
    entity = MorphoEntity()
    entity.update_state(MorphoGlobalState(collateral_price=0.93, debt_price=1.0))
    entity.action_deposit(10_000.0)
    entity.action_borrow(7_000.0)
    entity.action_repay(2_000.0)
    assert entity.internal_state.debt == pytest.approx(5_000.0)


def test_morpho_overrepay_rejected() -> None:
    entity = MorphoEntity()
    entity.update_state(MorphoGlobalState(collateral_price=0.93, debt_price=1.0))
    entity.action_deposit(10_000.0)
    entity.action_borrow(1_000.0)
    with pytest.raises(Exception, match="only.*debt"):
        entity.action_repay(2_000.0)


# ----------------------------------------------------------------------
# Cross-entity composition (the eventual loop building block)
# ----------------------------------------------------------------------

def test_one_loop_cycle_balances() -> None:
    """One iteration of the PT loop: buy PT, deposit as collateral, borrow USDC.

    Under the ideal-pool config (no fee, negligible slippage) the user's
    total equity (PT entity balance + Morpho equity) is preserved across
    the cycle. This is the topology check we need passing before plugging
    in real Pendle / Morpho data in Session 3.
    """
    pt = PendlePTEntity(_ideal_pt_config())
    morpho = MorphoEntity(MorphoConfig(lltv=0.86))

    pt.update_state(
        PendlePTGlobalState(
            pt_price=0.93,
            seconds_to_expiry=180 * 86400,
            pool_liquidity=_INFINITE_POOL,
        )
    )
    morpho.update_state(MorphoGlobalState(collateral_price=0.93, debt_price=1.0))

    # Step 1: 10k USDC in.
    pt.action_deposit(10_000.0)
    starting_equity = pt.balance + morpho.balance
    assert starting_equity == pytest.approx(10_000.0)

    # Step 2: buy PT.
    pt.action_buy_pt(10_000.0)
    face_bought = pt.internal_state.pt_face_amount

    # Step 3: move PT into Morpho as collateral. The handoff bypasses
    # the action API here — Session 4's loop strategy will route this
    # through ActionToTake; for the smoke test we mutate directly.
    pt.internal_state.pt_face_amount = 0.0  # leaves PT entity
    morpho.action_deposit(face_bought)  # arrives in Morpho

    # Step 4: borrow at LTV=0.80 (within Morpho's lltv=0.86).
    collat_val = morpho.collateral_value
    borrow = 0.80 * collat_val
    morpho.action_borrow(borrow)

    # The borrowed USDC lands back in the PT entity's cash bucket
    # (in real loop the strategy would route it; here we just simulate).
    pt.action_deposit(borrow)

    # Total equity: PT entity balance + Morpho equity.
    total = pt.balance + morpho.balance
    # Under ideal-pool config, conservation holds to within rounding —
    # the residual ~5e-5 USDC is the slippage_factor * trade^2 / pool
    # term that does not fully vanish even at pool=1e12. Within default
    # ``pytest.approx`` tolerance (1e-6 relative) that's a cent on a
    # ten-thousand-dollar position, well under any backtest noise floor.
    assert total == pytest.approx(10_000.0)
