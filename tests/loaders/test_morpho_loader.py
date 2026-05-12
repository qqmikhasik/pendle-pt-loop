"""Unit tests for :mod:`pendle_pt_loop.loaders.morpho` — Session 3.

The tests mock the Morpho GraphQL endpoint via ``monkeypatch``; no live
network traffic is required in the default suite. One opt-in integration
test hits the real API when ``MORPHO_INTEGRATION=1`` is set in the env.

Mocked market id values are taken from real Morpho Blue markets so the
shape of the test data matches what a downstream component would see.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from fractal.loaders.base_loader import LoaderType
from pendle_pt_loop.loaders.morpho import (
    MORPHO_GRAPHQL_URL,
    MorphoMarketLoader,
)


# Real Morpho Blue market id for PT-sUSDE-31JUL2025 / USDC on Ethereum.
# Used here purely as a realistic shaped input for unit tests; no live
# fetch happens unless ``MORPHO_INTEGRATION`` is set.
PT_SUSDE_USDC_ETHEREUM: str = (
    "0xbc552f0b14dd6f8e60b760a534ac1d8613d3539153b4d9675d697e048f2edc7e"
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _window(hours: int = 3) -> tuple[datetime, datetime]:
    """Return a fixed UTC ``(start, end)`` window of length ``hours``."""
    start = datetime(2025, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    return start, start + timedelta(hours=hours)


def _mock_payload(start_ts: int, n: int = 4) -> dict[str, Any]:
    """Synthesize a Morpho GraphQL response with ``n`` hourly snapshots."""
    borrow = [
        {"x": start_ts + i * 3600, "y": 0.05 + 0.001 * i} for i in range(n)
    ]
    supply = [
        {"x": start_ts + i * 3600, "y": 0.025 + 0.0005 * i} for i in range(n)
    ]
    util = [
        {"x": start_ts + i * 3600, "y": 0.40 + 0.05 * i} for i in range(n)
    ]
    return {
        "data": {
            "marketByUniqueKey": {
                "historicalState": {
                    "borrowApy": borrow,
                    "supplyApy": supply,
                    "utilization": util,
                },
            },
        },
    }


def _install_http_mock(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> dict[str, int]:
    """Patch ``HttpClient.post`` to return ``payload`` and count calls."""
    counter = {"calls": 0}

    def _post(self: Any, url: str, **_: Any) -> dict[str, Any]:
        counter["calls"] += 1
        assert url == MORPHO_GRAPHQL_URL
        return payload

    monkeypatch.setattr(
        "fractal.loaders._http.HttpClient.post", _post, raising=True
    )
    return counter


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------

def test_loader_instantiates() -> None:
    start, end = _window()
    loader = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
    )
    assert loader.market_id == PT_SUSDE_USDC_ETHEREUM
    assert loader.chain == "ethereum"
    # Cache key must distinguish chain, market, and both endpoints.
    key = loader._cache_key()
    assert "ethereum" in key
    assert PT_SUSDE_USDC_ETHEREUM in key
    assert str(int(start.timestamp())) in key
    assert str(int(end.timestamp())) in key


def test_loader_rejects_unknown_chain() -> None:
    start, end = _window()
    with pytest.raises(ValueError, match="unsupported chain"):
        MorphoMarketLoader(
            market_id=PT_SUSDE_USDC_ETHEREUM,
            chain="solana",
            start_time=start,
            end_time=end,
        )


def test_loader_rejects_bad_market_id() -> None:
    start, end = _window()
    with pytest.raises(ValueError, match="market_id"):
        MorphoMarketLoader(
            market_id="not-a-hex-id",
            chain="ethereum",
            start_time=start,
            end_time=end,
        )


def test_loader_rejects_reversed_window() -> None:
    start, end = _window()
    with pytest.raises(ValueError, match="precedes"):
        MorphoMarketLoader(
            market_id=PT_SUSDE_USDC_ETHEREUM,
            chain="ethereum",
            start_time=end,
            end_time=start,
        )


# ----------------------------------------------------------------------
# extract / transform contract
# ----------------------------------------------------------------------

def test_extract_then_transform_returns_expected_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start, end = _window(hours=4)
    payload = _mock_payload(start_ts=int(start.timestamp()), n=4)
    _install_http_mock(monkeypatch, payload)

    loader = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
    )
    loader.extract()
    loader.transform()
    df = loader._data
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["borrowing_rate", "utilization", "supply_apy"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None  # UTC-aware
    assert df.index.name == "time"
    assert len(df) == 4
    # All numeric.
    assert df.dtypes.apply(lambda d: d.kind == "f").all()


def test_borrowing_rate_in_reasonable_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start, end = _window(hours=5)
    payload = _mock_payload(start_ts=int(start.timestamp()), n=5)
    _install_http_mock(monkeypatch, payload)

    loader = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
    )
    loader.extract()
    loader.transform()
    rates = loader._data["borrowing_rate"]
    # Sanity: rates expressed as decimal APY, in [0, 1] for our fixtures.
    assert (rates >= 0.0).all()
    assert (rates < 1.0).all()


def test_utilization_in_zero_one_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start, end = _window(hours=5)
    payload = _mock_payload(start_ts=int(start.timestamp()), n=5)
    _install_http_mock(monkeypatch, payload)

    loader = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
    )
    loader.extract()
    loader.transform()
    util = loader._data["utilization"]
    assert (util >= 0.0).all()
    assert (util <= 1.0).all()


def test_handles_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing market or empty historical state returns a shaped empty DF."""
    start, end = _window()
    _install_http_mock(monkeypatch, {"data": {"marketByUniqueKey": None}})

    loader = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
    )
    loader.extract()
    loader.transform()
    df = loader._data
    assert isinstance(df, pd.DataFrame)
    assert df.empty
    assert list(df.columns) == ["borrowing_rate", "utilization", "supply_apy"]
    assert isinstance(df.index, pd.DatetimeIndex)


