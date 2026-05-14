"""Validate that Hyperliquid funding rate is a clean proxy for Boros.

The hedged-loop strategy uses Hyperliquid ETH-USDC perpetual funding
as the underlying hedge instrument. Pendle Boros tokenises this same
rate, so going long Boros is *economically* equivalent to receiving
the underlying funding. Boros' public API only exposes the most recent
~500 bars (≈3 weeks at 1h granularity), which is too short for our
historical backtests — but it is long enough to **validate** the proxy
assumption empirically.

This script:

1. Loads ~3 weeks of Boros mark APR for Hyperliquid-ETH-26JUN2026
   (market id 74).
2. Loads the same window from Hyperliquid's own funding-history
   endpoint via :class:`FundingHedgeLoader`.
3. Aligns on hourly timestamps.
4. Reports the Pearson correlation, mean spread, and a side-by-side
   table for inspection.

A high correlation (≥0.7) and small mean spread (<50 bps) supports
the claim that the proxy is faithful enough for backtest purposes.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from pendle_pt_loop.loaders import BorosMarketLoader, FundingHedgeLoader


def main() -> int:
    # Boros only exposes the most recent ~500 hourly bars, so the window
    # has to be the current 3 weeks regardless of when we run.
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=21)
    print(f"Validation window: {start.isoformat()} -> {end.isoformat()}")
    print()

    # 1. Boros mark APR.
    boros = BorosMarketLoader(
        market_id=74,  # HYPERLIQUID-ETH-26JUN2026
        start_time=start,
        end_time=end,
    )
    df_b = boros.read(with_run=True)
    print(f"Boros bars: {len(df_b)}")
    if df_b.empty:
        print("Boros returned no data — abort.")
        return 1

    # 2. Hyperliquid funding history.
    hl = FundingHedgeLoader(
        ticker="ETH",
        start_time=start,
        end_time=end,
    )
    df_h = hl.read(with_run=True)
    print(f"Hyperliquid bars: {len(df_h)}")
    if df_h.empty:
        print("Hyperliquid returned no data — abort.")
        return 1

    # 3. Align on Boros' index (Hyperliquid sometimes has gaps).
    joined = df_b[["mark_apr"]].join(
        df_h[["funding_rate_annualised"]],
        how="inner",
    ).dropna()
    print(f"Joined bars: {len(joined)}")
    if joined.empty:
        print("No overlap.")
        return 1

    # 4. Statistics.
    mark = joined["mark_apr"]
    funding = joined["funding_rate_annualised"]
    corr = float(mark.corr(funding))
    spread = float((mark - funding).abs().mean())
    bias = float((mark - funding).mean())
    rmse = float(((mark - funding) ** 2).mean() ** 0.5)

    print()
    print("=== Statistical comparison ===")
    print(f"  Pearson correlation:        {corr:+.4f}")
    print(f"  Mean absolute spread:       {spread:.4f}   ({spread*1e4:.0f} bps)")
    print(f"  Mean bias (mark - funding): {bias:+.4f}    ({bias*1e4:+.0f} bps)")
    print(f"  RMSE:                       {rmse:.4f}    ({rmse*1e4:.0f} bps)")
    print()
    print(f"  Boros mark range:           [{mark.min():+.4f}, {mark.max():+.4f}]")
    print(f"  HL funding range:           [{funding.min():+.4f}, {funding.max():+.4f}]")

    # 5. Verdict.
    print()
    if corr >= 0.7 and spread < 0.05:
        print("PROXY VALIDATED: Hyperliquid funding tracks Boros mark "
              "closely enough for backtest substitution.")
    elif corr >= 0.5:
        print("PROXY ACCEPTABLE: moderate correlation, document spread as caveat.")
    else:
        print("PROXY QUESTIONABLE: low correlation, consider native Boros "
              "integration as a follow-up.")

    # 6. Persist to CSV for the whitepaper appendix.
    out_dir = Path("data/boros_validation")
    out_dir.mkdir(parents=True, exist_ok=True)
    joined.to_csv(out_dir / "boros_vs_hyperliquid.csv")
    pd.DataFrame([{
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "n_observations": len(joined),
        "pearson_correlation": corr,
        "mean_absolute_spread": spread,
        "bias_mark_minus_funding": bias,
        "rmse": rmse,
    }]).to_csv(out_dir / "summary.csv", index=False)
    print(f"\nSaved {out_dir / 'boros_vs_hyperliquid.csv'}")
    print(f"Saved {out_dir / 'summary.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
