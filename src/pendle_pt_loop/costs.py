"""Gas / friction cost models per network.

Loop strategies on Pendle + Morpho touch many contracts per cycle:

* Pendle ``swapExactTokenForPt``       — ~250-400k gas
* Morpho ``supplyCollateral``          — ~150k gas
* Morpho ``borrow``                    — ~250k gas

At Ethereum mainnet 20 gwei / ETH \$2400 a single cycle (3 calls)
costs about \$30-50. A five-cycle open is \$150-250; the symmetric
unwind another \$120-200. On Arbitrum gas is 15-25× cheaper.

We model these as fixed-USDC deductions applied at well-defined
moments in the strategy lifecycle:

* ``open_cost_usdc``        — paid once on the open action sequence.
* ``unwind_cost_usdc``      — paid once on the unwind sequence.
* ``rebalance_cost_usdc``   — paid per dynamic-controller rebalance.

The deductions are debited from the PT entity's ``cash`` directly
(same direct-mutation channel the strategy already uses to mirror
borrow → wallet cash). Numbers are conservative midpoints of the
ranges above; loop strategies expose them as configuration so
sensitivity analysis can vary them independently.

Reference ranges (collected mid-2025 to mid-2026 on-chain):

* **Ethereum mainnet** (20 gwei, ETH \$2400, three-call cycle):
    cycle ≈ \$30, 5-cycle round-trip ≈ \$300.
* **Arbitrum** (0.1 gwei effective L2 fee, ETH \$2400, three-call cycle):
    cycle ≈ \$1.5, 5-cycle round-trip ≈ \$20.

The defaults set ``open`` and ``unwind`` to round-trip / 2 on the
generous side so the modelled cost slightly *over*-estimates real
friction — the strategy looks slightly worse than reality, which is
the conservative side to err on for a published result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

NetworkName = Literal["ethereum", "arbitrum", "base", "zero"]


@dataclass
class GasModel:
    """Fixed-cost friction model in USDC.

    Attributes:
        open_cost_usdc:        deducted once when the loop opens.
        unwind_cost_usdc:      deducted once when the loop closes.
        rebalance_cost_usdc:   deducted on each dynamic-controller rebalance.
        name:                  free-form label for the summary table.
    """

    open_cost_usdc: float = 0.0
    unwind_cost_usdc: float = 0.0
    rebalance_cost_usdc: float = 0.0
    name: str = "zero"

    @classmethod
    def ethereum(cls) -> "GasModel":
        """Conservative Ethereum-mainnet defaults (mid-2025 rates)."""
        return cls(
            open_cost_usdc=150.0,
            unwind_cost_usdc=125.0,
            rebalance_cost_usdc=30.0,
            name="ethereum",
        )

    @classmethod
    def arbitrum(cls) -> "GasModel":
        """Arbitrum-One defaults — about 15× cheaper than mainnet."""
        return cls(
            open_cost_usdc=10.0,
            unwind_cost_usdc=8.0,
            rebalance_cost_usdc=2.0,
            name="arbitrum",
        )

    @classmethod
    def base(cls) -> "GasModel":
        """Base L2 — about as cheap as Arbitrum."""
        return cls(
            open_cost_usdc=8.0,
            unwind_cost_usdc=6.0,
            rebalance_cost_usdc=1.5,
            name="base",
        )

    @classmethod
    def zero(cls) -> "GasModel":
        """No friction at all — used by unit tests and theoretical comparisons."""
        return cls(name="zero")

    @classmethod
    def for_network(cls, network: NetworkName) -> "GasModel":
        """Map a network name to its default ``GasModel``."""
        if network == "ethereum":
            return cls.ethereum()
        if network == "arbitrum":
            return cls.arbitrum()
        if network == "base":
            return cls.base()
        return cls.zero()


__all__ = ["GasModel", "NetworkName"]
