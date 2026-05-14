"""Join Pendle / Morpho / sUSDe feeds into a strategy-ready Observation list.

``build_observations`` is the integration glue between the three Session-3
loaders and a fractal-defi strategy. It produces:

1. A list of ``Observation`` objects — one per hourly slot in the
   requested window — each carrying:

   * ``"PT"`` → ``PendlePTGlobalState`` populated from the Pendle feed.
   * ``"MORPHO"`` → ``MorphoGlobalState`` populated from the Morpho
     feed, with ``collateral_price`` set to the matching ``pt_price``
     and ``timestamp_seconds`` from the observation timestamp.

2. A side ``DataFrame`` carrying the sUSDe spot price aligned to the
   same hourly index. This is currently unused by the entities but
   exposed for diagnostics, depeg detection plots, and a planned
   Session-5/6 use in the redeem-time SY → USDC conversion.

The join is an **inner join** on the hourly UTC index — any timestamp
missing from any of the three feeds is dropped. We forward-fill within
each feed before the join so that natural Pendle/Morpho update cadences
(typically every block or every few minutes) don't punch holes in the
hourly grid.

The strategy registers entities under the names ``"PT"`` and
``"MORPHO"``; ``build_observations`` writes the matching keys into
``Observation.states``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from fractal.core.base import Observation
from fractal.loaders.base_loader import LoaderType

from pendle_pt_loop.entities import (
    FundingHedgeGlobalState,
    MorphoGlobalState,
    PendlePTGlobalState,
)
from pendle_pt_loop.loaders import (
    FundingHedgeLoader,
    MorphoMarketLoader,
    PendleMarketLoader,
    SUSDePriceLoader,
)

# Entity slot names. Must match the ``NamedEntity`` names that strategies
# register in their ``set_up`` hook.
PT_SLOT: str = "PT"
MORPHO_SLOT: str = "MORPHO"
HEDGE_SLOT: str = "HEDGE"


def build_observations(
    *,
    start_time: datetime,
    end_time: datetime,
    pendle_market_address: str,
    pendle_expiry_timestamp: int,
    morpho_market_id: str,
    morpho_chain: str = "ethereum",
    susde_source: str = "binance",
    hedge_ticker: str | None = "ETH",
    hedge_scaling: float = 1.0,
    api_key: str | None = None,
    loader_type: LoaderType = LoaderType.CSV,
    with_run: bool = True,
) -> tuple[list[Observation], pd.DataFrame]:
    """Build the joined ``Observation`` stream and the sUSDe side-feed.

    Args:
        start_time: Inclusive UTC start of the backtest window.
        end_time: Inclusive UTC end.
        pendle_market_address: Pendle market contract address (20 bytes
            hex). Used to scope the Pendle GraphQL query.
        pendle_expiry_timestamp: Unix seconds at which the chosen PT
            market expires. Used to compute ``seconds_to_expiry`` per
            row.
        morpho_market_id: 32-byte Morpho market identifier.
        morpho_chain: ``"ethereum"`` | ``"arbitrum"`` | ``"base"``.
        susde_source: ``"binance"`` (default) — falls back to USDEUSDT
            inside the loader because SUSDEUSDT is not listed.
        api_key: Optional The Graph API key (Pendle / Morpho keyless
            endpoints don't need it; reserved for future routing).
        loader_type: Caching backend; defaults to ``LoaderType.CSV``.
        with_run: When ``True``, fetches fresh data; ``False`` reads
            cache only.

    Returns:
        ``(observations, susde_price_df)`` where ``observations`` is
        the list ready for ``strategy.run(observations)`` and
        ``susde_price_df`` is the aligned price series.
    """
    # Map chain name to id for Pendle's URL routing.
    _chain_id_map = {"ethereum": 1, "arbitrum": 42161, "base": 8453}
    pendle = PendleMarketLoader(
        market_address=pendle_market_address,
        expiry_timestamp=pendle_expiry_timestamp,
        start_time=start_time,
        end_time=end_time,
        chain_id=_chain_id_map.get(morpho_chain, 1),
        api_key=api_key,
        loader_type=loader_type,
    )
    morpho = MorphoMarketLoader(
        market_id=morpho_market_id,
        chain=morpho_chain,
        start_time=start_time,
        end_time=end_time,
        api_key=api_key,
        loader_type=loader_type,
    )
    susde = SUSDePriceLoader(
        start_time=start_time,
        end_time=end_time,
        source=susde_source,  # type: ignore[arg-type]
        loader_type=loader_type,
    )

    pendle_df = pendle.read(with_run=with_run)
    morpho_df = morpho.read(with_run=with_run)
    susde_df = susde.read(with_run=with_run)

    hedge_df: pd.DataFrame | None = None
    if hedge_ticker is not None:
        try:
            hedge_loader = FundingHedgeLoader(
                ticker=hedge_ticker,
                start_time=start_time,
                end_time=end_time,
                scaling=hedge_scaling,
                loader_type=loader_type,
            )
            hedge_df = hedge_loader.read(with_run=with_run)
        except Exception:
            # Hyperliquid public-info API can be flaky; gracefully skip
            # the hedge feed and let any hedge-using strategy fail loud.
            hedge_df = None

    return _join_and_pack(pendle_df, morpho_df, susde_df, hedge_df)


def _join_and_pack(
    pendle_df: pd.DataFrame,
    morpho_df: pd.DataFrame,
    susde_df: pd.DataFrame,
    hedge_df: pd.DataFrame | None = None,
) -> tuple[list[Observation], pd.DataFrame]:
    """Pure join + Observation construction. Separated from the loader
    plumbing so the unit tests can exercise it with hand-built frames.

    When ``hedge_df`` is provided and non-empty, each ``Observation``
    gains a ``HEDGE`` slot carrying a ``FundingHedgeGlobalState`` with
    the annualised funding rate at that hour.
    """
    if pendle_df.empty or morpho_df.empty:
        return [], susde_df

    pendle_df = pendle_df.sort_index().ffill()
    morpho_df = morpho_df.sort_index().ffill()
    susde_df = susde_df.sort_index().ffill()

    # Inner join PT + Morpho on the hourly UTC index.
    joined = pendle_df.join(morpho_df, how="inner")
    if joined.empty:
        return [], susde_df

    # Reindex sUSDe to the joined index (forward-fill the last known
    # spot price into any hourly slot Binance happens to skip).
    susde_aligned = susde_df.reindex(joined.index, method="ffill")

    hedge_aligned: pd.DataFrame | None = None
    if hedge_df is not None and not hedge_df.empty:
        hedge_aligned = hedge_df.sort_index().ffill().reindex(
            joined.index, method="ffill"
        )

    observations: list[Observation] = [
        _row_to_observation(
            ts,
            row,
            hedge_row=(
                hedge_aligned.loc[ts] if hedge_aligned is not None else None
            ),
            sy_price=float(susde_aligned.loc[ts, "price"])
            if (
                susde_aligned is not None
                and not susde_aligned.empty
                and ts in susde_aligned.index
                and "price" in susde_aligned.columns
            )
            else 1.0,
        )
        for ts, row in joined.iterrows()
    ]
    return observations, susde_aligned


def _row_to_observation(
    ts: pd.Timestamp,
    row: pd.Series,
    hedge_row: pd.Series | None = None,
    sy_price: float = 1.0,
) -> Observation:
    """One hourly row → one ``Observation``.

    If ``hedge_row`` is provided, a ``HEDGE`` slot is added carrying
    a ``FundingHedgeGlobalState`` with the annualised funding rate.
    ``sy_price`` (defaulting to 1.0) is plumbed into
    ``PendlePTGlobalState.sy_price_in_usdc`` so the redeem step can
    realise underlying-depeg losses correctly.
    """
    ts_seconds = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
    pt_state = PendlePTGlobalState(
        pt_price=float(row["pt_price"]),
        implied_yield=float(row["implied_yield"]),
        seconds_to_expiry=float(row["seconds_to_expiry"]),
        pool_liquidity=float(row["pool_liquidity"]),
        sy_price_in_usdc=float(sy_price),
    )
    morpho_state = MorphoGlobalState(
        collateral_price=float(row["pt_price"]),
        debt_price=1.0,
        lending_rate=0.0,
        borrowing_rate=float(row["borrowing_rate"]),
        utilization=float(row["utilization"]),
        timestamp_seconds=ts_seconds,
    )
    states: dict[str, Any] = {PT_SLOT: pt_state, MORPHO_SLOT: morpho_state}
    if hedge_row is not None:
        states[HEDGE_SLOT] = FundingHedgeGlobalState(
            funding_rate=float(hedge_row["funding_rate_annualised"]),
            timestamp_seconds=ts_seconds,
        )
    return Observation(
        timestamp=ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts,
        states=states,
    )


__all__ = ["build_observations", "PT_SLOT", "MORPHO_SLOT", "HEDGE_SLOT"]
