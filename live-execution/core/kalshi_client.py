"""
kalshi_client.py
================
The single Kalshi gateway for the live-execution terminal.

Responsibilities
----------------
* Implements the `MarketData` protocol from `core.interfaces`:
    - quote(ticker)            -> Quote
    - orderbook(ticker, depth) -> OrderBook
    - list_nba_markets(status) -> list[str]
* Thin REST wrappers used by the execution / portfolio subsystems:
    - get_balance()            -> float (dollars)
    - get_positions()          -> list[dict]
    - get_fills(since=None)    -> list[dict]
    - get_open_orders()        -> list[dict]
    - post_order(payload)      -> dict        (ACTUALLY SENDS — guarded; see note)
    - cancel_order(order_id)   -> bool        (ACTUALLY SENDS)

Price-unit normalization (see DESIGN.md "Key API facts")
--------------------------------------------------------
Kalshi's REST surface mixes units. We normalize EVERYTHING crossing this
boundary to float dollars in [0, 1]:
  * /markets fields `yes_bid`, `yes_ask`, `last_price`  -> INTEGER CENTS (0-100)
    (the API also exposes `*_dollars` string variants which we prefer when present)
  * /markets/{t}/orderbook levels                       -> [price_cents, qty]
  * /portfolio/balance.balance                          -> INTEGER CENTS
  * /portfolio/fills price/fee fields                   -> INTEGER CENTS
Order placement (post_order) expects prices in CENTS — callers pass a normalized
Order/limit price in dollars and we convert to cents in the payload builder.

Auth
----
Public reads (markets, orderbook) need NO auth. Account/order endpoints
(/portfolio/*, order placement/cancel) require `authed=True`. We reuse the
repo-root `kalshi_auth` module (RSA-PSS signing) for all REST.
"""
from __future__ import annotations

import sys
import datetime as dt
from pathlib import Path
from typing import Optional

import requests

# ── Import bootstrap (the package dir name has a hyphen; see _bootstrap.py) ─────
# Put live-execution/ + repo root on sys.path so `config`, `core.*`, and
# `kalshi_auth` all resolve no matter the cwd.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))   # .../live-execution
from _bootstrap import boot  # noqa: E402
boot()

from kalshi_auth import get as kalshi_get, auth_headers, BASE_URL  # noqa: E402
from core.interfaces import Quote, OrderBook, OrderBookLevel  # noqa: E402

NBA_SERIES = "KXNBAGAME"


# ── Unit helpers ───────────────────────────────────────────────────────────────
def _cents_to_dollars(v) -> float:
    """Normalize a Kalshi price/amount to float dollars.

    Accepts ints/floats in CENTS (0-100 for prices, arbitrary for balances) or
    strings already in dollars. Returns 0.0 for None/empty.
    """
    if v is None or v == "":
        return 0.0
    f = float(v)
    # Heuristic: values > 1 are cents (prices) or integer cents (balances).
    # Prices in dollars are always <= 1.0; we only call this on known-cent fields,
    # so dividing by 100 is correct.
    return f / 100.0


