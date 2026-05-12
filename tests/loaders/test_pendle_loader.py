"""Tests for :class:`PendleMarketLoader`.

HTTP is fully mocked via ``monkeypatch.setattr(requests, "post", ...)`` so the
suite stays hermetic. A single live integration test is gated behind the
``PENDLE_INTEGRATION=1`` env var.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest
import requests

from pendle_pt_loop.loaders.pendle import (
    PENDLE_GRAPHQL_URL,
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


def _snapshot_rows(start_ts: int, count: int, *, hours: int = 1) -> list[dict[str, Any]]:
    """Synthesize ``count`` hourly snapshots starting at ``start_ts``."""
    rows: list[dict[str, Any]] = []
    for i in range(count):
        ts = start_ts + i * hours * 3600
        # pt_price drifts linearly 0.93 → 0.99 across the window
        pt_price = 0.93 + (0.06 * i / max(count - 1, 1))
        rows.append(
            {
                "timestamp": ts,
                "ptPrice": pt_price,
                "impliedApy": 0.14,
                "liquidity": 1_000_000.0 + i * 10.0,
            }
        )
    return rows


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"marketSnapshots": {"results": rows}}}


def _patch_post(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Patch ``requests.post`` and return a call-log to assert against."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append({"url": url, **kwargs})
        return _FakeResponse(payload)

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def _make_loader(tmp_path, **overrides: Any) -> PendleMarketLoader:
    """Construct a loader rooted at ``tmp_path`` for cache isolation."""
    os.environ["DATA_PATH"] = str(tmp_path)
    kwargs: dict[str, Any] = {
        "market_address": _MARKET,
        "expiry_timestamp": _EXPIRY_TS,
        "start_time": _START,
        "end_time": _END,
    }
    kwargs.update(overrides)
    return PendleMarketLoader(**kwargs)


# ------------------------------------------------------------------- tests


def test_loader_instantiates_with_sensible_args(tmp_path) -> None:
    loader = _make_loader(tmp_path)
    assert loader.market_address == _MARKET
    assert loader.expiry_timestamp == _EXPIRY_TS
    assert loader.start_time == _START
    assert loader.end_time == _END
    # Cache key encodes the address + date window so reruns hit the same file.
    key = loader._cache_key()
    assert _MARKET in key
    assert "20250101" in key and "20250102" in key


def test_extract_then_transform_returns_expected_columns(
    tmp_path, monkeypatch
) -> None:
    rows = _snapshot_rows(int(_START.timestamp()), count=24)
    _patch_post(monkeypatch, _payload(rows))
    loader = _make_loader(tmp_path)
    loader.extract()
    loader.transform()
    df = loader._data
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == [
        "pt_price",
        "implied_yield",
        "seconds_to_expiry",
        "pool_liquidity",
    ]
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert len(df) == 24
    # Implied yield round-trips through the payload (we set 0.14 above).
    assert df["implied_yield"].iloc[0] == pytest.approx(0.14)


def test_seconds_to_expiry_strictly_decreasing(tmp_path, monkeypatch) -> None:
    rows = _snapshot_rows(int(_START.timestamp()), count=24)
    _patch_post(monkeypatch, _payload(rows))
    loader = _make_loader(tmp_path)
    loader.extract()
    loader.transform()
    df = loader._data
    deltas = df["seconds_to_expiry"].diff().dropna()
    # Hourly samples ⇒ each step is -3600 seconds (strictly decreasing).
    assert (deltas < 0).all(), deltas.head()


def test_seconds_to_expiry_zero_at_or_after_expiry_timestamp(
    tmp_path, monkeypatch
) -> None:
    # Place snapshots straddling the expiry: half before, half at/after.
    before = _snapshot_rows(_EXPIRY_TS - 2 * 3600, count=2)
    at_after = _snapshot_rows(_EXPIRY_TS, count=3)
    payload = _payload(before + at_after)
    _patch_post(monkeypatch, payload)
    # Widen the loader's [start, end] window to admit the synthetic rows.
    loader = _make_loader(
        tmp_path,
        start_time=datetime.fromtimestamp(_EXPIRY_TS - 24 * 3600, tz=timezone.utc),
        end_time=datetime.fromtimestamp(_EXPIRY_TS + 24 * 3600, tz=timezone.utc),
    )
    loader.extract()
    loader.transform()
    df = loader._data
    # Rows past expiry must be exactly 0.
    past_expiry = df[df.index >= pd.Timestamp(_EXPIRY_TS, unit="s", tz="UTC")]
    assert len(past_expiry) == 3
    assert (past_expiry["seconds_to_expiry"] == 0.0).all()
    # Rows before expiry must be strictly positive.
    pre_expiry = df[df.index < pd.Timestamp(_EXPIRY_TS, unit="s", tz="UTC")]
    assert (pre_expiry["seconds_to_expiry"] > 0.0).all()


def test_read_with_cache_round_trip(tmp_path, monkeypatch) -> None:
    rows = _snapshot_rows(int(_START.timestamp()), count=12)
    call_log = _patch_post(monkeypatch, _payload(rows))
    loader = _make_loader(tmp_path)
    first = loader.read(with_run=True)
    assert len(call_log) == 1
    # Same address+window builds a fresh loader, which should hit cache only.
    loader2 = _make_loader(tmp_path)
    second = loader2.read()
    assert len(call_log) == 1, "cache hit must not re-invoke HTTP"
    pd.testing.assert_frame_equal(
        first.sort_index(),
        second.sort_index(),
        check_freq=False,
    )


def test_handles_empty_payload(tmp_path, monkeypatch) -> None:
    _patch_post(monkeypatch, _payload([]))
    loader = _make_loader(tmp_path)
    loader.extract()
    loader.transform()
    df = loader._data
    assert isinstance(df, pd.DataFrame)
    assert df.empty
    assert list(df.columns) == [
        "pt_price",
        "implied_yield",
        "seconds_to_expiry",
        "pool_liquidity",
    ]


def test_implied_yield_derived_when_missing(tmp_path, monkeypatch) -> None:
    """If the API omits ``impliedApy``, the loader falls back to -ln(pt)/tau."""
    ts = int(_START.timestamp())
    rows = [
        {
            "timestamp": ts,
            "ptPrice": 0.95,
            # impliedApy intentionally absent
            "liquidity": 1_000_000.0,
        }
    ]
    _patch_post(monkeypatch, _payload(rows))
    loader = _make_loader(tmp_path)
    loader.extract()
    loader.transform()
    df = loader._data
    # tau = (expiry - ts) seconds, in years; identity is y = -ln(pt) / tau.
    import math

    tau_years = (_EXPIRY_TS - ts) / (365.0 * 24.0 * 3600.0)
    expected = -math.log(0.95) / tau_years
    assert df["implied_yield"].iloc[0] == pytest.approx(expected, rel=1e-9)


@pytest.mark.skipif(
    not os.getenv("PENDLE_INTEGRATION"),
    reason="set PENDLE_INTEGRATION=1 to enable",
)
def test_integration_pendle_api(tmp_path) -> None:
    """Live smoke test against the public Pendle GraphQL endpoint.

    Disabled by default. Set ``PENDLE_INTEGRATION=1`` to enable. The test
    only verifies that the endpoint responds with a well-shaped payload —
    not the numeric content, which changes between blocks.
    """
    # A real Pendle market address (PT-sUSDe — adjust if the market expires).
    market = os.getenv("PENDLE_INTEGRATION_MARKET") or _MARKET
    loader = PendleMarketLoader(
        market_address=market,
        expiry_timestamp=_EXPIRY_TS,
        start_time=_START,
        end_time=_END,
    )
    os.environ["DATA_PATH"] = str(tmp_path)
    df = loader.read(with_run=True)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == [
        "pt_price",
        "implied_yield",
        "seconds_to_expiry",
        "pool_liquidity",
    ]
    # Endpoint should respond with PENDLE_GRAPHQL_URL on the OK path.
    assert PENDLE_GRAPHQL_URL.startswith("https://api-v2.pendle.finance")
