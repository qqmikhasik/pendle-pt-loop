"""Historical PT market loader (Pendle public REST API).

Goal
----
Produce a UTC-indexed hourly :class:`pandas.DataFrame` with the four columns
consumed by :class:`pendle_pt_loop.entities.PendlePTEntity` through its
:class:`PendlePTGlobalState`:

* ``pt_price``         — market price of 1 PT in USDC, :math:`\\in (0, 1]`.
* ``implied_yield``    — annualised fixed yield baked into ``pt_price``
  at observation time (decimal, 0.14 = 14% APY).
* ``seconds_to_expiry``— wall-clock seconds remaining until PT redeem
  unlocks; zero at or after expiry.
* ``pool_liquidity``   — total Pendle pool liquidity in USDC equivalent.

Data source
-----------
**Pendle public REST API**, keyless:

  ``GET https://api-v2.pendle.finance/core/v1/{chain_id}/markets/{market}/historical-data``
  ``?time_frame=hour&from=<ISO>&to=<ISO>``

The response is a dict with parallel arrays:

* ``timestamp[]``     → row index (Unix epoch seconds, UTC).
* ``impliedApy[]``    → ``implied_yield``.
* ``tvl[]``           → ``pool_liquidity``.
* ``baseApy[]``, ``underlyingApy[]``, ``maxApy[]`` — informational.

The endpoint does NOT expose a direct ``ptPrice`` field, so we compute it
from ``implied_yield`` + ``seconds_to_expiry`` using
:func:`pendle_pt_loop.entities.pendle_pt.compute_pt_price` with the
``"linear"`` mode (matches Pendle Oracle's own convention, which is
what Morpho's oracle reads — keeping pricing consistent across the two
sides of the loop).

``seconds_to_expiry`` is always computed locally as
``max(expiry_timestamp - row_timestamp, 0)``.

Notes
-----
* The Pendle endpoint caps returned rows (~60-day windows at hourly
  granularity). For longer windows the loader would need pagination
  via successive ``from`` slices; for our PT-sUSDE-27NOV2025 backtest
  the active-market data covers roughly Sep 28 → Nov 27 (≈60 days),
  which fits in one call. If we ever need longer history, extend
  ``extract`` to paginate.
* Live integration is exercised in tests by setting
  ``PENDLE_INTEGRATION=1``; all other tests mock ``requests.get``.

The cache lives under ``<DATA_PATH or cwd>/fractal_data/pendlemarketloader/``
as a CSV, keyed by ``<chain>-<market_address>-<start_date>-<end_date>``.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from fractal.loaders.base_loader import Loader, LoaderType

from pendle_pt_loop.entities.pendle_pt import compute_pt_price

PENDLE_REST_BASE: str = "https://api-v2.pendle.finance/core/v1"

# One year (Julian, matching SECONDS_PER_YEAR in the entity) for the
# implied-yield fallback identity.
_SECONDS_PER_YEAR: float = 365.25 * 24.0 * 3600.0

# REST timeout. Pendle endpoints usually return < 2 s.
_REQUEST_TIMEOUT_S: float = 30.0

# Output columns, in canonical order. PendlePTEntity reads these.
_OUTPUT_COLUMNS: tuple[str, ...] = (
    "pt_price",
    "implied_yield",
    "seconds_to_expiry",
    "pool_liquidity",
)


def _to_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a UTC-aware datetime; assume naive datetimes are UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _derive_implied_yield(pt_price: float, seconds_to_expiry: float) -> float:
    """Continuous-compounding implied yield from ``pt_price`` and ``tau``.

    Inverse of ``compute_pt_price`` in exponential mode. Returns 0.0 at
    expiry or for degenerate inputs.
    """
    if seconds_to_expiry <= 0.0 or pt_price <= 0.0 or pt_price >= 1.0:
        return 0.0
    tau_years = seconds_to_expiry / _SECONDS_PER_YEAR
    return -math.log(pt_price) / tau_years


class PendleMarketLoader(Loader):
    """Hourly PT market history for a single Pendle market.

    Pipeline: ``extract`` issues a single REST GET to the Pendle
    ``historical-data`` endpoint and stashes the JSON in ``self._raw``;
    ``transform`` parses the parallel-array payload into a UTC-indexed
    DataFrame with the four canonical columns; ``read(with_run=True)``
    runs the whole pipeline and writes a CSV cache; ``read()`` reads
    that cache back.
    """

    def __init__(
        self,
        market_address: str,
        expiry_timestamp: int,
        start_time: datetime,
        end_time: datetime,
        *,
        chain_id: int = 1,
        api_key: str | None = None,
        loader_type: LoaderType = LoaderType.CSV,
    ) -> None:
        super().__init__(loader_type=loader_type)
        self.market_address: str = market_address.lower()
        self.expiry_timestamp: int = int(expiry_timestamp)
        self.start_time: datetime = _to_utc(start_time)
        self.end_time: datetime = _to_utc(end_time)
        self.chain_id: int = int(chain_id)
        self._api_key: str | None = api_key
        # ``extract`` populates this with the raw payload dict so
        # ``transform`` can be unit-tested independently if needed.
        self._raw: dict[str, Any] = {}

    def _cache_key(self) -> str:
        """``<chain>-<market_address>-<YYYYMMDD>-<YYYYMMDD>`` — stable."""
        s = self.start_time.strftime("%Y%m%d")
        e = self.end_time.strftime("%Y%m%d")
        return f"{self.chain_id}-{self.market_address}-{s}-{e}"

    def _url(self) -> str:
        return (
            f"{PENDLE_REST_BASE}/{self.chain_id}/markets/"
            f"{self.market_address}/historical-data"
        )

    def _params(self) -> dict[str, str]:
        return {
            "time_frame": "hour",
            "from": self.start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": self.end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def _headers(self) -> dict[str, str]:
        """Pendle REST is keyless; tolerate an optional ``api_key``."""
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def extract(self) -> None:
        """GET the historical-data endpoint and stash the JSON in ``_raw``."""
        response = requests.get(
            self._url(),
            params=self._params(),
            headers=self._headers(),
            timeout=_REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json() if callable(getattr(response, "json", None)) else {}
        self._raw = payload if isinstance(payload, dict) else {}

    def transform(self) -> None:
        """Convert ``self._raw`` (parallel arrays) into the canonical DataFrame."""
        if not self._raw:
            self._data = _empty_frame()
            return
        df = _build_frame(self._raw, self.expiry_timestamp)
        df = _clip_window(df, self.start_time, self.end_time)
        self._data = df

    def read(self, with_run: bool = False) -> pd.DataFrame:
        """Return the cached DataFrame; rebuild from API if ``with_run``."""
        if with_run:
            self.run()
        else:
            self._read(self._cache_key())
            # CSV round-trip dropped the index name and tz — restore both.
            self._data = _restore_index(self._data)
        return self._data


# ---------------------------------------------------------------- helpers


def _empty_frame() -> pd.DataFrame:
    """Empty DataFrame with the canonical column set and a UTC datetime index."""
    idx = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return pd.DataFrame(
        {c: pd.Series(dtype=float) for c in _OUTPUT_COLUMNS}, index=idx
    )


def _build_frame(payload: dict[str, Any], expiry_ts: int) -> pd.DataFrame:
    """Build a UTC-indexed DataFrame from Pendle's parallel-array response.

    Expected payload keys:
        ``timestamp`` (list[int]), ``impliedApy`` (list[str|float]),
        ``tvl`` (list[str|float]).
    """
    timestamps = payload.get("timestamp") or []
    implied = payload.get("impliedApy") or []
    tvl = payload.get("tvl") or []
    if not timestamps:
        return _empty_frame()

    n = min(len(timestamps), len(implied), len(tvl))
    if n == 0:
        return _empty_frame()

    parsed: list[dict[str, Any]] = []
    for i in range(n):
        try:
            epoch = int(timestamps[i])
            implied_y = float(implied[i])
            liquidity = float(tvl[i])
        except (TypeError, ValueError):
            continue
        seconds_to_expiry = float(max(expiry_ts - epoch, 0))
        # Linear pricing matches Pendle Oracle convention (what Morpho reads).
        pt_price = compute_pt_price(
            implied_yield=implied_y,
            seconds_to_expiry=seconds_to_expiry,
            mode="linear",
        )
        parsed.append(
            {
                "timestamp": pd.Timestamp(epoch, unit="s", tz="UTC"),
                "pt_price": pt_price,
                "implied_yield": implied_y,
                "seconds_to_expiry": seconds_to_expiry,
                "pool_liquidity": liquidity,
            }
        )
    if not parsed:
        return _empty_frame()
    df = pd.DataFrame(parsed).set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df.index.name = "timestamp"
    return df[list(_OUTPUT_COLUMNS)]


def _clip_window(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    """Restrict ``df`` to ``[start, end]`` inclusive on its UTC index."""
    if df.empty:
        return df
    mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
    return df.loc[mask]


def _restore_index(df: pd.DataFrame) -> pd.DataFrame:
    """Re-establish UTC ``timestamp`` index after a CSV round-trip."""
    if df is None:
        return _empty_frame()
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "timestamp"
    for col in _OUTPUT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[list(_OUTPUT_COLUMNS)]


# Backwards-compatible alias for tests that imported the GraphQL URL.
PENDLE_GRAPHQL_URL: str = PENDLE_REST_BASE
