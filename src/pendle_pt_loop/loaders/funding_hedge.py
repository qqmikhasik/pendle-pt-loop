"""Hyperliquid perp funding-rate loader, annualised for the PT-loop hedge.

Why wrap rather than use the inner loader directly
--------------------------------------------------
:class:`fractal.loaders.hyperliquid.HyperliquidFundingRatesLoader` returns a
``FundingHistory(rates, time)`` — i.e. a one-column DataFrame named
``rate`` whose values are the **raw per-hour funding fraction quoted by
Hyperliquid** (e.g. ``0.0001`` means ``+1 bp`` of position notional paid
*every hour*). Hyperliquid documents funding as an hourly settlement,
not an annualised APY, so consumers that talk in "APY decimals" (the
rest of the PT-loop math) need to multiply by ``HOURS_PER_YEAR``.

Rather than scattering that multiplication across every downstream
consumer, this loader does it once and emits both columns side by side:

* ``funding_rate_hourly`` — the raw per-hour fraction (signed; positive
  = longs pay shorts, the funding flow a delta-neutral short captures).
* ``funding_rate_annualised`` — ``hourly × HOURS_PER_YEAR`` where
  ``HOURS_PER_YEAR = 365.25 × 24 ≈ 8766``. Linear (not compounded)
  annualisation matches what we do for the lending leg in
  ``MorphoMarketLoader`` (Morpho's ``borrowApy`` is itself a "linearised
  per-second × seconds-per-year" view at hourly granularity), so the
  spread between borrow APR and funding APR is apples-to-apples.

Output contract
---------------
:meth:`FundingHedgeLoader.read` returns a ``pandas.DataFrame`` with:

* Index: UTC-aware ``DatetimeIndex`` named ``time`` (sorted ascending).
* Columns: exactly ``["funding_rate_hourly", "funding_rate_annualised"]``,
  both ``float64``.

An empty time window yields a same-shape zero-row DataFrame.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

import fractal.loaders.hyperliquid as _hl
from fractal.loaders._dt import to_seconds, to_utc
from fractal.loaders.base_loader import Loader, LoaderType

# -----------------------------------------------------------------------
# Annualisation constants
# -----------------------------------------------------------------------
# 365.25 picks up the leap-year average; consistent with the rest of the
# project (see ``pendle.py``'s ``SECONDS_PER_YEAR``).
SECONDS_PER_YEAR: float = 365.25 * 24 * 3600
HOURS_PER_YEAR: float = 365.25 * 24

# Output column order is part of the public contract.
_OUTPUT_COLUMNS: tuple[str, ...] = (
    "funding_rate_hourly",
    "funding_rate_annualised",
)


class FundingHedgeLoader(Loader):
    """Hourly Hyperliquid funding rate for a single perp, annualised.

    Composes :class:`HyperliquidFundingRatesLoader` to fetch raw rows
    (which already paginate Hyperliquid's 500-row API cap), then
    transforms the resulting ``FundingHistory`` into the two-column
    output documented at module level. The inner loader is *not* asked
    to write its own cache — caching happens at this wrapper's level so
    one CSV per ``(ticker, start, end)`` triple is all the strategy
    code has to know about.

    Parameters
    ----------
    ticker:
        Hyperliquid perp symbol, e.g. ``"ETH"``. Case-sensitive per
        Hyperliquid (their API takes ``"ETH"``, not ``"eth"``).
    start_time, end_time:
        UTC window for the funding history. Naive datetimes are
        interpreted as UTC.
    loader_type:
        Cache backend. CSV by default — round-trips a DataFrame with a
        named ``DatetimeIndex`` correctly through :meth:`_load` /
        :meth:`_read`.
    """

    def __init__(
        self,
        ticker: str,
        start_time: datetime,
        end_time: datetime,
        *,
        loader_type: LoaderType = LoaderType.CSV,
    ) -> None:
        super().__init__(loader_type=loader_type)
        if not isinstance(ticker, str) or not ticker:
            raise ValueError(f"ticker must be a non-empty string, got {ticker!r}")
        start = to_utc(start_time)
        end = to_utc(end_time)
        if start is None or end is None:
            raise ValueError("start_time and end_time are required")
        if end < start:
            raise ValueError(f"end_time {end} precedes start_time {start}")
        self.ticker: str = ticker
        self.start_time: datetime = start
        self.end_time: datetime = end
        # Inner loader is built lazily so tests can monkeypatch the
        # ``HyperliquidFundingRatesLoader`` symbol on the module before
        # we instantiate it.
        self._raw: Any = None

    # ------------------------------------------------------------------
    # Cache identity
    # ------------------------------------------------------------------

    def _cache_key(self) -> str:
        return (
            f"{self.ticker}-{to_seconds(self.start_time)}-"
            f"{to_seconds(self.end_time)}"
        )

    # ------------------------------------------------------------------
    # extract / transform
    # ------------------------------------------------------------------

    def _build_inner(self) -> Any:
        """Construct the inner Hyperliquid funding loader.

        Resolves ``HyperliquidFundingRatesLoader`` through the module
        attribute (not a top-level import) so tests can monkeypatch the
        symbol on :mod:`fractal.loaders.hyperliquid` between sessions.
        """
        cls = _hl.HyperliquidFundingRatesLoader
        return cls(
            ticker=self.ticker,
            start_time=self.start_time,
            end_time=self.end_time,
            loader_type=self.loader_type,
        )

    def extract(self) -> None:
        """Fetch the ``FundingHistory`` from the inner Hyperliquid loader."""
        inner = self._build_inner()
        # ``read(with_run=True)`` drives extract→transform→load on the
        # inner loader and returns the typed ``FundingHistory`` struct.
        # We discard the inner cache file (it's keyed differently) and
        # rely on this wrapper's cache.
        self._raw = inner.read(with_run=True)

    def transform(self) -> None:
        """Convert ``FundingHistory`` to the two-column annualised frame."""
        raw = self._raw
        if raw is None or len(raw) == 0:
            self._data = self._empty_frame()
            return
        idx = pd.to_datetime(raw.index, utc=True)
        hourly = pd.Series(raw["rate"].astype(float).to_numpy(), index=idx)
        df = pd.DataFrame(
            {
                "funding_rate_hourly": hourly,
                "funding_rate_annualised": hourly * HOURS_PER_YEAR,
            }
        )
        df.index = pd.DatetimeIndex(df.index, name="time")
        self._data = df.sort_index()

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def read(self, with_run: bool = False) -> pd.DataFrame:
        """Return the annualised funding DataFrame; cache or pipeline."""
        if with_run:
            self.run()
        else:
            self._read(self._cache_key())
            self._restore_index()
        if self._data is None:
            return self._empty_frame()
        return self._data

    def _restore_index(self) -> None:
        """Re-attach a UTC DatetimeIndex after a CSV round-trip."""
        if self._data is None or self._data.empty:
            self._data = self._empty_frame()
            return
        df: pd.DataFrame = self._data
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df.set_index("time").sort_index()
        else:
            df.index = pd.to_datetime(df.index, utc=True)
        df.index = pd.DatetimeIndex(df.index, name="time")
        cols = [c for c in _OUTPUT_COLUMNS if c in df.columns]
        self._data = df[cols].astype(float)

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        idx = pd.DatetimeIndex([], tz="UTC", name="time")
        return pd.DataFrame(
            {c: pd.Series(dtype=float) for c in _OUTPUT_COLUMNS}, index=idx
        )
