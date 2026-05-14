"""Pendle Boros market loader.

Boros is Pendle's funding-rate tokenisation layer launched August 2025.
A Boros market represents a forward on the *average funding rate* of a
specific perpetual futures market (e.g. Hyperliquid ETH-USDC) up to a
fixed maturity. Going long a Boros market = receiving funding payments
between now and maturity; going short = paying.

API
---
Public, keyless:

* List markets: ``GET https://api.boros.finance/core/v1/markets``
  Returns a list of objects with ``marketId``, ``imData.symbol``,
  ``imData.maturity``, ``metadata.fundingRateSymbol``,
  ``data.markApr`` (current annualised mark rate), and so on.

* Historical bars: ``GET https://api.boros.finance/core/v1/markets/chart``
  ``?marketId=<N>&timeFrame=<5m|1h|1d|1w>``. The endpoint **ignores
  ``from``/``to`` parameters and always returns the most recent ~500
  bars** (verified empirically May 2026). For longer history we would
  need to scrape on-chain via ``eth_getLogs`` on the AMM contract —
  out of scope for the current research project.

Output
------
DataFrame indexed by UTC datetime, columns:

* ``mark_apr``         — close mark rate (annualised, decimal).
* ``observed_funding`` — close observed funding rate (annualised).
* ``mark_apr_7d_ma``   — 7-day moving average of observed funding.
* ``mark_apr_30d_ma``  — 30-day moving average of observed funding.

The ``mark_apr`` column is what a long-Boros holder receives in funding
per unit of time. It is the analogue of
``funding_rate_annualised`` in the ``FundingHedgeLoader`` (Hyperliquid
proxy) and can be swapped in directly in any strategy that consumes
funding-rate observations.

Recent-only coverage is a known limitation. We use the Hyperliquid
funding-rate loader as a long-history proxy and validate empirically
on the overlap window that the two series track each other closely.
See ``scripts/validate_boros_proxy.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from fractal.loaders.base_loader import Loader, LoaderType

BOROS_API_BASE: str = "https://api.boros.finance/core/v1"

_REQUEST_TIMEOUT_S: float = 30.0
_OUTPUT_COLUMNS: tuple[str, ...] = (
    "mark_apr",
    "observed_funding",
    "mark_apr_7d_ma",
    "mark_apr_30d_ma",
)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class BorosMarketLoader(Loader):
    """Recent funding-rate bars for one Boros market.

    Pipeline: ``extract`` issues a single REST GET to the Boros chart
    endpoint and stashes the JSON in ``self._raw``; ``transform`` parses
    the bar list into a UTC-indexed DataFrame.
    """

    def __init__(
        self,
        market_id: int,
        start_time: datetime,
        end_time: datetime,
        *,
        time_frame: str = "1h",
        api_key: str | None = None,
        loader_type: LoaderType = LoaderType.CSV,
    ) -> None:
        super().__init__(loader_type=loader_type)
        self.market_id: int = int(market_id)
        self.start_time: datetime = _to_utc(start_time)
        self.end_time: datetime = _to_utc(end_time)
        self.time_frame: str = time_frame
        self._api_key: str | None = api_key
        self._raw: dict[str, Any] = {}

    def _cache_key(self) -> str:
        s = self.start_time.strftime("%Y%m%d")
        e = self.end_time.strftime("%Y%m%d")
        return f"market{self.market_id}-{self.time_frame}-{s}-{e}"

    def _url(self) -> str:
        return f"{BOROS_API_BASE}/markets/chart"

    def _params(self) -> dict[str, Any]:
        return {"marketId": self.market_id, "timeFrame": self.time_frame}

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def extract(self) -> None:
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
        if not self._raw:
            self._data = _empty_frame()
            return
        df = _build_frame(self._raw)
        df = _clip_window(df, self.start_time, self.end_time)
        self._data = df

    def read(self, with_run: bool = False) -> pd.DataFrame:
        if with_run:
            self.run()
        else:
            self._read(self._cache_key())
            self._data = _restore_index(self._data)
        return self._data


# ---------------------------------------------------------------- helpers


def _empty_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return pd.DataFrame(
        {c: pd.Series(dtype=float) for c in _OUTPUT_COLUMNS}, index=idx
    )


def _build_frame(payload: dict[str, Any]) -> pd.DataFrame:
    """Parse Boros chart payload — list of bars under ``results``."""
    results = payload.get("results") or []
    if not isinstance(results, list) or not results:
        return _empty_frame()

    parsed: list[dict[str, Any]] = []
    for row in results:
        try:
            epoch = int(row["ts"])
            mark_apr = float(row.get("c", row.get("mr", 0.0)))
            observed = float(row.get("u", row.get("ofr", 0.0)))
            ma7 = float(row.get("b7dmafr", 0.0))
            ma30 = float(row.get("b30dmafr", 0.0))
        except (TypeError, ValueError, KeyError):
            continue
        parsed.append(
            {
                "timestamp": pd.Timestamp(epoch, unit="s", tz="UTC"),
                "mark_apr": mark_apr,
                "observed_funding": observed,
                "mark_apr_7d_ma": ma7,
                "mark_apr_30d_ma": ma30,
            }
        )
    if not parsed:
        return _empty_frame()
    df = pd.DataFrame(parsed).set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df.index.name = "timestamp"
    return df[list(_OUTPUT_COLUMNS)]


def _clip_window(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
    return df.loc[mask]


def _restore_index(df: pd.DataFrame) -> pd.DataFrame:
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


__all__ = ["BorosMarketLoader", "BOROS_API_BASE"]
