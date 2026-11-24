"""
exec_model.py
=============
Configurable execution model for the backtester.

The backtester separates two things that the live P&L conflates:
  * SIGNAL quality  — did the strategy pick the right side / time?
  * EXECUTION quality — what fill did we actually get, at what fee?

This module owns the second. A strategy emits an intent; the engine decides a
direction and a "decision price" (the prevailing tick); this model turns that
into an actual fill under a chosen set of assumptions, so the same recorded
game can be scored as best-case, realistic, or worst-case execution.

We only have last-trade ticks (no historical L2 book), so the touch is
synthesised from a configurable half-spread around the last trade, and maker
fills are modelled by ADVERSE SELECTION: a resting order fills only if the
tape later trades through its price within the wait window. That is the honest
way to price a passive strategy without an orderbook — it captures the fact
that the convergence moves you're right about are exactly the ones a resting
order misses.

All prices are YES-frame dollars (0-1). `yes_buy` is the direction of the leg
in YES terms:
  long YES  (dir +1): open = buy YES,  close = sell YES
  long NO   (dir -1): open = sell YES, close = buy YES
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class ExecConfig:
    """One execution scenario. Name it so reports are readable."""
    name: str = "taker"
    mode: str = "taker"          # 'taker' | 'maker' | 'mid' | 'maker_then_taker'
    half_spread: float = 0.01    # synthesised half-spread around last trade (dollars)
    slippage: float = 0.0        # extra adverse slip on CROSSING fills (dollars)
    maker_wait_polls: int = 3    # maker/hybrid: poll cycles to wait for a through-trade

    def leg_is_yes_buy(self, opening: bool, direction: int) -> bool:
        """Is this leg buying YES (lifting) vs selling YES (hitting)?"""
        return (opening and direction > 0) or (not opening and direction < 0)


@dataclass
class FillResult:
    fill_yes: float      # YES-frame fill price
    is_maker: bool       # True -> maker fee, False -> taker fee


def _clamp(p: float) -> float:
    return min(0.99, max(0.01, p))


def resolve_fill(cfg: ExecConfig, yes_buy: bool, decision_yes: float,
                 forward_prices: np.ndarray) -> Optional[FillResult]:
    """Resolve one leg's fill under `cfg`.

    Parameters
    ----------
    yes_buy        : True if the leg buys YES (lifts ask), False if it sells YES.
    decision_yes   : last-trade YES price at decision time (our 'mid').
    forward_prices : YES-frame tick prices in the look-ahead window (for maker
                     through-trade detection). May be empty.

    Returns a FillResult, or None if a passive order never filled (the caller
    treats that as "no fill" — the position is not opened / stays open).
    """
    hs = cfg.half_spread

    if cfg.mode == "mid":
        # idealised: cross at the last trade, no spread; book as maker (best case)
        return FillResult(_clamp(decision_yes), True)

    if cfg.mode == "taker":
        adj = hs + cfg.slippage
        fill = decision_yes + adj if yes_buy else decision_yes - adj
        return FillResult(_clamp(fill), False)

    if cfg.mode in ("maker", "maker_then_taker"):
        if yes_buy:
            level = decision_yes - hs                       # post a bid below mid
            filled = forward_prices.size > 0 and float(forward_prices.min()) <= level
        else:
            level = decision_yes + hs                       # post an ask above mid
            filled = forward_prices.size > 0 and float(forward_prices.max()) >= level
        if filled:
            return FillResult(_clamp(level), True)          # rests at the posted limit
        if cfg.mode == "maker_then_taker":
            cross = float(forward_prices[-1]) if forward_prices.size > 0 else decision_yes
            adj = hs + cfg.slippage
            fill = cross + adj if yes_buy else cross - adj
            return FillResult(_clamp(fill), False)
        return None                                         # pure maker: missed

    raise ValueError(f"unknown exec mode: {cfg.mode!r}")


# ── the standard scenario set used in reports ─────────────────────────────────
def default_scenarios() -> list[ExecConfig]:
    return [
        ExecConfig("ideal_mid",        mode="mid",              half_spread=0.0),
        ExecConfig("maker_rest",       mode="maker",            half_spread=0.01),
        ExecConfig("maker_then_taker", mode="maker_then_taker", half_spread=0.01),
        ExecConfig("taker",            mode="taker",            half_spread=0.01),
        ExecConfig("taker_slip2c",     mode="taker",            half_spread=0.01, slippage=0.02),
    ]
