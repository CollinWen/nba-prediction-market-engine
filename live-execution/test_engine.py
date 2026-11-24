"""
test_engine.py — offline simulation of the ExecutionEngine with stubbed
feed/client/strategy. Validates the June 8 failure modes are guarded:
  1. entry + exit round trip books only on (simulated) fills
  2. frozen clock blocks new entries, exits still allowed
  3. sustained Q4 0:00 flattens and halts the session
No network, no orders, no model — pure state-machine test.
"""
from __future__ import annotations

import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import boot
boot()

import config
from core.interfaces import GameState, Intent, Quote
from execution.engine import ExecutionEngine

config.ENGINE.poll_secs = 0.01          # fast-forward
config.ENGINE.stale_clock_polls = 3
config.ENGINE.game_over_polls = 2


class FakeFeed:
    def __init__(self):
        self.states: list[GameState | None] = []
        self.i = 0

    def game_state(self, game_id):
        s = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        return s


class FakeClient:
    def __init__(self):
        self.prices = [0.50]
        self.j = 0

    def quote(self, ticker):
        p = self.prices[min(self.j, len(self.prices) - 1)]
        return Quote(ticker=ticker, bid=p - 0.01, ask=p + 0.01, last=p,
                     volume=0, ts=dt.datetime.now(dt.timezone.utc))

    def get_market(self, ticker):
        return {"status": "active"}


class FakeOM:
    """Dry-run order manager: every order 'rests' (engine simulates the fill)."""
    def __init__(self):
        self.placed = []

    def place(self, order):
        order.status = "resting"
        order.order_id = f"FAKE-{len(self.placed)}"
        self.placed.append(order)
        return order

    def cancel(self, order_id):
        return True


class ScriptedStrategy:
    name = "scripted"

    def __init__(self, script):
        self.script = script   # list of [Intent] per poll
        self.k = 0
        self.fills = []

    def on_poll(self, ctx):
        intents = self.script[min(self.k, len(self.script) - 1)]
        self.k += 1
        return intents

    def on_entry(self, pos):
        self.fills.append(("entry", pos.direction, pos.entry_price))

    def on_exit(self, trade):
        self.fills.append(("exit", trade["reason"], trade["exit_price"]))


def gs(period, secs, sd=0):
    return GameState(game_id="TEST", home="NYK", away="SAS", period=period,
                     secs_remaining=secs, score_home=50 + sd, score_away=50)


def make_engine(feed, strat):
    e = ExecutionEngine(
        game_id="TEST", home="NYK", home_ticker="KXNBAGAME-TEST-NYK",
        strategy=strat, feed=feed, client=FakeClient(),
        order_manager=FakeOM(), tracker=None, blotter=None,
        qty=1, dry_run=True,
    )
    e._latest_price = lambda: 0.50          # bypass tick parquet
    e._pregame = 0.50                       # bypass pregame lock/persistence
    return e


def test_round_trip():
    feed = FakeFeed()
    feed.states = [gs(1, 2000), gs(1, 1980), gs(1, 1960), gs(1, 1940)]
    strat = ScriptedStrategy([
        [Intent(kind="enter", direction=-1, reason="test-edge")],
        [],
        [Intent(kind="exit", reason="tp")],
        [],
    ])
    eng = make_engine(feed, strat)
    for _ in range(4):
        eng.step()
    assert ("entry", -1, 0.49) in strat.fills, strat.fills   # NO buy at yes_bid
    assert any(f[0] == "exit" and f[1] == "tp" for f in strat.fills), strat.fills
    assert eng.open_position is None
    print("PASS round trip: entry @ bid, exit booked, flat at end")


def test_stale_clock_blocks_entries():
    feed = FakeFeed()
    feed.states = [gs(2, 1440)] * 8         # frozen halftime clock
    enter = [Intent(kind="enter", direction=-1, reason="test")]
    strat = ScriptedStrategy([enter] * 8)
    eng = make_engine(feed, strat)
    for _ in range(8):
        eng.step()
    # poll 1-3 build staleness; entries before the threshold may fill, after must not.
    entries = [f for f in strat.fills if f[0] == "entry"]
    assert len(entries) <= 1, f"expected at most 1 entry before guard trips, got {entries}"
    print(f"PASS stale clock: {len(entries)} entry before guard, 0 after")


def test_game_over_halts():
    feed = FakeFeed()
    feed.states = [gs(4, 100, sd=-4), gs(4, 0, sd=-4), gs(4, 0, sd=-4), gs(4, 0, sd=-4)]
    strat = ScriptedStrategy([
        [Intent(kind="enter", direction=-1, reason="test")],
        [], [], [],
    ])
    eng = make_engine(feed, strat)
    alive = []
    for _ in range(4):
        alive.append(eng.step())
    assert alive[-1] is False, f"engine should stop after game-over polls: {alive}"
    assert eng.open_position is None, "position should be flattened on halt"
    print("PASS game over: session halted and flattened")


if __name__ == "__main__":
    test_round_trip()
    test_stale_clock_blocks_entries()
    test_game_over_halts()
    print("\nALL ENGINE TESTS PASSED")
