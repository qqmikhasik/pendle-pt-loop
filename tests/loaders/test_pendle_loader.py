"""Tests for :class:`PendleMarketLoader`.

HTTP is mocked via ``monkeypatch.setattr(requests, "get", ...)`` so the
suite stays hermetic. A single live integration test is gated behind
``PENDLE_INTEGRATION=1``.

The loader hits Pendle's public REST endpoint
``GET /core/v1/{chain_id}/markets/{market}/historical-data`` and parses
its parallel-array response shape.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest
import requests

from pendle_pt_loop.loaders.pendle import (
    PENDLE_REST_BASE,
    PendleMarketLoader,
)


# ----------------------------------------------------------------- helpers


_MARKET = "0x" + "ab" * 20  # 40 hex chars, lower-case.
_EXPIRY_TS = 1_750_000_000  # somewhere in mid-2025
_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
_END = datetime(2025, 1, 2, tzinfo=timezone.utc)


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def _hourly_payload(start_ts: int, count: int) -> dict[str, Any]:
    """Synthesize the Pendle parallel-array payload for ``count`` hourly rows."""
    timestamps: list[int] = []
    implied_apy: list[str] = []
    tvl: list[str] = []
    for i in range(count):
        timestamps.append(start_ts + i * 3600)
        # implied_yield drifts 14% → 10% across the window
        implied_apy.append(f"{0.14 - 0.04 * i / max(count - 1, 1):.6f}")
        tvl.append(f"{1_000_000.0 + i * 10.0:.4f}")
    return {
        "total": count,
        "timestamp_start": timestamps[0],
        "timestamp_end": timestamps[-1],
        "timestamp": timestamps,
        "impliedApy": implied_apy,
        "baseApy": ["0.10"] * count,
        "underlyingApy": ["0.07"] * count,
        "maxApy": ["0.20"] * count,
        "tvl": tvl,
    }


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect Loader._base_path to a tmp dir so cache tests don't pollute cwd."""
    monkeypatch.setenv("DATA_PATH", str(tmp_path))
    yield


# ----------------------------------------------------------------- tests


def test_loader_instantiates_with_sensible_args() -> None:
    loader = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    assert loader.market_address == _MARKET
    assert loader.expiry_timestamp == _EXPIRY_TS
    assert loader.chain_id == 1


def test_extract_then_transform_returns_expected_columns(monkeypatch) -> None:
    start_ts = int(_START.timestamp())
    payload = _hourly_payload(start_ts, count=24)
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: _FakeResponse(payload)
    )
    loader = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    df = loader.read(with_run=True)
    assert list(df.columns) == [
        "pt_price",
        "implied_yield",
        "seconds_to_expiry",
        "pool_liquidity",
    ]
    assert len(df) == 24
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None


def test_seconds_to_expiry_strictly_decreasing(monkeypatch) -> None:
    start_ts = int(_START.timestamp())
    payload = _hourly_payload(start_ts, count=12)
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: _FakeResponse(payload)
    )
    loader = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    df = loader.read(with_run=True)
    diffs = df["seconds_to_expiry"].diff().dropna()
    assert (diffs < 0).all()


def test_seconds_to_expiry_zero_at_or_after_expiry(monkeypatch) -> None:
    # First three timestamps are exactly at expiry; rest are after.
    start_ts = _EXPIRY_TS
    payload = _hourly_payload(start_ts, count=5)
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: _FakeResponse(payload)
    )
    loader = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=datetime.fromtimestamp(start_ts, tz=timezone.utc),
        end_time=datetime.fromtimestamp(start_ts + 4 * 3600, tz=timezone.utc),
    )
    df = loader.read(with_run=True)
    assert (df["seconds_to_expiry"] == 0).all()


def test_pt_price_derived_from_implied_yield(monkeypatch) -> None:
    """Linear pricing: pt_price = 1 - implied_yield * tau."""
    start_ts = int(_START.timestamp())
    payload = _hourly_payload(start_ts, count=4)
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: _FakeResponse(payload)
    )
    loader = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    df = loader.read(with_run=True)
    SECONDS_PER_YEAR = 365.25 * 24 * 3600
    for ts, row in df.iterrows():
        tau = row["seconds_to_expiry"] / SECONDS_PER_YEAR
        expected = max(0.0, min(1.0, 1.0 - row["implied_yield"] * tau))
        assert row["pt_price"] == pytest.approx(expected, abs=1e-9)


def test_read_with_cache_round_trip(monkeypatch) -> None:
    start_ts = int(_START.timestamp())
    payload = _hourly_payload(start_ts, count=6)

    calls = {"n": 0}

    def fake_get(*args: Any, **kwargs: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(payload)

    monkeypatch.setattr(requests, "get", fake_get)
    loader = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    df_fresh = loader.read(with_run=True).copy()
    assert calls["n"] == 1
    # Second loader instance reads cache only — must not hit HTTP again.
    loader2 = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    df_cached = loader2.read(with_run=False)
    assert calls["n"] == 1  # unchanged
    pd.testing.assert_frame_equal(
        df_cached.astype(float), df_fresh.astype(float), check_exact=False
    )


def test_handles_empty_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeResponse(
            {
                "total": 0,
                "timestamp": [],
                "impliedApy": [],
                "tvl": [],
            }
        ),
    )
    loader = PendleMarketLoader(
        market_address=_MARKET,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    df = loader.read(with_run=True)
    assert df.empty
    assert list(df.columns) == [
        "pt_price",
        "implied_yield",
        "seconds_to_expiry",
        "pool_liquidity",
    ]


def test_pendle_rest_url_unchanged() -> None:
    """Lock the base URL so a regression on the constant trips this test."""
    assert PENDLE_REST_BASE == "https://api-v2.pendle.finance/core/v1"


@pytest.mark.skipif(
    not os.getenv("PENDLE_INTEGRATION"),
    reason="set PENDLE_INTEGRATION=1 to enable",
)
def test_integration_pendle_api() -> None:
    """End-to-end live fetch — only runs when explicitly opted into."""
    market = "0xb6ac3d5da138918ac4e84441e924a20daa60dbdd"  # PT-sUSDE-27NOV2025
    expiry = 1764201600  # 2025-11-27 UTC
    start = datetime(2025, 10, 1, tzinfo=timezone.utc)
    end = datetime(2025, 11, 1, tzinfo=timezone.utc)
    loader = PendleMarketLoader(
        market_address=market,
        expiry_timestamp=expiry,
        start_time=start,
        end_time=end,
    )
    df = loader.read(with_run=True)
    assert not df.empty
    assert (df["pt_price"] > 0).all()
    assert (df["pt_price"] <= 1.0).all()
    assert (df["implied_yield"] >= 0).all()
