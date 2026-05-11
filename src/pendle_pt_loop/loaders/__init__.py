"""Data loaders for Pendle / Morpho / underlying assets.

Session 1: empty. Session 3 populates with:

* ``PendleMarketLoader`` — PT mark + implied yield from Pendle subgraph.
* ``MorphoMarketLoader`` — borrow rate + utilization from Morpho subgraph.
* ``StableYieldPriceLoader`` — sUSDe price feed (Binance + on-chain).
* (Session 6) ``PendleBorosLoader`` for the hedge leg.

All loaders cache locally under ``data/cache/`` (gitignored).
"""
