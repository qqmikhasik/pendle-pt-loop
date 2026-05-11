"""Pendle PT-Loop — leveraged Pendle PT carry strategy with dynamic LTV control.

Project 2 of the Blockchain & DeFi special course. Backtested on
``fractal-defi`` framework (https://github.com/Logarithm-Labs/fractal-defi).

Top-level package layout:

* ``entities`` — Pendle PT, Morpho lending, (later) Pendle Boros entities
  that extend ``fractal.core.base.BaseEntity``.
* ``strategies`` — static PT-loop, dynamic-LTV PT-loop, and the
  Boros-hedged variant.
* ``loaders`` — Pendle subgraph, Morpho subgraph, sUSDe price feed.
* ``risk`` — first-passage probability and the asymmetric LTV-band
  controller.
"""

__version__ = "0.1.0"
