"""
Kalshi trading-fee formulas (see DESIGN.md / api.md).

Kalshi charges a per-contract fee that depends on the execution price and whether
you were the maker (resting limit, low fee) or taker (crossing the spread, high
fee). NBA markets carry a 1.15x multiplier. The fee is rounded UP to the nearest
cent on the TOTAL (per the published formula).

Formulas (C = number of contracts, P = price in dollars 0-1):
    maker : ceil_to_cent( 0.0175 * C * P * (1 - P) * nba_mult )
    taker : ceil_to_cent( 0.07   * C * P * (1 - P) * nba_mult )
where nba_mult = 1.15 for NBA (KXNBAGAME) markets.

The strategy is only viable on MAKER fills, which is why order_manager defaults to
resting limit orders.
"""
from __future__ import annotations

import math

MAKER_RATE = 0.0175
TAKER_RATE = 0.07
NBA_MULT = 1.15


def _ceil_cent(x: float) -> float:
    """Round a dollar amount UP to the next cent."""
    return math.ceil(round(x * 100, 6)) / 100.0


def fee(count: int, price: float, *, maker: bool = True, nba: bool = True) -> float:
    """
    Total fee in dollars for ``count`` contracts at ``price`` dollars.

    Parameters
    ----------
    count : int      number of contracts
    price : float    execution price in dollars (0-1)
    maker : bool     True for resting-limit (maker) fills, False for taker
    nba   : bool     apply the 1.15x NBA multiplier (default True for this project)
    """
    if count <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(price)))
    rate = MAKER_RATE if maker else TAKER_RATE
    mult = NBA_MULT if nba else 1.0
    raw = rate * count * p * (1.0 - p) * mult
    return _ceil_cent(raw)


if __name__ == "__main__":
    print("fee maker 5@0.50 NBA :", fee(5, 0.50, maker=True))   # 0.0175*5*0.25*1.15 = 0.02516 -> 0.03
    print("fee taker 5@0.50 NBA :", fee(5, 0.50, maker=False))  # 0.07*5*0.25*1.15   = 0.10063 -> 0.11
    print("fee maker 1@0.95 NBA :", fee(1, 0.95, maker=True))   # tiny -> 0.01 (ceil)
    print("fee maker 100@0.40   :", fee(100, 0.40, maker=True, nba=False))
