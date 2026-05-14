"""HEDGE_RATIO sensitivity sweep for the HedgedLoopStrategy.

Runs the full strategy panel once for each HEDGE_RATIO in a configurable
grid and emits a CSV summarising final balance, APY, Sharpe, and max
drawdown per ratio. Loader caches mean only the first ratio fetches
live data; subsequent ratios reuse the CSV cache.

Used to populate the sensitivity table in the Project 2 whitepaper.

Usage::

    python scripts/sweep_hedge_ratio.py --output data/sweep_hedge.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from fractal.core.base import Observation

from pendle_pt_loop.observations import build_observations
from pendle_pt_loop.strategies import (
    HedgedLoopParams,
    HedgedLoopStrategy,
    StaticLoopParams,
    StaticLoopStrategy,
)


_DEFAULT_PENDLE_MARKET = "0xb6ac3d5da138918ac4e84441e924a20daa60dbdd"
_DEFAULT_PENDLE_EXPIRY = 1764201600
_DEFAULT_MORPHO_MARKET = (
    "0x05702edf1c4709808b62fe65a7d082dccc9386f858ae460ef207ec8dd1debfa2"
)
_DEFAULT_START = datetime(2025, 9, 29, tzinfo=UTC)
_DEFAULT_END = datetime(2025, 11, 27, tzinfo=UTC)
_DEFAULT_GRID = (0.0, 0.25, 0.50, 0.75, 1.0, 1.25, 1.50)


def _filter_observations_for(strategy, observations: list[Observation]) -> list[Observation]:
    registered = set(strategy.get_all_available_entities().keys())
    return [
        Observation(
            timestamp=o.timestamp,
            states={k: v for k, v in o.states.items() if k in registered},
        )
        for o in observations
    ]


def _summary(name: str, df: pd.DataFrame, initial: float, hedge_ratio: float) -> dict:
    equity = df["net_balance"].astype(float).to_numpy()
    final = float(equity[-1])
    start_ts = pd.to_datetime(df["timestamp"].iloc[0])
    end_ts = pd.to_datetime(df["timestamp"].iloc[-1])
    years = (end_ts - start_ts).total_seconds() / (365.25 * 86400)
    total_return = final / initial - 1.0
    apy = total_return / years if years > 0 else 0.0

    # Sharpe / drawdown on positive equity points only.
    eq_pos = equity[np.isfinite(equity) & (equity > 0)]
    sharpe = 0.0
    max_dd = 0.0
    if len(eq_pos) >= 2:
        hr = np.diff(eq_pos) / eq_pos[:-1]
        sigma = float(np.std(hr, ddof=1))
        if sigma > 0:
            mu = float(np.mean(hr))
            sharpe = (mu / sigma) * (365.25 * 24.0) ** 0.5
        rmax = np.maximum.accumulate(eq_pos)
        max_dd = float(((eq_pos - rmax) / rmax).min())

    return {
        "strategy": name,
        "hedge_ratio": hedge_ratio,
        "final": final,
        "total_return": total_return,
        "apy": apy,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--grid",
        type=float,
        nargs="+",
        default=list(_DEFAULT_GRID),
        help="HEDGE_RATIO values to evaluate.",
    )
    p.add_argument(
        "--initial-balance", type=float, default=10_000.0
    )
    p.add_argument(
        "--target-ltv", type=float, default=0.80
    )
    p.add_argument(
        "--n-cycles", type=int, default=5
    )
    p.add_argument(
        "--output", type=Path, default=Path("data/sweep_hedge.csv")
    )
    args = p.parse_args()

    print(
        f"fetching observation stream once "
        f"({_DEFAULT_START.date()} - {_DEFAULT_END.date()})"
    )
    observations, susde = build_observations(
        start_time=_DEFAULT_START,
        end_time=_DEFAULT_END,
        pendle_market_address=_DEFAULT_PENDLE_MARKET,
        pendle_expiry_timestamp=_DEFAULT_PENDLE_EXPIRY,
        morpho_market_id=_DEFAULT_MORPHO_MARKET,
        morpho_chain="ethereum",
        with_run=True,
    )
    print(
        f"loaded {len(observations)} obs; "
        f"sUSDe range {susde['price'].min():.4f}-{susde['price'].max():.4f}"
    )

    rows: list[dict] = []

    # Static loop reference run (no hedge).
    static = StaticLoopStrategy(
        params=StaticLoopParams(
            INITIAL_BALANCE=args.initial_balance,
            TARGET_LTV=args.target_ltv,
            N_CYCLES=args.n_cycles,
        )
    )
    df_static = static.run(_filter_observations_for(static, observations)).to_dataframe()
    rows.append(_summary("StaticLoop", df_static, args.initial_balance, hedge_ratio=float("nan")))

    # Hedged variants across the grid.
    for ratio in args.grid:
        strat = HedgedLoopStrategy(
            params=HedgedLoopParams(
                INITIAL_BALANCE=args.initial_balance,
                TARGET_LTV=args.target_ltv,
                N_CYCLES=args.n_cycles,
                HEDGE_RATIO=ratio,
            )
        )
        print(f"  running HedgedLoop HR={ratio:.2f}...", flush=True)
        df = strat.run(_filter_observations_for(strat, observations)).to_dataframe()
        rows.append(_summary("HedgedLoop", df, args.initial_balance, hedge_ratio=ratio))

    out = pd.DataFrame(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)
    print()
    print(
        out.to_string(
            index=False,
            float_format=lambda x: f"{x:.4f}",
        )
    )
    print()
    print(f"sweep written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
