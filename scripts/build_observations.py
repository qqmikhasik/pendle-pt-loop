"""CLI wrapper around ``pendle_pt_loop.observations.build_observations``.

Pulls historical data from Pendle, Morpho, and Binance for a chosen
PT-sUSDe market window, joins the feeds, and pickles the resulting
``Observation`` stream to disk so the backtest in Session 4 can
``pickle.load`` it without re-hitting the APIs.

Usage::

    python scripts/build_observations.py \\
        --pendle-market 0x... \\
        --pendle-expiry 1735603200 \\
        --morpho-market 0xbc552f0b14dd6f8e60b760a534ac1d8613d3539153b4d9675d697e048f2edc7e \\
        --morpho-chain ethereum \\
        --start 2024-06-01 \\
        --end 2024-12-26 \\
        --output data/observations_susde_dec2024.pkl

Environment variables:
    THE_GRAPH_API_KEY — optional; the Pendle / Morpho GraphQL endpoints
        used here are keyless, but kept for future routing.
"""

from __future__ import annotations

import argparse
import pickle
from datetime import UTC, datetime
from pathlib import Path

from pendle_pt_loop.observations import build_observations


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pendle-market", required=True, type=str)
    parser.add_argument("--pendle-expiry", required=True, type=int)
    parser.add_argument("--morpho-market", required=True, type=str)
    parser.add_argument("--morpho-chain", default="ethereum", type=str)
    parser.add_argument("--start", required=True, type=_parse_date)
    parser.add_argument("--end", required=True, type=_parse_date)
    parser.add_argument(
        "--susde-source", default="binance", choices=["binance", "fallback"]
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/observations.pkl"),
        help="Path to pickle the (observations, susde_df) tuple to.",
    )
    args = parser.parse_args()

    observations, susde = build_observations(
        start_time=args.start,
        end_time=args.end,
        pendle_market_address=args.pendle_market,
        pendle_expiry_timestamp=args.pendle_expiry,
        morpho_market_id=args.morpho_market,
        morpho_chain=args.morpho_chain,
        susde_source=args.susde_source,
        with_run=True,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as f:
        pickle.dump({"observations": observations, "susde": susde}, f)

    print(f"Saved {len(observations)} observations to {args.output}")
    print(
        f"sUSDe price range: {susde['price'].min():.6f} – "
        f"{susde['price'].max():.6f}"
    )


if __name__ == "__main__":
    main()
