"""Strategy implementations for the PT-loop backtest.

* Baselines (``baselines``) — three reference strategies for risk/return
  comparison. All inherit ``BaseStrategy[BaselineParams]``:

  - ``HoldUSDCStrategy`` — pure cash, equity stays flat.
  - ``HoldSUSDeStrategy`` — accrues the PT-implied yield (as a proxy
    for realized sUSDe staking yield) on the initial deposit.
  - ``HoldPTNoLeverageStrategy`` — buy PT on day 1, hold to expiry,
    redeem 1:1. Confirms entity math + observation pipeline:
    realised APY matches implied APY within ~12 basis points (the
    AMM fee on entry).

* Static loop (``static_loop``) — the headline Variant 1 strategy.

  - ``StaticLoopStrategy`` — fixed-LTV N-cycle PT loop. Three states:
    ``"uninvested" → "open" → "unwound"``. Opens on the first
    eligible tick by emitting a ``1 + 4N`` action sequence (initial
    deposit, then per cycle: buy_pt, morpho_deposit, morpho_borrow,
    pt_deposit_mirroring_borrow). Idle in the middle. Unwinds at
    expiry via a flash-repay model — flagged as Session-5 tech debt.

Slot naming convention: strategies register entities under names
``"PT"`` and ``"MORPHO"`` matching ``pendle_pt_loop.observations.PT_SLOT``
and ``MORPHO_SLOT``. Anything else silently breaks the observation
plumbing.
"""

from pendle_pt_loop.strategies.baselines import (
    BaselineParams,
    HoldPTNoLeverageStrategy,
    HoldSUSDeStrategy,
    HoldUSDCStrategy,
)
from pendle_pt_loop.strategies.dynamic_loop import (
    DynamicLoopParams,
    DynamicLoopStrategy,
)
from pendle_pt_loop.strategies.static_loop import (
    StaticLoopParams,
    StaticLoopStrategy,
)

__all__ = [
    "BaselineParams",
    "DynamicLoopParams",
    "DynamicLoopStrategy",
    "HoldPTNoLeverageStrategy",
    "HoldSUSDeStrategy",
    "HoldUSDCStrategy",
    "StaticLoopParams",
    "StaticLoopStrategy",
]
