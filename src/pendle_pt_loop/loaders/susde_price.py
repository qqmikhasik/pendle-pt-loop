"""Historical hourly sUSDe price loader (in USDC terms).

Purpose
-------
Two consumers in the PT-loop backtest:

1. **Depeg sanity tracking** — confirms that sUSDe trades close to 1 USDC
   over the backtest window. Material drift here invalidates the
   "stablecoin-on-stablecoin carry" framing of the strategy.
2. **Underlying conversion at expiry** — when the Pendle PT redeems for
   sUSDe, the strategy converts proceeds to USDC at the observed
   sUSDe -> USDC price.

Source choice
-------------
Binance does NOT list a direct ``SUSDEUSDT`` market — calling
``https://api.binance.com/api/v3/exchangeInfo?symbol=SUSDEUSDT`` returns
``{"code": -1121, "msg": "Invalid symbol."}`` on both spot and USDT-M
futures. The closest liquid proxy is ``USDEUSDT`` (Ethena's unstaked
USDe vs Tether) on Binance **spot**, which exists and is actively
trading.

We use ``USDEUSDT`` as the **primary** feed via
:class:`fractal.loaders.binance.binance_prices.BinanceSpotPriceLoader`
(which hits the same ``/api/v3/klines`` REST endpoint and inherits all
the pagination / caching / retry logic). Note this is fallback
strategy (b) from the design spec.

What this *misses* — sUSDe accrues staking yield against USDe via a
monotonically rising ``pricePerShare`` (the ERC-4626 share price of the
sUSDe vault). USDEUSDT only captures the **spot peg** of the unstaked
USDe stablecoin; it does not reflect the ~0–15% APY vault accumulation.
For the strategy's purposes that is acceptable: PT-loop redeems PT for
**sUSDe shares**, and the strategy converts share -> USDC using a
combined model of (USDe spot peg) * (sUSDe vault pricePerShare). The
pricePerShare component will be loaded separately on-chain (out of scope
for this loader). This loader provides only the spot-peg component.

USDT is treated as 1:1 with USDC over the backtest window (both are
USD-pegged stablecoins; their bilateral peg holds to within a few bps
historically).

Output schema
-------------
``read(with_run=...)`` returns a ``pandas.DataFrame`` with:

* index: UTC-aware ``DatetimeIndex`` (open time of each hourly bar)
* columns: exactly ``["price"]`` (close price of the hour, float)

Empty windows are valid and yield a zero-row DataFrame of the same
shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import pandas as pd
import requests

from fractal.loaders._dt import to_ms, to_utc
from fractal.loaders.base_loader import Loader, LoaderType
from fractal.loaders.binance.binance_prices import BinanceSpotPriceLoader

# Public REST endpoint used by the spot Binance loader. Exposed at module
# level both for documentation and so tests can patch it deterministically.
BINANCE_REST_URL: str = "https://api.binance.com/api/v3/klines"

# Primary proxy symbol — Ethena USDe vs Tether on Binance spot.
PRIMARY_SYMBOL: str = "USDEUSDT"

# Sanity bounds — sUSDe / USDe has not traded outside this band absent a
# major depeg event. Used by tests; loader does not enforce.
SANITY_LOWER: float = 0.95
SANITY_UPPER: float = 1.10

Source = Literal["binance", "fallback"]


class SUSDePriceLoader(Loader):
    """Hourly sUSDe -> USDC price loader.

    Internally composes :class:`BinanceSpotPriceLoader` on
    ``USDEUSDT`` (see module docstring for rationale). The wrapper
    exposes a single ``price`` column DataFrame and an independent cache
    key so this loader's on-disk artefact does not collide with a raw
    ``USDEUSDT`` price dump used elsewhere in the project.

    Parameters
    ----------
    start_time, end_time
        UTC window for the price history. Naive datetimes are
        interpreted as UTC.
    source
        Either ``"binance"`` (the only currently-wired source) or
        ``"fallback"`` (reserved for future on-chain pricePerShare
        feeds). Today both route to the same Binance spot endpoint;
        the field exists so the cache filename changes when a new
        source is introduced.
    loader_type
        Cache format. CSV by default — round-trips a DataFrame with a
        DatetimeIndex correctly via :meth:`_load` / :meth:`_read`.
    """

    def __init__(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        source: Source = "binance",
        loader_type: LoaderType = LoaderType.CSV,
    ) -> None:
        super().__init__(loader_type=loader_type)
        if source not in ("binance", "fallback"):
            raise ValueError(
                f"Unknown source {source!r}; expected 'binance' or 'fallback'."
            )
        self.source: Source = source
        self.start_time: datetime = to_utc(start_time)  # type: ignore[assignment]
        self.end_time: datetime = to_utc(end_time)  # type: ignore[assignment]

    # ------------------------------------------------------------------ helpers
    def _cache_key(self) -> str:
        """Filename stem encoding all params that affect the dump."""
        start_ms = to_ms(self.start_time)
        end_ms = to_ms(self.end_time)
        return f"susde-{self.source}-{PRIMARY_SYMBOL}-1h-{start_ms}-{end_ms}"

    def _build_inner_loader(self) -> BinanceSpotPriceLoader:
        """Compose the underlying Binance loader without writing its cache.

        We instantiate with the default Binance HTTP client; tests
        monkeypatch ``requests.get`` (or the loader's ``http``
        attribute) to mock the REST call.
        """
        return BinanceSpotPriceLoader(
            ticker=PRIMARY_SYMBOL,
            interval="1h",
            start_time=self.start_time,
            end_time=self.end_time,
            loader_type=self.loader_type,
        )

    # ----------------------------------------------------------------- lifecycle
    def extract(self) -> None:
        """Pull raw klines via :class:`BinanceSpotPriceLoader`.

        Stores the loader's internal ``_data`` DataFrame directly (with
        the full ``openTime / open / high / low / close / volume``
        columns); :meth:`transform` collapses it to a single ``price``
        column.
        """
        inner = self._build_inner_loader()
        inner.extract()
        inner.transform()  # normalises the DataFrame schema
        self._data = inner._data  # raw OHLCV; transform() trims this down

    def transform(self) -> None:
        """Collapse the OHLCV frame to ``price`` indexed by UTC time."""
        if self._data is None or len(self._data) == 0:
            self._data = pd.DataFrame(
                {"price": pd.Series([], dtype=float)},
                index=pd.DatetimeIndex([], tz="UTC", name="time"),
            )
            return
        idx = pd.to_datetime(self._data["openTime"], utc=True)
        idx.name = "time"
        self._data = pd.DataFrame(
            {"price": self._data["close"].astype(float).to_numpy()},
            index=pd.DatetimeIndex(idx),
        )

    def read(self, with_run: bool = False) -> pd.DataFrame:
        """Return the cached / freshly-loaded ``price`` DataFrame.

        With ``with_run=True`` the loader runs the full
        extract -> transform -> load pipeline and writes the cache. With
        ``with_run=False`` it reads the on-disk artefact produced by a
        prior run.
        """
        if with_run:
            self.run()
        else:
            self._read(self._cache_key())
            self._data = _restore_utc_index(self._data)
        return self._data


# --------------------------------------------------------------------- helpers
def _restore_utc_index(df: pd.DataFrame) -> pd.DataFrame:
    """Re-attach the UTC DatetimeIndex after a CSV/JSON round-trip.

    ``pd.read_csv`` / ``pd.read_json`` materialise the index as a plain
    column; we promote it back so consumers see the documented schema.
    """
    if df is None or df.empty:
        return pd.DataFrame(
            {"price": pd.Series([], dtype=float)},
            index=pd.DatetimeIndex([], tz="UTC", name="time"),
        )
    if "time" in df.columns:
        idx = pd.to_datetime(df["time"], utc=True)
        df = df.drop(columns=["time"])
    else:
        # CSV without a named index header — first column carries the timestamps.
        first = df.columns[0]
        idx = pd.to_datetime(df[first], utc=True)
        df = df.drop(columns=[first])
    df.index = pd.DatetimeIndex(idx, name="time")
    df["price"] = df["price"].astype(float)
    return df[["price"]]


def _parse_klines_payload(rows: List[List[Any]]) -> Dict[str, List[float]]:
    """Parse a raw Binance klines payload into close prices + times.

    Helper kept module-level so tests can exercise the parsing logic
    without instantiating the full loader. Each row follows Binance's
    public schema: ``[openTime_ms, open, high, low, close, volume, ...]``.
    """
    times: List[int] = []
    closes: List[float] = []
    for row in rows:
        times.append(int(row[0]))
        closes.append(float(row[4]))
    return {"openTime_ms": times, "close": closes}  # type: ignore[dict-item]


def _direct_rest_call(
    start_time: datetime,
    end_time: datetime,
    *,
    symbol: str = PRIMARY_SYMBOL,
    interval: str = "1h",
    session: Optional[requests.Session] = None,
) -> List[List[Any]]:
    """Single-shot helper that hits :data:`BINANCE_REST_URL` directly.

    Used by the live integration test and available for callers who
    want a one-call escape hatch without the full Loader pipeline.
    Does not paginate — callers must keep the window narrow enough
    that Binance returns the whole range in one ``limit=1000`` batch.
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": to_ms(start_time),
        "endTime": to_ms(end_time),
        "limit": 1000,
    }
    sess = session or requests
    resp = sess.get(BINANCE_REST_URL, params=params, timeout=15.0)
    resp.raise_for_status()
    return resp.json()
