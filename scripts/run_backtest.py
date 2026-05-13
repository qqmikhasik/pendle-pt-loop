"""End-to-end backtest CLI — runs all four strategies on the same window.

Pulls historical data (Pendle + Morpho + sUSDe price), runs three baselines
and the static-LTV PT loop on it, prints a risk/return summary table, and
optionally writes per-strategy equity curves to CSV for plotting.

Default parameters are PT-sUSDE-27NOV2025 on Ethereum mainnet (a completed
6-month cycle — clean expiry, full data history). Override with CLI flags
for any other cycle.

Usage::

    python scripts/run_backtest.py --output-dir data/backtest_susde_nov2025

For a quick smoke (no live fetch — relies on cached pickle from
``build_observations.py``)::

    python scripts/run_backtest.py --from-cache data/observations.pkl

Environment variables consumed:
    DATA_PATH — base directory for the loader cache. Defaults to cwd.
"""

from __future__ import annotations

import argparse
import pickle
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from fractal.core.base import Observation

from pendle_pt_loop.observations import build_observations
from pendle_pt_loop.strategies import (
    BaselineParams,
    HoldPTNoLeverageStrategy,
    HoldSUSDeStrategy,
    HoldUSDCStrategy,
    StaticLoopParams,
    StaticLoopStrategy,
)


# Defaults: PT-sUSDE-27NOV2025 on Ethereum mainnet. Cycle ran ~May 2025
# to Nov 27 2025; data window picked deliberately to cover the meat of
# the cycle (skip first 2 weeks of low liquidity at issuance and last
# few hours of last-trade noise).
DEFAULT_PENDLE_MARKET: str = "0xb6ac3d5da138918ac4e84441e924a20daa60dbdd"
DEFAULT_PENDLE_EXPIRY: int = 1764201600  # 2025-11-27 00:00 UTC
DEFAULT_MORPHO_MARKET: str = (
    "0x05702edf1c4709808b62fe65a7d082dccc9386f858ae460ef207ec8dd1debfa2"
)
DEFAULT_MORPHO_CHAIN: str = "ethereum"
DEFAULT_START: datetime = datetime(2025, 6, 1, tzinfo=UTC)
DEFAULT_END: datetime = datetime(2025, 11, 27, tzinfo=UTC)
DEFAULT_INITIAL_BALANCE: float = 10_000.0


@dataclass
class StrategyResult:
    """Summary of a single strategy run."""

    name: str
    initial_balance: float
    final_balance: float
    duration_days: float
    net_pnl: float
    total_return: float
    apy: float

    @classmethod
    def from_dataframe(
        cls, name: str, df: pd.DataFrame, initial: float
    ) -> StrategyResult:
        final = float(df["net_balance"].iloc[-1])
        start_ts = pd.to_datetime(df["timestamp"].iloc[0])
        end_ts = pd.to_datetime(df["timestamp"].iloc[-1])
        duration_days = (end_ts - start_ts).total_seconds() / 86400.0
        years = duration_days / 365.25 if duration_days > 0 else 1e-9
        total_return = final / initial - 1.0
        # Use simple (not compounded) annualisation for legibility in the
        # summary table; the StrategyResult.get_default_metrics() also
        # reports a different convention if needed.
        apy = total_return / years
        return cls(
            name=name,
            initial_balance=initial,
            final_balance=final,
            duration_days=duration_days,
            net_pnl=final - initial,
            total_return=total_return,
            apy=apy,
        )


def _print_summary(results: list[StrategyResult]) -> None:
    rows = [
        f"  {r.name:<26}  ${r.final_balance:>11,.2f}  "
        f"{r.total_return:+.4f}  {r.apy:+.4f}  "
        f"PnL ${r.net_pnl:+,.2f}"
        for r in results
    ]
    print()
    print(
        f"  {'Strategy':<26}  {'Final':>12}  "
        f"{'TotalRet':>8}  {'APY':>8}  {'PnL':>14}"
    )
    print("  " + "-" * 78)
    for row in rows:
        print(row)
    print()


