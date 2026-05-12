"""Unit tests for :mod:`pendle_pt_loop.loaders.susde_price`.

The loader composes :class:`fractal.loaders.binance.binance_prices.BinanceSpotPriceLoader`
internally, which routes through ``fractal.loaders._http.HttpClient`` (a
``requests.Session`` wrapper). Patching at ``requests.get`` would miss that
session, so we patch the seam the inner loader actually calls:
``fractal.loaders.binance.binance_client.BinanceHttp.get``. The
``_direct_rest_call`` helper is patched at ``requests.get`` as specified.

Live HTTP is gated behind ``SUSDE_INTEGRATION`` so unit runs stay offline.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import pandas as pd
import pytest
import requests

from fractal.loaders.base_loader import LoaderType
from fractal.loaders.binance import binance_client as _bc
from pendle_pt_loop.loaders import susde_price as mod
from pendle_pt_loop.loaders.susde_price import (
    BINANCE_REST_URL,
    PRIMARY_SYMBOL,
    SANITY_LOWER,
    SANITY_UPPER,
    SUSDePriceLoader,
    _direct_rest_call,
    _parse_klines_payload,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

START: datetime = datetime(2025, 4, 1, tzinfo=timezone.utc)
END: datetime = datetime(2025, 4, 1, 5, tzinfo=timezone.utc)


def _kline_row(open_ms: int, close: float) -> List[Any]:
    """Build one Binance klines row. Schema: [openTime, o, h, l, c, v, ...]."""
    return [
        open_ms,
        f"{close:.8f}",
        f"{close + 0.0001:.8f}",
        f"{close - 0.0001:.8f}",
        f"{close:.8f}",
        "1000.00000000",
        open_ms + 3_599_999,
        "1000.0",
        10,
        "500.0",
        "500.0",
        "0",
    ]


def _sample_klines() -> List[List[Any]]:
    """Five hours of mocked sUSDe-ish prices near 1.0 USDC."""
    hour_ms = 3_600_000
    base_ms = int(START.timestamp() * 1000)
    closes = [0.9998, 1.0001, 1.0000, 0.9999, 1.0002]
    return [_kline_row(base_ms + i * hour_ms, c) for i, c in enumerate(closes)]


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``DATA_PATH`` so cache writes land in a temp dir."""
    monkeypatch.setenv("DATA_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture
def patched_binance(monkeypatch: pytest.MonkeyPatch) -> List[List[Any]]:
    """Patch :class:`BinanceHttp.get` to return :func:`_sample_klines`."""
    payload = _sample_klines()

    def fake_get(self, section: str, path: str, params: Any = None) -> Any:
        # Defensive: confirm we are hitting the spot klines path. Tests
        # would still pass otherwise, but this catches accidental routing
        # changes upstream in fractal-defi.
        assert path == "/api/v3/klines"
        assert section == "spot"
        return payload

    monkeypatch.setattr(_bc.BinanceHttp, "get", fake_get)
    return payload


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------

def test_loader_instantiates() -> None:
    loader = SUSDePriceLoader(start_time=START, end_time=END)
    assert loader.source == "binance"
    assert loader.loader_type == LoaderType.CSV
    # Naive-datetime input gets coerced to UTC-aware (consistency with fractal _dt).
    naive = SUSDePriceLoader(
        start_time=datetime(2025, 4, 1),
        end_time=datetime(2025, 4, 2),
    )
    assert naive.start_time.tzinfo is not None
    assert naive.end_time.tzinfo is not None


def test_loader_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unknown source"):
        SUSDePriceLoader(  # type: ignore[arg-type]
            start_time=START,
            end_time=END,
            source="coingecko",
        )


def test_cache_key_includes_source_and_window() -> None:
    loader = SUSDePriceLoader(start_time=START, end_time=END)
    key = loader._cache_key()
    assert "binance" in key
    assert "USDEUSDT" in key
    assert str(int(START.timestamp() * 1000)) in key
    assert str(int(END.timestamp() * 1000)) in key
    # The "fallback" source must produce a distinct cache filename so the
    # two source modes can coexist on disk.
    other = SUSDePriceLoader(start_time=START, end_time=END, source="fallback")
    assert other._cache_key() != key


def test_module_constants() -> None:
    assert BINANCE_REST_URL == "https://api.binance.com/api/v3/klines"
    assert PRIMARY_SYMBOL == "USDEUSDT"


# ----------------------------------------------------------------------
# extract + transform
# ----------------------------------------------------------------------

def test_extract_then_transform_returns_price_column(
    patched_binance: List[List[Any]],
) -> None:
    loader = SUSDePriceLoader(start_time=START, end_time=END)
    loader.extract()
    loader.transform()
    df = loader._data
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["price"]
    assert len(df) == len(patched_binance)
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None  # UTC-aware


def test_price_close_to_one(patched_binance: List[List[Any]]) -> None:
    """Mocked sUSDe-style data must land inside the depeg sanity band."""
    loader = SUSDePriceLoader(start_time=START, end_time=END)
    loader.extract()
    loader.transform()
    prices = loader._data["price"]
    assert prices.between(SANITY_LOWER, SANITY_UPPER).all()
    # Tighter sanity for the mocked sample specifically.
    assert prices.between(0.99, 1.01).all()


def test_handles_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty Binance response -> zero-row DataFrame of the correct shape."""

    def fake_get(self, section: str, path: str, params: Any = None) -> Any:
        return []

    monkeypatch.setattr(_bc.BinanceHttp, "get", fake_get)
    loader = SUSDePriceLoader(start_time=START, end_time=END)
    loader.extract()
    loader.transform()
    df = loader._data
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["price"]
    assert len(df) == 0
    assert isinstance(df.index, pd.DatetimeIndex)


# ----------------------------------------------------------------------
# read() round-trip with on-disk cache
# ----------------------------------------------------------------------

def test_read_with_cache_round_trip(
    isolated_cache: Path,
    patched_binance: List[List[Any]],
) -> None:
    """read(with_run=True) writes cache; read() reads it back identical."""
    loader = SUSDePriceLoader(start_time=START, end_time=END)
    df_run = loader.read(with_run=True)
    assert list(df_run.columns) == ["price"]
    assert len(df_run) == len(patched_binance)

    # Second loader instance: same params, no HTTP. Must hydrate from cache.
    loader2 = SUSDePriceLoader(start_time=START, end_time=END)
    df_cached = loader2.read(with_run=False)
    assert list(df_cached.columns) == ["price"]
    assert len(df_cached) == len(df_run)
    pd.testing.assert_series_equal(
        df_cached["price"].reset_index(drop=True),
        df_run["price"].reset_index(drop=True),
        check_names=False,
    )
    # Index round-tripped as UTC-aware DatetimeIndex.
    assert isinstance(df_cached.index, pd.DatetimeIndex)
    assert df_cached.index.tz is not None


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------

def test_parse_klines_payload() -> None:
    rows = _sample_klines()
    parsed = _parse_klines_payload(rows)
    assert len(parsed["openTime_ms"]) == len(rows)
    assert len(parsed["close"]) == len(rows)
    # First close in the mocked payload is 0.9998 (see _sample_klines).
    assert parsed["close"][0] == pytest.approx(0.9998)


def test_direct_rest_call_uses_module_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: ``BINANCE_REST_URL`` is the seam patched via ``requests.get``.

    Mirrors the design-doc instruction to mock with
    ``monkeypatch.setattr(requests, "get", ...)``.
    """
    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> List[List[Any]]:
            return _sample_klines()

    def fake_get(url: str, params: Any = None, timeout: float = 15.0) -> _Resp:
        captured["url"] = url
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    rows = _direct_rest_call(START, END)
    assert captured["url"] == BINANCE_REST_URL
    assert captured["params"]["symbol"] == PRIMARY_SYMBOL
    assert captured["params"]["interval"] == "1h"
    assert len(rows) == 5


# ----------------------------------------------------------------------
# Live integration (gated)
# ----------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("SUSDE_INTEGRATION"),
    reason="Set SUSDE_INTEGRATION=1 to hit Binance live.",
)
def test_integration_susde_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_PATH", str(tmp_path))
    # Use a small two-hour window so the call is cheap.
    end = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end.replace(hour=max(end.hour - 2, 0))
    loader = SUSDePriceLoader(start_time=start, end_time=end)
    df = loader.read(with_run=True)
    assert list(df.columns) == ["price"]
    if not df.empty:
        assert df["price"].between(SANITY_LOWER, SANITY_UPPER).all()
