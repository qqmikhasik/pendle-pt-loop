"""Historical PT market loader (Pendle GraphQL API).

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
Primary: **Pendle GraphQL API** (keyless, public).

  ``POST https://api-v2.pendle.finance/core/graphql``

We use the ``marketSnapshots(chainId, address, timeFrame, ...)`` query,
which the Pendle team exposes for charting. The fields we read are:

* ``timestamp``      → row index (UTC).
* ``ptPrice``        → ``pt_price``.
* ``impliedApy``     → ``implied_yield``.
* ``liquidity``      → ``pool_liquidity``.

``seconds_to_expiry`` is always computed locally as
``max(expiry_timestamp - row_timestamp, 0)``.

Notes
-----
* The Pendle GraphQL endpoint occasionally renames fields (``ptDiscount``
  vs ``ptPrice``, ``impliedYield`` vs ``impliedApy``). The transform layer
  tolerates either spelling so a minor upstream rename does not break the
  loader; callers should regenerate the cache after such a rename.
* If the API ever omits ``impliedApy`` for a given snapshot, we fall back
  to the continuous-compounding identity
  :math:`y = -\\ln(\\text{pt\\_price}) / \\tau` where ``tau`` is years
  remaining to expiry.
* Live integration is exercised in tests by setting the
  ``PENDLE_INTEGRATION=1`` environment variable; all other tests mock
  ``requests.post``.

The cache lives under ``<DATA_PATH or cwd>/fractal_data/pendlemarketloader/``
as a CSV, keyed by ``<market_address>-<start_date>-<end_date>``.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from fractal.loaders.base_loader import Loader, LoaderType

PENDLE_GRAPHQL_URL = "https://api-v2.pendle.finance/core/graphql"

# One year, in seconds. Used to convert seconds-to-expiry → years-to-expiry
# for continuous-compounding implied-yield fallbacks.
_SECONDS_PER_YEAR: float = 365.0 * 24.0 * 3600.0

# Default GraphQL request timeout (seconds). The Pendle endpoint usually
# returns within a couple seconds; a 30s cap keeps backtests responsive
# while allowing a slow round-trip without crashing.
_REQUEST_TIMEOUT_S: float = 30.0

# Output columns, in canonical order. PendlePTEntity reads these.
_OUTPUT_COLUMNS: tuple[str, ...] = (
    "pt_price",
    "implied_yield",
    "seconds_to_expiry",
    "pool_liquidity",
)

# Default GraphQL query template. ``%s`` placeholders are interpolated by
# ``PendleMarketLoader.extract``. We request the ``HOUR`` timeframe so the
# resulting DataFrame is already at the cadence the backtest expects.
_SNAPSHOTS_QUERY: str = """
query MarketSnapshots($marketId: String!, $timeFrame: TimeFrame!) {
  marketSnapshots(marketId: $marketId, timeFrame: $timeFrame) {
    results {
      timestamp
      ptPrice
      impliedApy
      liquidity
    }
  }
}
""".strip()


def _to_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a UTC-aware datetime; assume naive datetimes are UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pick_field(row: dict[str, Any], *names: str) -> Any:
    """Return the first present non-null value among ``names`` in ``row``."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _parse_timestamp(raw: Any) -> pd.Timestamp:
    """Parse an integer-epoch or ISO-8601 timestamp to a UTC ``pd.Timestamp``."""
    if isinstance(raw, (int, float)):
        return pd.Timestamp(int(raw), unit="s", tz="UTC")
    return pd.Timestamp(raw, tz="UTC") if not isinstance(raw, pd.Timestamp) else raw


def _derive_implied_yield(pt_price: float, seconds_to_expiry: float) -> float:
    """Continuous-compounding implied yield from ``pt_price`` and ``tau``.

    Identity used by ``compute_pt_price`` in ``entities.pendle_pt`` in
    exponential mode: ``pt_price = exp(-y * tau)`` ⇒ ``y = -ln(pt) / tau``.

    Returns 0.0 if either input is non-positive or pt_price is degenerate
    (avoids ``log(0)`` and division-by-zero at/after expiry).
    """
    if seconds_to_expiry <= 0.0 or pt_price <= 0.0 or pt_price >= 1.0:
        return 0.0
    tau_years = seconds_to_expiry / _SECONDS_PER_YEAR
    return -math.log(pt_price) / tau_years


