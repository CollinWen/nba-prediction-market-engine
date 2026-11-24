"""
Shared contracts for the live-execution terminal.
Every subsystem imports its data types and protocols from here so modules stay
decoupled and the dashboard can develop against mocks.

All prices are floats in DOLLARS (0.0-1.0). Normalize at API boundaries.
All quantities are integer contracts.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, Optional
import datetime as dt

# ── Market data ───────────────────────────────────────────────────────────────
@dataclass
class Quote:
    ticker: str
    bid: float            # best yes bid (dollars)
    ask: float            # best yes ask (dollars)
    last: float           # last traded yes price
    volume: float
    ts: dt.datetime
    @property
    def mid(self) -> float: return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last
    @property
    def spread(self) -> float: return (self.ask - self.bid) if self.bid and self.ask else 0.0

@dataclass
class OrderBookLevel:
    price: float          # dollars
    qty: int              # contracts

@dataclass
class OrderBook:
    ticker: str
    yes_bids: list[OrderBookLevel]   # descending price
    yes_asks: list[OrderBookLevel]   # ascending price
    ts: dt.datetime

# ── Game state ────────────────────────────────────────────────────────────────
@dataclass
class GameState:
    game_id: str
    home: str
    away: str
    period: int
    secs_remaining: float       # regulation-seconds remaining (0-2880)
    score_home: int
    score_away: int
    @property
    def score_diff(self) -> int: return self.score_home - self.score_away

# ── Model signal ──────────────────────────────────────────────────────────────
@dataclass
class Signal:
    ticker: str
    ts: dt.datetime
    model_prob_home: float       # model P(home win)
    kalshi_price: float          # home-side price (dollars)
    edge: float                  # model_prob_home - kalshi_price (home side)
    action: str                  # 'enter_yes' | 'enter_no' | 'exit' | 'hold'
    reason: str = ""

# ── Orders & fills ────────────────────────────────────────────────────────────
@dataclass
class Order:
    ticker: str
    side: str                    # 'yes' | 'no'
    action: str                  # 'buy' | 'sell'
    qty: int                     # contracts
    order_type: str              # 'limit' | 'market'
    limit_price: Optional[float] = None   # dollars, required for limit
    order_id: Optional[str] = None        # set by Kalshi on ack
    status: str = "pending"               # pending|resting|filled|canceled|rejected
    ts: Optional[dt.datetime] = None

@dataclass
class Fill:
    ticker: str
    side: str
    action: str
    qty: int
    price: float                 # dollars
    fee: float                   # dollars
    order_id: str
    fill_id: str
    ts: dt.datetime

@dataclass
class Position:
    ticker: str
    side: str                    # net side: 'yes' | 'no'
    qty: int                     # net contracts (positive)
    avg_price: float             # dollars
    mark: float                  # current mark price (dollars)
    @property
    def unrealized(self) -> float:
        # value if marked now minus cost; sign by side
        return (self.mark - self.avg_price) * self.qty if self.side == "yes" else (self.avg_price - self.mark) * self.qty

@dataclass
class AccountSummary:
    balance_usd: float
    total_exposure_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    day_pnl_usd: float
    kill_switch: bool

# ── Strategy plug-in contracts ────────────────────────────────────────────────
# The ExecutionEngine (execution/engine.py) owns the poll loop, market/game
# feeds, guards, order routing, fills, risk, and the blotter. A Strategy is a
# pure decision layer: it receives a StrategyContext snapshot each poll and
# returns Intents. It never touches orders or the exchange directly — that is
# what makes strategies plug-and-play across live and backtest.
#
# All prices in strategy-land are YES-frame (P(home win), dollars 0-1).

@dataclass
class PositionState:
    """Engine-owned record of the confirmed open position (from real fills)."""
    direction: int               # +1 long YES, -1 long NO
    qty: int
    entry_price: float           # YES-frame avg fill price
    entry_ts: float              # epoch seconds
    entry_period: int = 0
    entry_secs: float = 0.0
    meta: dict = field(default_factory=dict)   # strategy scratch (model prob, edge...)

@dataclass
class Intent:
    """A strategy's request to the engine. The engine sizes, prices, and routes it."""
    kind: str                    # 'enter' | 'exit'
    direction: int = 0           # enter only: +1 YES, -1 NO
    qty: Optional[int] = None    # None -> engine decides (sizing hook)
    reason: str = ""
    meta: dict = field(default_factory=dict)

@dataclass
class StrategyContext:
    """Read-only world snapshot handed to Strategy.on_poll each cycle."""
    ts: float                            # epoch seconds
    yes_price: float                     # last traded YES price (tick stream)
    quote: Optional[Quote]               # REST bid/ask, may be None
    game_state: Optional[GameState]
    position: Optional[PositionState]    # None = flat (confirmed-fill truth)
    pregame_prob: float
    entries_blocked: bool = False        # guards: stale clock etc.
    block_reason: str = ""

class Strategy(Protocol):
    """Implemented by each pluggable strategy (e.g. strategy/model_edge.py)."""
    name: str
    def on_poll(self, ctx: StrategyContext) -> list[Intent]: ...
    def on_entry(self, position: PositionState) -> None: ...
    def on_exit(self, trade: dict) -> None: ...


# ── Protocols (implemented by subsystems) ─────────────────────────────────────
class MarketData(Protocol):
    def quote(self, ticker: str) -> Quote: ...
    def orderbook(self, ticker: str, depth: int = 10) -> OrderBook: ...
    def list_nba_markets(self, status: str = "open") -> list[str]: ...

class GameFeed(Protocol):
    def todays_games(self) -> list[dict]: ...                 # [{game_id, home, away, status}]
    def game_state(self, game_id: str) -> Optional[GameState]: ...

class SignalEngine(Protocol):
    def model_prob_home(self, game_state: GameState, pregame_prob: float) -> float: ...
    def signal(self, ticker: str, game_state: GameState, quote: Quote, pregame_prob: float) -> Signal: ...

class Executor(Protocol):
    def place(self, order: Order) -> Order: ...               # returns updated Order (order_id, status)
    def cancel(self, order_id: str) -> bool: ...
    def open_orders(self) -> list[Order]: ...

class Portfolio(Protocol):
    def positions(self) -> list[Position]: ...
    def fills(self, since: Optional[dt.datetime] = None) -> list[Fill]: ...
    def account(self) -> AccountSummary: ...
