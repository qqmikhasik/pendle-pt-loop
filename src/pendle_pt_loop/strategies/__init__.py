"""Strategy implementations.

Session 1: empty. Session 4 onwards populates this with:

* ``StaticLoopStrategy`` — fixed-LTV PT loop.
* ``DynamicLoopStrategy`` — Session 5; [LTV_L, LTV_U] band controller.
* ``BorosHedgedLoopStrategy`` — Session 6; loop + Pendle Boros hedge.
* baselines for comparison (hold USDC, hold sUSDe, hold PT-no-leverage).
"""