class PendleMarketLoader(Loader):
    """Hourly PT market history for a single Pendle market.

    Pipeline: ``extract`` posts a GraphQL query to the public Pendle API
    and stashes the raw payload in ``self._raw``; ``transform`` parses it
    into a UTC-indexed DataFrame with the four columns the backtest
    consumes; ``read(with_run=True)`` runs the whole pipeline and writes
    a CSV cache; ``read()`` reads that cache back.
    """

    def __init__(
        self,
        market_address: str,
        expiry_timestamp: int,
        start_time: datetime,
        end_time: datetime,
        *,
        api_key: str | None = None,
        loader_type: LoaderType = LoaderType.CSV,
    ) -> None:
        super().__init__(loader_type=loader_type)
        self.market_address: str = market_address.lower()
        self.expiry_timestamp: int = int(expiry_timestamp)
        self.start_time: datetime = _to_utc(start_time)
        self.end_time: datetime = _to_utc(end_time)
        self._api_key: str | None = api_key
        # ``extract`` populates this with the raw payload (list of snapshots)
        # so ``transform`` can be unit-tested independently if needed.
        self._raw: list[dict[str, Any]] = []

    def _cache_key(self) -> str:
        """``<market_address>-<YYYYMMDD>-<YYYYMMDD>`` — stable across runs."""
        s = self.start_time.strftime("%Y%m%d")
        e = self.end_time.strftime("%Y%m%d")
        return f"{self.market_address}-{s}-{e}"

    def _headers(self) -> dict[str, str]:
        """Pendle GraphQL is keyless, but tolerate an optional ``api_key``."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def extract(self) -> None:
        """POST the snapshots query and stash the result list in ``_raw``."""
        body = {
            "query": _SNAPSHOTS_QUERY,
            "variables": {
                "marketId": self.market_address,
                "timeFrame": "HOUR",
            },
        }
        response = requests.post(
            PENDLE_GRAPHQL_URL,
            json=body,
            headers=self._headers(),
            timeout=_REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json() if callable(getattr(response, "json", None)) else {}
        self._raw = _extract_snapshot_rows(payload)

    def transform(self) -> None:
        """Convert ``self._raw`` into the canonical hourly DataFrame."""
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
    return pd.DataFrame({c: pd.Series(dtype=float) for c in _OUTPUT_COLUMNS}, index=idx)


def _extract_snapshot_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the snapshot list out of a Pendle GraphQL response payload."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    snapshots = data.get("marketSnapshots") or {}
    results = snapshots.get("results") if isinstance(snapshots, dict) else snapshots
    if not isinstance(results, list):
        return []
    return results


def _build_frame(rows: list[dict[str, Any]], expiry_ts: int) -> pd.DataFrame:
    """Build a UTC-indexed DataFrame with the four canonical columns."""
    parsed: list[dict[str, Any]] = []
    for row in rows:
        ts_raw = _pick_field(row, "timestamp", "time", "hourStartUnix")
        pt_price = _pick_field(row, "ptPrice", "ptDiscount", "price")
        if ts_raw is None or pt_price is None:
            continue
        ts = _parse_timestamp(ts_raw)
        epoch = int(ts.timestamp())
        seconds_to_expiry = float(max(expiry_ts - epoch, 0))
        pt_price_f = float(pt_price)
        implied_raw = _pick_field(row, "impliedApy", "impliedYield")
        implied = (
            float(implied_raw)
            if implied_raw is not None
            else _derive_implied_yield(pt_price_f, seconds_to_expiry)
        )
        liquidity = _pick_field(row, "liquidity", "totalLiquidity", "pool_liquidity")
        parsed.append(
            {
                "timestamp": ts,
                "pt_price": pt_price_f,
                "implied_yield": implied,
                "seconds_to_expiry": seconds_to_expiry,
                "pool_liquidity": float(liquidity) if liquidity is not None else 0.0,
            }
        )
    if not parsed:
        return _empty_frame()
    df = pd.DataFrame(parsed).set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="first")]
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
    # Coerce numeric columns back to float (CSV reads them as object if NaN).
    for col in _OUTPUT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[list(_OUTPUT_COLUMNS)]
