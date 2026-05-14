"""Entity stubs for Pendle / Morpho protocols.

This subpackage holds the typed state-machine wrappers around the
on-chain protocols we touch. Each entity follows the fractal-defi
contract: ``GlobalState`` for market context, ``InternalState`` for
position state, ``action_*`` methods for mutations, ``update_state``
for time evolution.

In Session 1 these are stubs — interfaces nailed down, math marked
``TODO(Session 2)``.
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
