"""Risk / control utilities.

Session 1: empty. Session 5 populates with:

* ``first_passage_probability`` — closed-form Π_liq(L; h) from Krestenko
  paper, adapted: GBM underlying = PT price, barrier = LTV → LLTV.
* ``LTVBandController`` — asymmetric [LTV_L, LTV_U] band:
    - LTV_U from a liquidation budget ε_liq over horizon h_liq
      (solvency-driven, structural);
    - LTV_L from a carry-vs-rebalance-cost tradeoff (economic, may
      vanish at high rebalance cost).
"""
