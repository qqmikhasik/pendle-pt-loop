# Extra+1: PR plan — Pendle market loader into `fractal-defi`

This document plans the **Extra+1** contribution: a verified pull
request to [`Logarithm-Labs/fractal-defi`](https://github.com/Logarithm-Labs/fractal-defi)
that contributes our Pendle historical-data loader to the core
library.

## Why this is the right candidate

Of the four new loaders we wrote (Pendle, Morpho, sUSDe, Hyperliquid
funding-rate wrapper), the Pendle loader is the best PR candidate:

1. **Adds a missing protocol.** `fractal-defi` 1.3.1 covers Aave,
   Hyperliquid, Uniswap V2/V3, Lido, GMX. Pendle — by some metrics the
   largest yield-tokenisation protocol — has no loader.
2. **Keyless public API.** No new environment variables; tests can run
   on CI without secrets. Pendle's
   `/core/v1/{chain}/markets/{addr}/historical-data` is keyless and
   stable.
3. **Self-contained.** Pendle loader has no dependency on the
   Morpho / sUSDe / Hyperliquid wrappers; can ship as a single file
   plus its tests.
4. **Documented.** Module docstring already explains payload shape,
   linear vs. exponential pricing modes, and the no-direct-`pt_price`
   workaround.

The Morpho loader is a possible second PR (also self-contained, also
keyless), but Morpho Blue uses a less stable public GraphQL surface
than Pendle — better to merge Pendle first and let the maintainers
decide on Morpho as a follow-up.

## Files to adapt for upstream

Source side (single new file):

```
fractal-defi/fractal/loaders/pendle.py
```

Adapt from `pendle-pt-loop/src/pendle_pt_loop/loaders/pendle.py`.
Changes:

* Drop the `from pendle_pt_loop.entities.pendle_pt import compute_pt_price`
  dependency. Either:
  - inline the helper (one short function, ~15 lines), or
  - put `compute_pt_price` into a new `fractal/loaders/_pendle_pricing.py`.
  The first option is cleaner for the PR (one file).
* Adjust the loader output struct. fractal-defi uses
  `fractal.loaders.structs` typed structs (e.g. `FundingHistory`,
  `KlinesHistory`, `PriceHistory`). Add a new `PendleMarketHistory`
  struct with `pt_price`, `implied_yield`, `seconds_to_expiry`, and
  `pool_liquidity` arrays.
* Match fractal's `Loader._cache_key` naming convention and the
  existing The-Graph loader formatting.
* Convert docstrings to RST format for Sphinx (fractal uses
  Sphinx-RTD).

Tests side:

```
fractal-defi/tests/loaders/test_pendle.py
```

Port `pendle-pt-loop/tests/loaders/test_pendle_loader.py` with two
changes:

* Use fractal's existing test fixtures (monkeypatch HTTP via the same
  patterns used in `tests/loaders/test_thegraph_uniswap_v2.py`).
* Add one live integration test gated by an env var
  (e.g. `PENDLE_INTEGRATION=1`).

Documentation:

```
fractal-defi/docs/loaders/pendle.rst
```

One-page Sphinx page mirroring `docs/loaders/binance.rst` (which I
should read before writing this). Describe the data source, the
output struct, caching behaviour, and a minimal usage example.

CHANGELOG entry:

```
fractal-defi/CHANGELOG.md
```

Add an entry under the next minor version:

> - Added `PendleMarketLoader` for Pendle V3 historical market data
>   (PT price, implied yield, seconds to expiry, pool liquidity).
>   Uses the keyless Pendle public REST API.

## PR title and description

**Title.** `feat(loaders): add Pendle V3 market loader`

**Description.**

> Adds historical-data loader for Pendle V3 markets, mirroring the
> shape of the existing The-Graph and Binance loaders. Output struct:
> `PendleMarketHistory(pt_price, implied_yield, seconds_to_expiry,
> pool_liquidity)` indexed by UTC datetime, hourly cadence.
>
> Backed by Pendle's public REST endpoint
> `/core/v1/{chain}/markets/{addr}/historical-data` (keyless). No new
> required dependencies; uses only `requests` and `pandas`. Tests
> include a mocked unit suite and a single live integration test
> gated by `PENDLE_INTEGRATION=1`.
>
> Motivated by ongoing research on leveraged PT-carry strategies; the
> upstream addition lets other users build on the same loader without
> rebuilding the parser. Reference implementation has been used in
> production for a 153-test research project, full results to be
> linked once published.

## Branch and PR mechanics

1. Fork `Logarithm-Labs/fractal-defi` to `qqmikhasik/fractal-defi`.
2. Branch off `dev` (CONTRIBUTING says active work lives on `dev`):
   `git checkout -b feat/pendle-loader dev`.
3. Implement the three files described above.
4. Run `pre-commit run --all-files`; fix flake8/pylint nits.
5. Run `pytest -q -x tests/loaders/test_pendle.py`. All green.
6. Run the full suite `pytest -q` and confirm no regressions.
7. Push and open the PR against `dev`.
8. Watch CI; expect the live integration test to be skipped on CI.

## Estimated effort

3–4 hours, mostly stylistic / packaging:

- 30 min: read `ARCHITECTURE.md` end-to-end and the closest example
  (likely The-Graph Uniswap V2 loader).
- 60 min: port `pendle.py`, restructure for upstream conventions.
- 60 min: port tests, adapt to upstream fixture patterns.
- 30 min: write the RST doc page and CHANGELOG entry.
- 30 min: run hooks, push, write the PR description.

## Status (as of project submission)

PR not yet opened. The student should:

1. Review this plan against the actual state of `fractal-defi` `dev`.
2. Decide whether to open the PR before or after course submission.
3. If after, the PR can mention the course project as motivation and
   link the public GitHub repo once it exists.