def test_graphql_errors_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    start, end = _window()
    _install_http_mock(
        monkeypatch,
        {"errors": [{"message": "Field not found"}]},
    )
    loader = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
    )
    with pytest.raises(RuntimeError, match="Morpho GraphQL errors"):
        loader.extract()


# ----------------------------------------------------------------------
# Cache round trip
# ----------------------------------------------------------------------

def test_read_with_cache_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """First ``read(with_run=True)`` populates cache; second ``read()`` reads back without HTTP."""
    monkeypatch.setenv("DATA_PATH", str(tmp_path))

    start, end = _window(hours=4)
    payload = _mock_payload(start_ts=int(start.timestamp()), n=4)
    counter = _install_http_mock(monkeypatch, payload)

    loader_a = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
        loader_type=LoaderType.CSV,
    )
    df_first = loader_a.read(with_run=True)
    assert counter["calls"] == 1
    assert len(df_first) == 4
    assert list(df_first.columns) == ["borrowing_rate", "utilization", "supply_apy"]

    # Fresh loader hitting the cache only — no further HTTP calls.
    loader_b = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
        loader_type=LoaderType.CSV,
    )
    df_second = loader_b.read(with_run=False)
    assert counter["calls"] == 1  # still one — cache hit
    assert list(df_second.columns) == ["borrowing_rate", "utilization", "supply_apy"]
    assert isinstance(df_second.index, pd.DatetimeIndex)
    assert df_second.index.tz is not None
    # Values match.
    pd.testing.assert_frame_equal(
        df_first.reset_index(drop=True).astype(float),
        df_second.reset_index(drop=True).astype(float),
    )


# ----------------------------------------------------------------------
# Live integration (opt-in)
# ----------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("MORPHO_INTEGRATION"),
    reason="set MORPHO_INTEGRATION=1 to hit the live Morpho GraphQL endpoint",
)
def test_integration_morpho_api() -> None:
    """End-to-end: a real GraphQL roundtrip for a real PT/USDC market."""
    end = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=24)
    loader = MorphoMarketLoader(
        market_id=PT_SUSDE_USDC_ETHEREUM,
        chain="ethereum",
        start_time=start,
        end_time=end,
    )
    df = loader.read(with_run=True)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["borrowing_rate", "utilization", "supply_apy"]
    if not df.empty:
        assert (df["utilization"] >= 0).all() and (df["utilization"] <= 1).all()
        assert (df["borrowing_rate"] >= 0).all()
        assert (df["borrowing_rate"] < 2.0).all()
