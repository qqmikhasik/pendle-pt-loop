"""Tests for :class:`FundingHedgeLoader`.

The inner :class:`fractal.loaders.hyperliquid.HyperliquidFundingRatesLoader`
is monkeypatched so the suite runs hermetically — no live HTTP traffic.
A single opt-in live-integration test is gated behind
``FUNDING_INTEGRATION=1``.

Hyperliquid quotes funding rates per hour, so the loader's annualisation
contract is ``annualised = hourly * 365.25 * 24`` (≈ 8766). The tests
pin both columns and the magnitude of the conversion.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, List

import numpy as np
import pandas as pd
import pytest

import fractal.loaders.hyperliquid as _hl
from fractal.loaders.base_loader import LoaderType
from fractal.loaders.structs import FundingHistory

from pendle_pt_loop.loaders.funding_hedge import (
    HOURS_PER_YEAR,
    SECONDS_PER_YEAR,
    FundingHedgeLoader,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

START: datetime = datetime(2025, 4, 1, tzinfo=timezone.utc)
END: datetime = datetime(2025, 4, 1, 5, tzinfo=timezone.utc)
TICKER: str = "ETH"


def _times(start: datetime, n: int) -> np.ndarray:
    """N hourly UTC timestamps starting at ``start`` as nanosecond datetime64.

    ``np.datetime64`` has no timezone slot, so we drop tzinfo before
    conversion — the upstream ``FundingHistory`` will re-attach UTC on
    construction via ``pd.to_datetime(..., utc=True)``.
    """
    naive = [(start + timedelta(hours=i)).replace(tzinfo=None) for i in range(n)]
    return np.array(naive, dtype="datetime64[ns]")


def _make_fake_inner(rates: List[float], times: np.ndarray) -> type:
    """Build a fake inner-loader class returning the supplied FundingHistory."""

    class _FakeInner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Mirror the real loader's no-op-friendly constructor.
            self._args = args
            self._kwargs = kwargs

        def run(self) -> None:
            return None

        def read(self, with_run: bool = False) -> FundingHistory:
            return FundingHistory(rates=np.array(rates), time=times)

    return _FakeInner


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Redirect Loader._base_path to a tmp dir so cache tests don't pollute cwd."""
    monkeypatch.setenv("DATA_PATH", str(tmp_path))
    yield


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------

def test_loader_instantiates_with_sensible_args() -> None:
    loader = FundingHedgeLoader(
        ticker=TICKER,
        start_time=START,
        end_time=END,
    )
    assert loader.ticker == TICKER
    assert loader.start_time == START
    assert loader.end_time == END
    # Cache key must include ticker and both timestamps.
    key = loader._cache_key()
    assert TICKER in key
    assert str(int(START.timestamp())) in key
    assert str(int(END.timestamp())) in key


def test_loader_rejects_reversed_window() -> None:
    with pytest.raises(ValueError, match="precedes"):
        FundingHedgeLoader(
            ticker=TICKER,
            start_time=END,
            end_time=START,
        )


def test_loader_rejects_empty_ticker() -> None:
    with pytest.raises(ValueError, match="ticker"):
        FundingHedgeLoader(
            ticker="",
            start_time=START,
            end_time=END,
        )


def test_constants_have_documented_values() -> None:
    # Lock the constants — strategy code multiplies hourly funding by these.
    assert HOURS_PER_YEAR == pytest.approx(365.25 * 24)
    assert SECONDS_PER_YEAR == pytest.approx(365.25 * 24 * 3600)
    assert HOURS_PER_YEAR * 3600 == pytest.approx(SECONDS_PER_YEAR)


# ----------------------------------------------------------------------
# extract / transform contract
# ----------------------------------------------------------------------

def test_transform_produces_both_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    rates = [0.0001, 0.0002, 0.00015]
    times = _times(START, n=3)
    monkeypatch.setattr(
        _hl, "HyperliquidFundingRatesLoader", _make_fake_inner(rates, times)
    )
    loader = FundingHedgeLoader(
        ticker=TICKER,
        start_time=START,
        end_time=START + timedelta(hours=3),
    )
    df = loader.read(with_run=True)
    assert list(df.columns) == [
        "funding_rate_hourly",
        "funding_rate_annualised",
    ]
    assert len(df) == 3
    assert df.dtypes.apply(lambda d: d.kind == "f").all()


