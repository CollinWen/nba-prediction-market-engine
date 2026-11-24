"""
Mock data providers for the dashboard.

These implement every Protocol in core.interfaces (MarketData, GameFeed,
SignalEngine, Executor, Portfolio) with realistic, slowly-evolving fake NBA
data so the terminal can be built and demoed before the real providers
(kalshi_client, nba_feed, model_signal, order_manager, tracker) land.

Everything is in DOLLARS (0.0-1.0) and integer contracts, matching the
interfaces contract. Prices wander a little on each call (driven by wall-clock
time) so the live-refresh UI visibly updates.

Swap these for the real providers in run_terminal.py.
"""
from __future__ import annotations

from _bootstrap import boot
boot()

import datetime as dt
import math
import random
import threading
from typing import Optional

from core.interfaces import (
    AccountSummary,
    Fill,
    GameState,
    Order,
    OrderBook,
    OrderBookLevel,
    Position,
    Quote,
    Signal,
)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _clamp01(x: float) -> float:
    return max(0.01, min(0.99, x))


# ── Static universe of tracked NBA contracts ──────────────────────────────────
# ticker -> (home, away, game_id, pregame P(home win))
_GAMES = {
    "KXNBAGAME-26JUN05BOSNYK-BOS": ("BOS", "NYK", "0042500301", 0.62),
    "KXNBAGAME-26JUN05DENLAL-DEN": ("DEN", "LAL", "0042500311", 0.55),
    "KXNBAGAME-26JUN05MIAMIL-MIA": ("MIA", "MIL", "0042500321", 0.48),
}

# A reference epoch so the simulated trajectories are deterministic-ish per run
_T0 = dt.datetime.now()


def _elapsed_min() -> float:
    return (dt.datetime.now() - _T0).total_seconds() / 60.0


class MockMarketData:
    """Implements core.interfaces.MarketData."""

    def __init__(self) -> None:
        self._rng = random.Random(7)

    # -- internal: a smooth-ish "true" yes price for a ticker right now --------
    def _true_price(self, ticker: str) -> float:
        _, _, _, pregame = _GAMES.get(ticker, ("", "", "", 0.5))
        # drift the price with a slow sine + small noise so the UI moves
        t = _elapsed_min()
        drift = 0.10 * math.sin(0.6 * t + hash(ticker) % 7)
        noise = self._rng.uniform(-0.01, 0.01)
        return _clamp01(pregame + drift + noise)

    def quote(self, ticker: str) -> Quote:
        mid = self._true_price(ticker)
        half_spread = 0.01
        bid = _clamp01(mid - half_spread)
        ask = _clamp01(mid + half_spread)
        last = _clamp01(mid + self._rng.uniform(-0.005, 0.005))
        vol = 1200 + 400 * math.sin(0.3 * _elapsed_min() + hash(ticker) % 5) + self._rng.uniform(0, 50)
        return Quote(ticker=ticker, bid=round(bid, 2), ask=round(ask, 2),
                     last=round(last, 2), volume=round(abs(vol)), ts=_now())

    def orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        q = self.quote(ticker)
        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []
        for i in range(depth):
            bp = round(_clamp01(q.bid - 0.01 * i), 2)
            ap = round(_clamp01(q.ask + 0.01 * i), 2)
            # more size near the top of book, decaying outward, plus jitter
            bq = int(max(1, (depth - i) * 12 + self._rng.randint(-8, 20)))
            aq = int(max(1, (depth - i) * 12 + self._rng.randint(-8, 20)))
            bids.append(OrderBookLevel(price=bp, qty=bq))
            asks.append(OrderBookLevel(price=ap, qty=aq))
        return OrderBook(ticker=ticker, yes_bids=bids, yes_asks=asks, ts=_now())

    def list_nba_markets(self, status: str = "open") -> list[str]:
        return list(_GAMES.keys())