def _write_curves(
    results: dict[str, pd.DataFrame], output_dir: Path
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, df in results.items():
        out = output_dir / f"equity_{name}.csv"
        df.to_csv(out, index=False)
    print(f"Saved {len(results)} equity curves to {output_dir}")


def _filter_observations_for(strategy, observations):
    """Strip slots from each observation that aren't registered on strategy.

    Fractal's ``_validate_observation`` rejects any slot present in the
    observation but not registered as a NamedEntity. The PT loop strategy
    registers both PT and MORPHO; baselines register only PT. We rebuild
    a per-strategy observation list with the matching subset of slots.
    """
    registered = set(strategy.get_all_available_entities().keys())
    filtered: list[Observation] = []
    for o in observations:
        states = {k: v for k, v in o.states.items() if k in registered}
        filtered.append(Observation(timestamp=o.timestamp, states=states))
    return filtered


def _run_one(name: str, strategy, observations, initial: float) -> tuple[StrategyResult, pd.DataFrame]:
    print(f"  running {name}...", flush=True)
    obs = _filter_observations_for(strategy, observations)
    result = strategy.run(obs)
    df = result.to_dataframe()
    summary = StrategyResult.from_dataframe(name, df, initial)
    return summary, df


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pendle-market", default=DEFAULT_PENDLE_MARKET)
    parser.add_argument(
        "--pendle-expiry", type=int, default=DEFAULT_PENDLE_EXPIRY
    )
    parser.add_argument("--morpho-market", default=DEFAULT_MORPHO_MARKET)
    parser.add_argument("--morpho-chain", default=DEFAULT_MORPHO_CHAIN)
    parser.add_argument(
        "--start",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=DEFAULT_START,
    )
    parser.add_argument(
        "--end",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=DEFAULT_END,
    )
    parser.add_argument(
        "--initial-balance", type=float, default=DEFAULT_INITIAL_BALANCE
    )
    parser.add_argument(
        "--target-ltv",
        type=float,
        default=0.80,
        help="StaticLoopStrategy target LTV (must be < Morpho LLTV)",
    )
    parser.add_argument(
        "--n-cycles", type=int, default=5, help="Number of loop cycles"
    )
    parser.add_argument(
        "--from-cache",
        type=Path,
        default=None,
        help="Skip live fetch; load (observations, susde) pickle from this path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="If set, write per-strategy equity curves here as CSV.",
    )
    args = parser.parse_args()

    if args.from_cache:
        print(f"loading observations from {args.from_cache}")
        with args.from_cache.open("rb") as f:
            payload = pickle.load(f)
        observations = payload["observations"]
        susde = payload["susde"]
    else:
        print(
            f"fetching live data: pendle={args.pendle_market[:10]}... "
            f"morpho={args.morpho_market[:10]}... "
            f"window={args.start.date()} -> {args.end.date()}"
        )
        observations, susde = build_observations(
            start_time=args.start,
            end_time=args.end,
            pendle_market_address=args.pendle_market,
            pendle_expiry_timestamp=args.pendle_expiry,
            morpho_market_id=args.morpho_market,
            morpho_chain=args.morpho_chain,
            with_run=True,
        )

    print(
        f"loaded {len(observations)} observations; "
        f"sUSDe price range: "
        f"{susde['price'].min():.6f} - {susde['price'].max():.6f}"
    )
    if len(observations) == 0:
        print("no observations — aborting", file=sys.stderr)
        return 1

    baseline_params = BaselineParams(INITIAL_BALANCE=args.initial_balance)
    loop_params = StaticLoopParams(
        INITIAL_BALANCE=args.initial_balance,
        TARGET_LTV=args.target_ltv,
        N_CYCLES=args.n_cycles,
    )

    strategies = [
        ("HoldUSDC", HoldUSDCStrategy(params=baseline_params)),
        ("HoldSUSDe", HoldSUSDeStrategy(params=baseline_params)),
        ("HoldPTNoLeverage", HoldPTNoLeverageStrategy(params=baseline_params)),
        (
            f"StaticLoop_LTV{args.target_ltv:.2f}_N{args.n_cycles}",
            StaticLoopStrategy(params=loop_params),
        ),
    ]

    results: list[StrategyResult] = []
    curves: dict[str, pd.DataFrame] = {}
    for name, strat in strategies:
        summary, df = _run_one(name, strat, observations, args.initial_balance)
        results.append(summary)
        curves[name] = df

    _print_summary(results)

    if args.output_dir:
        _write_curves(curves, args.output_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
