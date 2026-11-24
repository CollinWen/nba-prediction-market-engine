"""Offline unit tests for the configurable execution model. No network."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.exec_model import ExecConfig, resolve_fill


def test_taker_crosses_and_pays_spread():
    cfg = ExecConfig("t", mode="taker", half_spread=0.01)
    # buying YES lifts the ask: fill ABOVE the last trade
    f = resolve_fill(cfg, yes_buy=True, decision_yes=0.50, forward_prices=np.array([]))
    assert f is not None and abs(f.fill_yes - 0.51) < 1e-9 and not f.is_maker
    # selling YES hits the bid: fill BELOW
    f = resolve_fill(cfg, yes_buy=False, decision_yes=0.50, forward_prices=np.array([]))
    assert abs(f.fill_yes - 0.49) < 1e-9
    print("PASS taker crosses the spread, always fills")


def test_taker_slippage_is_adverse():
    cfg = ExecConfig("t", mode="taker", half_spread=0.01, slippage=0.02)
    f = resolve_fill(cfg, yes_buy=True, decision_yes=0.50, forward_prices=np.array([]))
    assert abs(f.fill_yes - 0.53) < 1e-9   # +spread +slip
    print("PASS taker slippage adds adverse cost")


def test_maker_fills_only_on_through_trade():
    cfg = ExecConfig("m", mode="maker", half_spread=0.01, maker_wait_polls=3)
    # buying YES posts a bid at 0.49; tape dips to 0.48 -> fills at 0.49 (maker)
    f = resolve_fill(cfg, yes_buy=True, decision_yes=0.50,
                     forward_prices=np.array([0.50, 0.48, 0.50]))
    assert f is not None and abs(f.fill_yes - 0.49) < 1e-9 and f.is_maker
    # tape never trades down to 0.49 -> NO FILL (adverse selection: the move
    # went the other way, exactly the trade we wanted, and we missed it)
    f = resolve_fill(cfg, yes_buy=True, decision_yes=0.50,
                     forward_prices=np.array([0.51, 0.52, 0.55]))
    assert f is None
    print("PASS maker fills only when tape trades through; misses otherwise")


def test_hybrid_falls_back_to_cross():
    cfg = ExecConfig("h", mode="maker_then_taker", half_spread=0.01)
    # no through-trade -> cross at the last forward price (0.55) + spread
    f = resolve_fill(cfg, yes_buy=True, decision_yes=0.50,
                     forward_prices=np.array([0.52, 0.54, 0.55]))
    assert f is not None and not f.is_maker and abs(f.fill_yes - 0.56) < 1e-9
    print("PASS hybrid crosses at the post-wait price when maker misses")


def test_mid_is_frictionless():
    cfg = ExecConfig("i", mode="mid", half_spread=0.0)
    f = resolve_fill(cfg, yes_buy=True, decision_yes=0.50, forward_prices=np.array([]))
    assert abs(f.fill_yes - 0.50) < 1e-9 and f.is_maker
    print("PASS mid fills at last trade, no spread")


if __name__ == "__main__":
    test_taker_crosses_and_pays_spread()
    test_taker_slippage_is_adverse()
    test_maker_fills_only_on_through_trade()
    test_hybrid_falls_back_to_cross()
    test_mid_is_frictionless()
    print("\nALL EXEC MODEL TESTS PASSED")