class MockGameFeed:
    """Implements core.interfaces.GameFeed."""

    def todays_games(self) -> list[dict]:
        out = []
        for ticker, (home, away, gid, _) in _GAMES.items():
            out.append({"game_id": gid, "home": home, "away": away,
                        "status": "live", "ticker": ticker})
        return out

    def _game_by_id(self, game_id: str):
        for ticker, (home, away, gid, pregame) in _GAMES.items():
            if gid == game_id:
                return ticker, home, away, gid, pregame
        return None

    def game_state(self, game_id: str) -> Optional[GameState]:
        info = self._game_by_id(game_id)
        if info is None:
            return None
        ticker, home, away, gid, pregame = info
        t = _elapsed_min()
        # simulate game clock: 2880 regulation seconds, ticking down with elapsed time
        secs_remaining = max(0.0, 2880.0 - (t * 60.0) % 2880.0)
        elapsed_frac = 1.0 - secs_remaining / 2880.0
        period = min(4, int(elapsed_frac * 4) + 1)
        # score grows over the game; lead tracks the pregame favourite + wobble
        total_pts = int(elapsed_frac * 220)
        lead = int((pregame - 0.5) * 30 + 8 * math.sin(1.1 * t + gid.__hash__() % 5))
        score_home = max(0, total_pts // 2 + lead // 2)
        score_away = max(0, total_pts // 2 - lead // 2)
        return GameState(game_id=gid, home=home, away=away, period=period,
                         secs_remaining=secs_remaining,
                         score_home=score_home, score_away=score_away)


class MockSignalEngine:
    """Implements core.interfaces.SignalEngine (model stand-in)."""

    def __init__(self) -> None:
        self._rng = random.Random(11)

    def model_prob_home(self, game_state: GameState, pregame_prob: float) -> float:
        # blend pregame prior with live score-diff & time-remaining, logistic-ish
        diff = game_state.score_diff
        secs = max(1.0, game_state.secs_remaining)
        # score matters more as time runs out
        urgency = (2880.0 - secs) / 2880.0
        live_signal = diff * (0.02 + 0.06 * urgency)
        logit = math.log(pregame_prob / (1 - pregame_prob)) + live_signal
        p = 1.0 / (1.0 + math.exp(-logit))
        return _clamp01(p)

    def signal(self, ticker: str, game_state: GameState, quote: Quote,
               pregame_prob: float) -> Signal:
        prob = self.model_prob_home(game_state, pregame_prob)
        kalshi_price = quote.mid
        edge = prob - kalshi_price
        if edge > 0.04:
            action = "enter_yes"
            reason = f"model {prob:.0%} > mkt {kalshi_price:.0%}, edge +{edge:.0%}"
        elif edge < -0.04:
            action = "enter_no"
            reason = f"model {prob:.0%} < mkt {kalshi_price:.0%}, edge {edge:.0%}"
        elif abs(edge) < 0.01:
            action = "exit"
            reason = "edge closed, flatten"
        else:
            action = "hold"
            reason = "edge below threshold"
        return Signal(ticker=ticker, ts=_now(), model_prob_home=prob,
                      kalshi_price=kalshi_price, edge=edge,
                      action=action, reason=reason)


class MockExecutor:
    """Implements core.interfaces.Executor.

    Honours config.RISK.dry_run: when True (the default), placed orders are
    logged and recorded but never 'sent' (status stays 'dry_run'). Keeps an
    in-memory list of open orders so the UI has something to show/cancel.
    """

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self._orders: list[Order] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.order_log: list[str] = []
        # a couple of pre-existing resting orders so the panel isn't empty
        self._orders.append(Order(
            ticker="KXNBAGAME-26JUN05BOSNYK-BOS", side="yes", action="buy",
            qty=3, order_type="limit", limit_price=0.60,
            order_id="MOCK-RESTING-1", status="resting", ts=_now()))

    def place(self, order: Order) -> Order:
        with self._lock:
            self._seq += 1
            order.order_id = f"MOCK-{self._seq:04d}"
            order.ts = _now()
            if self.dry_run:
                order.status = "dry_run"
                msg = (f"[DRY_RUN] would place {order.action} {order.qty} "
                       f"{order.side} {order.ticker} "
                       f"@ {order.limit_price} ({order.order_type})")
            else:
                order.status = "resting" if order.order_type == "limit" else "filled"
                msg = (f"[LIVE] placed {order.order_id}: {order.action} "
                       f"{order.qty} {order.side} {order.ticker} "
                       f"@ {order.limit_price} ({order.order_type})")
            self.order_log.append(f"{order.ts:%H:%M:%S}  {msg}")
            # only resting/live limit orders are 'open'
            if order.status in ("resting", "dry_run"):
                self._orders.append(order)
            return order

    def cancel(self, order_id: str) -> bool:
        with self._lock:
            for o in self._orders:
                if o.order_id == order_id and o.status in ("resting", "dry_run"):
                    o.status = "canceled"
                    self.order_log.append(
                        f"{_now():%H:%M:%S}  canceled {order_id}")
                    return True
            return False

    def open_orders(self) -> list[Order]:
        with self._lock:
            return [o for o in self._orders if o.status in ("resting", "dry_run")]


class MockPortfolio:
    """Implements core.interfaces.Portfolio."""

    def __init__(self, market: MockMarketData) -> None:
        self._market = market
        self._rng = random.Random(13)
        # static open positions; mark refreshed from the market on each call
        self._positions = [
            Position(ticker="KXNBAGAME-26JUN05BOSNYK-BOS", side="yes", qty=5,
                     avg_price=0.58, mark=0.62),
            Position(ticker="KXNBAGAME-26JUN05DENLAL-DEN", side="no", qty=4,
                     avg_price=0.47, mark=0.45),
        ]
        # historical fills (some closed round-trips -> realized P&L in blotter)
        self._fills = self._seed_fills()

    def _seed_fills(self) -> list[Fill]:
        base = _now() - dt.timedelta(minutes=40)
        f = []
        f.append(Fill(ticker="KXNBAGAME-26JUN05BOSNYK-BOS", side="yes",
                      action="buy", qty=5, price=0.58, fee=0.04,
                      order_id="MOCK-H1", fill_id="F1",
                      ts=base + dt.timedelta(minutes=1)))
        f.append(Fill(ticker="KXNBAGAME-26JUN05DENLAL-DEN", side="no",
                      action="buy", qty=4, price=0.47, fee=0.03,
                      order_id="MOCK-H2", fill_id="F2",
                      ts=base + dt.timedelta(minutes=8)))
        # a closed round trip on MIA: bought 3 @ .50, sold 3 @ .57
        f.append(Fill(ticker="KXNBAGAME-26JUN05MIAMIL-MIA", side="yes",
                      action="buy", qty=3, price=0.50, fee=0.02,
                      order_id="MOCK-H3", fill_id="F3",
                      ts=base + dt.timedelta(minutes=15)))
        f.append(Fill(ticker="KXNBAGAME-26JUN05MIAMIL-MIA", side="yes",
                      action="sell", qty=3, price=0.57, fee=0.03,
                      order_id="MOCK-H4", fill_id="F4",
                      ts=base + dt.timedelta(minutes=25)))
        return f

    def positions(self) -> list[Position]:
        # refresh marks from the live mock market
        out = []
        for p in self._positions:
            q = self._market.quote(p.ticker)
            out.append(Position(ticker=p.ticker, side=p.side, qty=p.qty,
                                avg_price=p.avg_price, mark=round(q.mid, 2)))
        return out

    def fills(self, since: Optional[dt.datetime] = None) -> list[Fill]:
        if since is None:
            return list(self._fills)
        return [f for f in self._fills if f.ts >= since]

    def account(self) -> AccountSummary:
        positions = self.positions()
        unrealized = sum(p.unrealized for p in positions)
        exposure = sum(p.avg_price * p.qty for p in positions)
        # realized from the closed MIA round trip: (0.57-0.50)*3 - fees
        realized = (0.57 - 0.50) * 3 - 0.02 - 0.03
        day_pnl = realized + unrealized
        return AccountSummary(
            balance_usd=44.12,
            total_exposure_usd=round(exposure, 2),
            realized_pnl_usd=round(realized, 2),
            unrealized_pnl_usd=round(unrealized, 2),
            day_pnl_usd=round(day_pnl, 2),
            kill_switch=False,
        )


# ── Convenience bundle ────────────────────────────────────────────────────────
class MockProviders:
    """Holds one instance of every provider, wired together."""

    def __init__(self, dry_run: bool = True) -> None:
        self.market = MockMarketData()
        self.games = MockGameFeed()
        self.signals = MockSignalEngine()
        self.executor = MockExecutor(dry_run=dry_run)
        self.portfolio = MockPortfolio(self.market)

    # map a ticker back to its game_id + pregame prob (UI needs this glue)
    @staticmethod
    def game_for_ticker(ticker: str):
        if ticker in _GAMES:
            home, away, gid, pregame = _GAMES[ticker]
            return gid, home, away, pregame
        return None


if __name__ == "__main__":
    # smoke test
    p = MockProviders()
    tk = p.market.list_nba_markets()[0]
    print("markets:", p.market.list_nba_markets())
    print("quote  :", p.market.quote(tk))
    ob = p.market.orderbook(tk, depth=5)
    print("book   :", len(ob.yes_bids), "bids", len(ob.yes_asks), "asks")
    print("games  :", p.games.todays_games())
    gid, home, away, pre = p.game_for_ticker(tk)
    gs = p.games.game_state(gid)
    print("state  :", gs)
    print("signal :", p.signals.signal(tk, gs, p.market.quote(tk), pre))
    print("posns  :", p.portfolio.positions())
    print("fills  :", len(p.portfolio.fills()))
    print("account:", p.portfolio.account())
    o = p.executor.place(Order(ticker=tk, side="yes", action="buy", qty=2,
                               order_type="limit", limit_price=0.55))
    print("placed :", o)
    print("open   :", p.executor.open_orders())
    print("log    :", p.executor.order_log)
