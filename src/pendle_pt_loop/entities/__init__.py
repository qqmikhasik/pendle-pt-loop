"""Typed state-machine entities for the protocols this project touches.

Each entity follows the fractal-defi contract: ``GlobalState`` for
market context, ``InternalState`` for position state, ``action_*``
methods for mutations, ``update_state`` for time evolution.

Available entities:

* ``PendlePTEntity`` — Pendle Principal Token position. AMM swap with
  fee + linear slippage, expiry snap, redeem at par.
* ``MorphoEntity`` — Morpho Blue isolated-market position. Borrow-rate
  accrual, LLTV enforcement on action mutation, liquidation flag.
* ``FundingHedgeEntity`` — long-funding-rate position. Linear PnL
  accrual against an annualised funding-rate stream; conceptually the
  Pendle Boros exposure modelled directly via the underlying perp's
  funding rate.
"""

from pendle_pt_loop.entities.funding_hedge import (
    FundingHedgeConfig,
    FundingHedgeEntity,
    FundingHedgeGlobalState,
    FundingHedgeInternalState,
)
from pendle_pt_loop.entities.morpho import (
    MorphoConfig,
    MorphoEntity,
    MorphoGlobalState,
    MorphoInternalState,
)
from pendle_pt_loop.entities.pendle_pt import (
    PendlePTConfig,
    PendlePTEntity,
    PendlePTGlobalState,
    PendlePTInternalState,
)

__all__ = [
    "FundingHedgeConfig",
    "FundingHedgeEntity",
    "FundingHedgeGlobalState",
    "FundingHedgeInternalState",
    "MorphoConfig",
    "MorphoEntity",
    "MorphoGlobalState",
    "MorphoInternalState",
    "PendlePTConfig",
    "PendlePTEntity",
    "PendlePTGlobalState",
    "PendlePTInternalState",
]