def test_annualisation_factor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hourly 0.0001 → annualised ≈ 0.0001 × 8766 ≈ 0.8766."""
    rates = [0.0001, 0.0002, -0.00005]
    times = _times(START, n=3)
    monkeypatch.setattr(
        _hl, "HyperliquidFundingRatesLoader", _make_fake_inner(rates, times)
    )
    loader = FundingHedgeLoader(
        ticker=TICKER,
        start_time=START,
        end_time=START + timedelta(hours=3),
    )
    df = loader.read(with_run=True)
    # Per-element check at machine precision.
    for hourly, annual in zip(df["funding_rate_hourly"], df["funding_rate_annualised"]):
        assert annual == pytest.approx(hourly * HOURS_PER_YEAR, rel=1e-12)
    # Headline magnitude check — 1 bp/hour ≈ 87.66% APY.
    assert df["funding_rate_annualised"].iloc[0] == pytest.approx(
        0.0001 * HOURS_PER_YEAR, rel=1e-12
    )
    assert 0.85 < df["funding_rate_annualised"].iloc[0] < 0.90


def test_utc_datetime_index(monkeypatch: pytest.MonkeyPatch) -> None:
    rates = [0.0001, 0.0002]
    times = _times(START, n=2)
    monkeypatch.setattr(
        _hl, "HyperliquidFundingRatesLoader", _make_fake_inner(rates, times)
    )
    loader = FundingHedgeLoader(
        ticker=TICKER,
        start_time=START,
        end_time=START + timedelta(hours=2),
    )
    df = loader.read(with_run=True)
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None
    assert str(df.index.tz) in {"UTC", "utc", "tzutc()"}
    assert df.index.name == "time"


def test_empty_input_gives_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _hl,
        "HyperliquidFundingRatesLoader",
        _make_fake_inner(rates=[], times=np.array([], dtype="datetime64[ns]")),
    )
    loader = FundingHedgeLoader(
        ticker=TICKER,
        start_time=START,
        end_time=END,
    )
    df = loader.read(with_run=True)
    assert df.empty
    assert list(df.columns) == [
        "funding_rate_hourly",
        "funding_rate_annualised",
    ]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None


# ----------------------------------------------------------------------
# Cache round trip
# ----------------------------------------------------------------------

def test_read_with_cache_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """First ``read(with_run=True)`` writes CSV; second ``read()`` reads it."""
    rates = [0.0001, 0.0002, 0.00015, -0.0001]
    times = _times(START, n=4)

    instantiations = {"n": 0}

    fake_cls = _make_fake_inner(rates, times)

    class _Counted(fake_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            instantiations["n"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(_hl, "HyperliquidFundingRatesLoader", _Counted)

    loader_a = FundingHedgeLoader(
        ticker=TICKER,
        start_time=START,
        end_time=START + timedelta(hours=4),
        loader_type=LoaderType.CSV,
    )
    df_first = loader_a.read(with_run=True).copy()
    assert instantiations["n"] == 1
    assert len(df_first) == 4

    # Fresh loader instance hits cache only — must NOT instantiate inner again.
    loader_b = FundingHedgeLoader(
        ticker=TICKER,
        start_time=START,
        end_time=START + timedelta(hours=4),
        loader_type=LoaderType.CSV,
    )
    df_cached = loader_b.read(with_run=False)
    assert instantiations["n"] == 1  # unchanged
    assert list(df_cached.columns) == [
        "funding_rate_hourly",
        "funding_rate_annualised",
    ]
    assert isinstance(df_cached.index, pd.DatetimeIndex)
    assert df_cached.index.tz is not None
    # Values match the freshly-loaded frame.
    pd.testing.assert_frame_equal(
        df_cached.reset_index(drop=True).astype(float),
        df_first.reset_index(drop=True).astype(float),
        check_exact=False,
    )


# ----------------------------------------------------------------------
# Live integration (opt-in)
# ----------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("FUNDING_INTEGRATION"),
    reason="set FUNDING_INTEGRATION=1 to hit the live Hyperliquid info API",
)
def test_integration_hyperliquid_api() -> None:
    """End-to-end live fetch — only runs when explicitly opted into."""
    end = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=24)
    loader = FundingHedgeLoader(
        ticker="ETH",
        start_time=start,
        end_time=end,
    )
    df = loader.read(with_run=True)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == [
        "funding_rate_hourly",
        "funding_rate_annualised",
    ]
    if not df.empty:
        # Hyperliquid funding rates are bounded by ±4% per hour in
        # practice (their cap is much higher but never approached).
        assert (df["funding_rate_hourly"].abs() < 0.04).all()
        # Round-trip the annualisation identity.
        ratio = df["funding_rate_annualised"] / df["funding_rate_hourly"]
        np.testing.assert_allclose(ratio.dropna().to_numpy(), HOURS_PER_YEAR)
