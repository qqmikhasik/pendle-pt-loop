# Pendle PT-Loop — Leveraged Carry with Dynamic LTV Control

Research code for **Project 2** of the Blockchain & DeFi special course.
Implements and backtests a leveraged carry strategy on Pendle PT-sUSDe
collateralized in Morpho, with two variants:

1. **Static PT-loop** — fixed loan-to-value (LTV) ratio, classical
   leveraged carry baseline.
2. **Dynamic-LTV controller** — asymmetric band $[L_L, L_U]$ around a target
   $L^\dagger$, with the upper bound derived from a first-passage-probability
   constraint (analog to the $\alpha$-controller of the Krestenko *Vega Basis*
   paper, adapted from perpetual margin to Morpho health factor).
3. **PT-loop + Pendle Boros hedge** — overlays variant 1 with a long
   position on Pendle Boros to hedge implied-yield-rise risk; expected
   to improve Sharpe at the cost of some carry.

Framework: [`fractal-defi`](https://github.com/Logarithm-Labs/fractal-defi) v1.3.1.

## Hypothesis

The premium of a leveraged PT carry strategy over the risk-free rate
(~10 percentage points per six months on Arbitrum at the time of writing)
is fully explained by liquidation risk. An optimal dynamic LTV controller
preserves the majority of carry while reducing maximum drawdown by an
order of magnitude — the same qualitative result Krestenko *et al.* (2026)
report for spot–perpetual basis on Hyperliquid, transferred here to a
new asset class (Pendle fixed-yield tokens) and a new safety primitive
(Morpho health factor).

## Repository layout

```
pendle-pt-loop/
├── src/pendle_pt_loop/
│   ├── entities/        # PendlePT, Morpho, PendleBoros entities (extend fractal BaseEntity)
│   ├── strategies/      # Static / dynamic / hedged variants
│   ├── loaders/         # Pendle subgraph, Morpho subgraph, sUSDe price feed
│   └── risk/            # First-passage probability, LTV controller
├── tests/               # Pytest unit + invariant + scenario tests
├── notebooks/           # Exploratory + final-figure notebooks
├── scripts/             # CLI entry points for backtest + grid search
├── docs/                # Whitepaper (LaTeX) + figures
└── data/                # Loader caches (gitignored)
```

## Status — complete

All 7 development sessions delivered; 153 tests passing.

| Session | Deliverable |
|---|---|
| 1 | Repo scaffold + entity stubs + smoke tests |
| 2 | Real Pendle/Morpho math + invariant tests |
| 3 | Pendle / Morpho / sUSDe loaders + observation builder |
| 4 | Baselines (3) + static loop + first live backtest |
| 5 | Dynamic LTV controller (first-passage probability + asymmetric band) |
| 6 | Funding-rate hedge variant + Sharpe/MaxDD metrics |
| 7 | HEDGE_RATIO sweep + LaTeX whitepaper + Extra+1 PR plan |

### Headline results (PT-sUSDE-27NOV2025, 60 days, 1,416 hourly obs)

| Strategy | Final | APY | Sharpe | MaxDD |
|---|---:|---:|---:|---:|
| HoldUSDC | $10,000 | 0.00% | 0.00 | 0.00% |
| HoldPTNoLeverage | $10,114.79 | +7.11% | 9.91 | −0.10% |
| StaticLoop_LTV0.80_N5 | $10,140.46 | +8.70% | 4.14 | −0.36% |
| DynamicLoop_LTV0.80_N5 | $10,140.46 | +8.70% | 4.14 | −0.36% |
| **HedgedLoop_LTV0.80_HR1.0** | **$10,549.70** | **+34.05%** | **13.28** | −0.59% |

The whitepaper is at [`docs/whitepaper.pdf`](docs/whitepaper.pdf).

## Local setup

```bash
python -m venv .venv
source .venv/Scripts/activate   # Git Bash on Windows; on POSIX: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## API keys

All Session 1-4 loaders use **keyless public APIs** (Pendle REST, Morpho
GraphQL, Binance public REST), so the backtest runs without any keys
configured. Optional integrations that DO need keys are listed in
`.env.example`:

- `ALCHEMY_URL` — for direct on-chain reads (e.g. verifying sUSDe
  `pricePerShare` in Session 5/6 if depeg modeling becomes relevant).
- `ETHERSCAN_API_KEY` — for transaction / contract-state spot checks.
- `THEGRAPH_API_KEY` — fallback subgraph access for Pendle / Morpho if
  the public REST/GraphQL endpoints become rate-limited.
- `DUNE_API_KEY` — Dune dashboards for the whitepaper (Session 7).

Copy `.env.example` to `.env` and fill in real values; `.env` is
gitignored.

## License

BSD-3-Clause, matching the parent `fractal-defi` license.

## References

- Krestenko, Butov, Berezovskiy, Bolotin. *Dynamic Collateral Control for Permissionless Spot–Perpetual Basis Trading.* 2026.
- Pendle Finance. [Documentation](https://docs.pendle.finance/).
- Morpho Labs. [Documentation](https://docs.morpho.org/).
- Logarithm Labs. [fractal-defi](https://github.com/Logarithm-Labs/fractal-defi).
- Ethena Labs. [USDe whitepaper](https://ethena-labs.gitbook.io/ethena-labs).