def _dollars_field(market: dict, dollars_key: str, cents_key: str) -> float:
    """Prefer the API's *_dollars string field; fall back to integer-cents field."""
    if market.get(dollars_key) not in (None, ""):
        return float(market[dollars_key])
    return _cents_to_dollars(market.get(cents_key))


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class KalshiClient:
    """REST gateway implementing the MarketData protocol + account/order wrappers."""

    name = "LIVE"   # distinguishes the real client from _StubKalshiClient

    def __init__(self, series_ticker: str = NBA_SERIES):
        self.series_ticker = series_ticker

    # ── Raw market read (cents units, for consumers that normalize themselves) ──
    def get_market(self, ticker: str) -> dict:
        """Raw /markets/{ticker} dict (prices in CENTS — caller normalizes)."""
        resp = kalshi_get(f"/markets/{ticker}")
        return resp.get("market", resp)

    # ── MarketData protocol ─────────────────────────────────────────────────────
    def quote(self, ticker: str) -> Quote:
        """Best yes bid/ask/last + volume for a single market (public read)."""
        resp = kalshi_get(f"/markets/{ticker}")
        m = resp.get("market", resp)  # endpoint wraps in {"market": {...}}
        bid = _dollars_field(m, "yes_bid_dollars", "yes_bid")
        ask = _dollars_field(m, "yes_ask_dollars", "yes_ask")
        last = _dollars_field(m, "last_price_dollars", "last_price")
        volume = float(m.get("volume") or 0)
        return Quote(ticker=ticker, bid=bid, ask=ask, last=last,
                     volume=volume, ts=_now_utc())

    def orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        """Full depth orderbook (public read).

        Kalshi returns one-sided books keyed "yes" and "no", each a list of
        [price_cents, qty] ascending by price. A NO bid at price p is
        economically a YES *ask* at (100 - p) cents. We fold both sides into the
        YES book so consumers see a unified ladder.
        """
        params = {"depth": depth} if depth else None
        resp = kalshi_get(f"/markets/{ticker}/orderbook", params=params)
        ob = resp.get("orderbook", resp) or {}

        yes_raw = ob.get("yes") or []   # [[price_cents, qty], ...]  YES bids
        no_raw = ob.get("no") or []     # [[price_cents, qty], ...]  NO bids

        # YES bids: someone willing to BUY yes at price_cents.
        # (levels are [price_cents, qty]; tolerate extra trailing fields.)
        yes_bids = [OrderBookLevel(price=_cents_to_dollars(lvl[0]), qty=int(lvl[1]))
                    for lvl in yes_raw]
        # NO bids => YES asks at (100 - price). A NO bid at 40c == YES offered at 60c.
        yes_asks = [OrderBookLevel(price=_cents_to_dollars(100 - int(lvl[0])), qty=int(lvl[1]))
                    for lvl in no_raw]

        yes_bids.sort(key=lambda l: l.price, reverse=True)   # descending
        yes_asks.sort(key=lambda l: l.price)                 # ascending

        if depth:
            yes_bids = yes_bids[:depth]
            yes_asks = yes_asks[:depth]

        return OrderBook(ticker=ticker, yes_bids=yes_bids,
                         yes_asks=yes_asks, ts=_now_utc())

    def list_nba_markets(self, status: str = "open") -> list[str]:
        """Return all NBA game-market tickers for the given status (public read)."""
        tickers: list[str] = []
        cursor = None
        while True:
            params = {"series_ticker": self.series_ticker,
                      "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = kalshi_get("/markets", params=params)
            for m in resp.get("markets", []):
                tickers.append(m["ticker"])
            cursor = resp.get("cursor")
            if not cursor:
                break
        return tickers

    # ── Account reads (authed) ──────────────────────────────────────────────────
    def get_balance(self) -> float:
        """Available cash balance in DOLLARS (API returns cents)."""
        resp = kalshi_get("/portfolio/balance", authed=True)
        return _cents_to_dollars(resp.get("balance"))

    def get_positions(self) -> list[dict]:
        """Open market positions (raw dicts; portfolio layer maps to Position).

        We surface the `market_positions` list. Prices remain in their raw API
        units — the portfolio subsystem owns the typed mapping, but we add
        convenience dollar fields where obvious.
        """
        out: list[dict] = []
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = kalshi_get("/portfolio/positions", params=params, authed=True)
            out.extend(resp.get("market_positions", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return out

    def get_fills(self, since: Optional[dt.datetime] = None) -> list[dict]:
        """Trade fills, newest-first. `since` filters via min_ts (epoch seconds)."""
        out: list[dict] = []
        cursor = None
        while True:
            params: dict = {"limit": 200}
            if since is not None:
                if since.tzinfo is None:
                    since = since.replace(tzinfo=dt.timezone.utc)
                params["min_ts"] = int(since.timestamp())
            if cursor:
                params["cursor"] = cursor
            resp = kalshi_get("/portfolio/fills", params=params, authed=True)
            out.extend(resp.get("fills", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return out

    def get_order(self, order_id: str) -> dict:
        """Single order by id — used to confirm fills before booking positions.

        Returns the raw order dict (status, fill counts). Fill-count fields can
        arrive as integers ('fill_count') or fixed-point strings
        ('fill_count_fp'); use filled_qty() to read them safely.
        """
        resp = kalshi_get(f"/portfolio/orders/{order_id}", authed=True)
        return resp.get("order", resp)

    @staticmethod
    def filled_qty(order_raw: dict) -> int:
        """Contracts filled on a raw order dict, tolerant of both field shapes."""
        for key in ("fill_count", "filled_count"):
            if order_raw.get(key) not in (None, ""):
                return int(order_raw[key])
        for key in ("fill_count_fp", "filled_count_fp"):
            if order_raw.get(key) not in (None, ""):
                try:
                    return int(float(order_raw[key]))
                except (TypeError, ValueError):
                    pass
        # fall back: initial - remaining when both present
        try:
            initial = int(float(order_raw.get("initial_count", 0) or 0))
            remaining = int(float(order_raw.get("remaining_count", 0) or 0))
            if initial:
                return initial - remaining
        except (TypeError, ValueError):
            pass
        return 0

    def get_open_orders(self) -> list[dict]:
        """Resting (open) orders for this account."""
        out: list[dict] = []
        cursor = None
        while True:
            params = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = kalshi_get("/portfolio/orders", params=params, authed=True)
            out.extend(resp.get("orders", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return out

    # ── Order placement / cancel (authed, WRITE) ────────────────────────────────
    @staticmethod
    def build_order_payload(ticker: str, side: str, action: str, qty: int,
                            order_type: str = "limit",
                            limit_price: Optional[float] = None,
                            client_order_id: Optional[str] = None) -> dict:
        """Construct the POST /portfolio/orders body WITHOUT sending it.

        Prices are converted from float dollars -> integer cents here. For a
        limit order we set `yes_price`/`no_price` (cents) on the appropriate side.

        side:   'yes' | 'no'
        action: 'buy' | 'sell'
        type:   'limit' | 'market'
        """
        import uuid
        payload: dict = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": side,
            "action": action,
            "count": int(qty),
            "type": order_type,
        }
        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order requires limit_price (dollars)")
            cents = int(round(limit_price * 100))
            cents = max(1, min(99, cents))   # Kalshi limit prices live in 1..99
            if side == "yes":
                payload["yes_price"] = cents
            else:
                payload["no_price"] = cents
        return payload

    def post_order(self, payload: dict) -> dict:
        """Send a fully-built order payload to Kalshi (LIVE — places real order).

        Callers (execution/order_manager) are responsible for risk checks and
        dry_run gating BEFORE calling this. This method always sends.
        Returns the raw API response (contains the created order with order_id).
        """
        path = "/portfolio/orders"
        url = BASE_URL + path
        hdrs = auth_headers("POST", f"/trade-api/v2{path}")
        hdrs["Content-Type"] = "application/json"
        r = requests.post(url, json=payload, headers=hdrs)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order by id (LIVE). Returns True on 2xx."""
        path = f"/portfolio/orders/{order_id}"
        url = BASE_URL + path
        hdrs = auth_headers("DELETE", f"/trade-api/v2{path}")
        r = requests.delete(url, headers=hdrs)
        if r.status_code in (200, 201, 204):
            return True
        r.raise_for_status()
        return False


# ── Smoke test (read-only against LIVE API; never places orders) ────────────────
if __name__ == "__main__":
    c = KalshiClient()
    print("=" * 64)
    print("KalshiClient smoke test (LIVE, read-only)")
    print("=" * 64)

    # 1. List NBA markets
    print("\n[1] list_nba_markets(status='open')")
    open_mkts = c.list_nba_markets("open")
    print(f"    open NBA markets: {len(open_mkts)}")
    sample = open_mkts[:3]
    for t in sample:
        print(f"      {t}")

    # If nothing open (off-day), fall back to recently settled markets for the
    # quote/orderbook demos so the smoke test still exercises the parsers.
    probe_list = open_mkts
    if not probe_list:
        print("    (no open markets — trying status='settled' for parser demo)")
        probe_list = c.list_nba_markets("settled")[:3]
        sample = probe_list[:1]

    # 2. Quote
    if sample:
        t = sample[0]
        print(f"\n[2] quote({t})")
        q = c.quote(t)
        print(f"    bid={q.bid:.2f} ask={q.ask:.2f} last={q.last:.2f} "
              f"mid={q.mid:.3f} spread={q.spread:.3f} vol={q.volume:.0f}")

        # 3. Orderbook
        print(f"\n[3] orderbook({t}, depth=5)")
        ob = c.orderbook(t, depth=5)
        print(f"    yes_bids (top 5): "
              f"{[(round(l.price,2), l.qty) for l in ob.yes_bids]}")
        print(f"    yes_asks (top 5): "
              f"{[(round(l.price,2), l.qty) for l in ob.yes_asks]}")
    else:
        print("\n[2/3] no markets available to quote — skipping quote/orderbook")

    # 4. Account reads (authed)
    print("\n[4] account reads (authed)")
    try:
        bal = c.get_balance()
        print(f"    balance: ${bal:.2f}")
    except Exception as e:
        print(f"    get_balance failed: {e}")
    try:
        pos = c.get_positions()
        nonzero = [p for p in pos if (p.get('position') or 0) != 0]
        print(f"    positions: {len(pos)} rows ({len(nonzero)} nonzero)")
    except Exception as e:
        print(f"    get_positions failed: {e}")
    try:
        oo = c.get_open_orders()
        print(f"    open orders: {len(oo)}")
    except Exception as e:
        print(f"    get_open_orders failed: {e}")
    try:
        fills = c.get_fills()
        print(f"    fills (all-time): {len(fills)}")
    except Exception as e:
        print(f"    get_fills failed: {e}")

    # 5. Order payload construction (NO send)
    print("\n[5] build_order_payload (constructed, NOT sent)")
    demo = KalshiClient.build_order_payload(
        ticker=sample[0] if sample else "KXNBAGAME-DEMO-XXX",
        side="yes", action="buy", qty=1, order_type="limit", limit_price=0.42)
    print(f"    {demo}")
    print("\n    (post_order / cancel_order intentionally NOT called)")
    print("\nDone.")
