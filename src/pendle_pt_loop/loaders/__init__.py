"""Historical data loaders for Pendle / Morpho / underlying assets.

Each loader follows the ``fractal.loaders.base_loader.Loader`` lifecycle:
``extract → transform → load``, with on-disk CSV caching under
``<DATA_PATH or cwd>/fractal_data/<loader_class>/<key>.csv``.

Loaders:

* ``PendleMarketLoader`` — historical PT mark, implied yield, seconds
  to expiry, and pool liquidity for a single Pendle market. Backed by
  the keyless Pendle GraphQL API.
* ``MorphoMarketLoader`` — historical borrow APY, supply APY, and
  utilization for a single Morpho Blue isolated market. Backed by the
  keyless Morpho GraphQL API.
* ``SUSDePriceLoader`` — historical sUSDe spot price in USDC, used for
  depeg detection and (eventually) redeem-time accounting. Falls back
  to Binance ``USDEUSDT`` because ``SUSDEUSDT`` does not exist on
  Binance.

Use ``build_observations`` (from ``pendle_pt_loop.observations``) to
join the three feeds into a list of ``fractal.core.base.Observation``
ready to be passed into a strategy's ``run`` loop.
"""

from pendle_pt_loop.loaders.morpho import MorphoMarketLoader
from pendle_pt_loop.loaders.pendle import PendleMarketLoader
from pendle_pt_loop.loaders.susde_price import SUSDePriceLoader

__all__ = [
    "MorphoMarketLoader",
    "PendleMarketLoader",
    "SUSDePriceLoader",
]
