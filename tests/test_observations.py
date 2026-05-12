"""Tests for the cross-feed Observation builder.

Exercises the pure-join helper ``_join_and_pack`` so we don't need to
mock three HTTP clients to assert the output shape. The live
``build_observations`` wrapper is covered indirectly: the loaders have
their own mocked unit tests, and the pure join is what binds them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from pendle_pt_loop.observations import (
    MORPHO_SLOT,
    PT_SLOT,
    _join_and_pack,
)


def _hourly_index(n: int, start: datetime | None = None) -> pd.DatetimeIndex:
    base = start or datetime(2024, 6, 1, tzinfo=UTC)
    return pd.DatetimeIndex(
        [base + timedelta(hours=i) for i in range(n)], tz=UTC
    )


def _pendle_frame(n: int = 5) -> pd.DataFrame:
    idx = _hourly_index(n)
    return pd.DataFrame(
        {
            "pt_price": [0.930 + 0.0001 * i for i in range(n)],
            "implied_yield": [0.140 - 0.0005 * i for i in range(n)],
            "seconds_to_expiry": [180 * 86400 - 3600 * i for i in range(n)],
            "pool_liquidity": [1e7 + 1000.0 * i for i in range(n)],
        },
        index=idx,
    )


def _morpho_frame(n: int = 5) -> pd.DataFrame:
    idx = _hourly_index(n)
    return pd.DataFrame(
        {
            "borrowing_rate": [0.060 + 0.001 * i for i in range(n)],
            "utilization": [0.70 + 0.01 * i for i in range(n)],
            "supply_apy": [0.040 + 0.0005 * i for i in range(n)],
        },
        index=idx,
    )


def _susde_frame(n: int = 5) -> pd.DataFrame:
    idx = _hourly_index(n)
    return pd.DataFrame({"price": [1.0001 + 0.00001 * i for i in range(n)]}, index=idx)


def test_join_produces_one_observation_per_row() -> None:
    obs, susde = _join_and_pack(_pendle_frame(5), _morpho_frame(5), _susde_frame(5))
    assert len(obs) == 5
    assert len(susde) == 5


def test_join_uses_inner_join_drops_unmatched() -> None:
    # Morpho missing the last two slots → only 3 joined rows.
    obs, _ = _join_and_pack(_pendle_frame(5), _morpho_frame(3), _susde_frame(5))
    assert len(obs) == 3


def test_observation_states_carry_pt_and_morpho_slots() -> None:
    obs, _ = _join_and_pack(_pendle_frame(3), _morpho_frame(3), _susde_frame(3))
    states = obs[0].states
    assert set(states.keys()) == {PT_SLOT, MORPHO_SLOT}


def test_pt_state_fields_carry_through_from_pendle_feed() -> None:
    pendle_df = _pendle_frame(2)
    obs, _ = _join_and_pack(pendle_df, _morpho_frame(2), _susde_frame(2))
    pt = obs[0].states[PT_SLOT]
    assert pt.pt_price == pytest.approx(0.930)
    assert pt.implied_yield == pytest.approx(0.140)
    assert pt.seconds_to_expiry == pytest.approx(180 * 86400)
    assert pt.pool_liquidity == pytest.approx(1e7)


def test_morpho_state_collateral_price_equals_pt_price() -> None:
    """The Morpho oracle prices PT at the Pendle mark — we mirror that
    so the entity's ltv / health-factor read the same value as the loop
    strategy sees on the Pendle side."""
    obs, _ = _join_and_pack(_pendle_frame(2), _morpho_frame(2), _susde_frame(2))
    m = obs[0].states[MORPHO_SLOT]
    pt = obs[0].states[PT_SLOT]
    assert m.collateral_price == pytest.approx(pt.pt_price)


def test_morpho_state_debt_price_is_one_for_usdc() -> None:
    obs, _ = _join_and_pack(_pendle_frame(2), _morpho_frame(2), _susde_frame(2))
    assert obs[0].states[MORPHO_SLOT].debt_price == pytest.approx(1.0)


def test_morpho_state_timestamp_seconds_matches_observation_timestamp() -> None:
    obs, _ = _join_and_pack(_pendle_frame(3), _morpho_frame(3), _susde_frame(3))
    for o in obs:
        ts_expected = o.timestamp.timestamp()
        assert o.states[MORPHO_SLOT].timestamp_seconds == pytest.approx(ts_expected)


def test_susde_aligned_to_joined_index() -> None:
    # sUSDe has more rows than the join — should be reindexed to match.
    obs, susde_aligned = _join_and_pack(
        _pendle_frame(3), _morpho_frame(3), _susde_frame(10)
    )
    assert len(susde_aligned) == len(obs) == 3


def test_empty_input_returns_empty_output() -> None:
    obs, susde = _join_and_pack(
        pd.DataFrame(columns=["pt_price", "implied_yield", "seconds_to_expiry", "pool_liquidity"]),
        _morpho_frame(3),
        _susde_frame(3),
    )
    assert obs == []
    # sUSDe is passed through (it's an independent series).
    assert len(susde) == 3


def test_forward_fill_within_feed_before_join() -> None:
    """Holes in PT data (e.g. one missing hour) are forward-filled
    before the inner join — so we don't drop the matching Morpho row."""
    pendle = _pendle_frame(5).copy()
    pendle.iloc[2] = float("nan")  # punch a hole in the third row
    obs, _ = _join_and_pack(pendle, _morpho_frame(5), _susde_frame(5))
    # All 5 rows survive: the NaN slot gets forward-filled from row 1.
    assert len(obs) == 5
    # Forward-filled values equal the previous row.
    assert obs[2].states[PT_SLOT].pt_price == pytest.approx(
        obs[1].states[PT_SLOT].pt_price
    )
