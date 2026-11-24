"""
Standalone fallback for ``core.kalshi_client`` (built by a sibling agent).

This lets execution/portfolio modules be developed and smoke-tested in parallel,
before the real gateway lands. It implements the SAME read methods against the
repo-root ``kalshi_auth.get()`` so tracker.py / reconcile.py can read REAL account
data even with the stub. WRITE methods (post_order/cancel_order) raise unless the
real client is present, because the stub must NEVER place real orders.

EXPECTED kalshi_client INTERFACE (the contract order_manager / tracker / reconcile
depend on). The sibling's real client must expose these:

    # ── reads (return raw Kalshi JSON dicts, prices in CENTS where Kalshi uses cents) ──
    get_balance() -> dict
        {"balance": <cents int>, ...}                         GET /portfolio/balance

    get_positions() -> list[dict]
        market_positions: [{"ticker", "position" (signed contracts, + = long YES,
        - = long NO), "market_exposure" (cents), "total_traded" (cents),
        "realized_pnl" (cents), "resting_orders_count", ...}, ...]
        GET /portfolio/positions  (returns the "market_positions" list)

    get_fills(ticker=None, limit=200) -> list[dict]
        [{"trade_id", "order_id", "ticker", "side" ("yes"/"no"),
          "action" ("buy"/"sell"), "count" (int), "yes_price" (cents),
          "no_price" (cents), "is_taker" (bool), "created_time" (iso str)}, ...]
        GET /portfolio/fills  (returns the "fills" list)

    get_open_orders() -> list[dict]
        [{"order_id", "client_order_id", "ticker", "side", "action",
          "type" ("limit"/"market"), "yes_price" (cents), "no_price" (cents),
          "remaining_count", "status", "created_time"}, ...]
        GET /portfolio/orders?status=resting  (returns the "orders" list)

    get_market(ticker) -> dict
        {"ticker", "yes_bid" (cents), "yes_ask" (cents), "last_price" (cents),
         "volume", ...}                                       GET /markets/{ticker}

    # ── writes (order_manager calls these only when dry_run is False) ──
    post_order(payload: dict) -> dict
        POST /portfolio/orders with the exact body order_manager builds:
        {"ticker", "client_order_id", "side", "action", "count",
         "type", "yes_price"|"no_price" (cents, limit only),
         "time_in_force" (optional)} -> returns {"order": {...}}

    cancel_order(order_id: str) -> dict
        DELETE /portfolio/orders/{order_id} -> returns {"order": {...}, "reduced_by": n}

All price-unit normalization (cents->dollars) happens in the CONSUMERS
(tracker/order_manager), not here, so the contract mirrors raw Kalshi JSON.
"""
from __future__ import annotations

import sys
from pathlib import Path

# repo root on path for kalshi_auth
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _StubKalshiClient:
    """Read-only stub. Reads real account data; refuses to write."""

    name = "STUB"

    def __init__(self):
        # import lazily so the module imports even without creds set
        from kalshi_auth import get  # noqa
        self._get = get

    # ── reads ─────────────────────────────────────────────────────────────────
    def get_balance(self) -> dict:
        return self._get("/portfolio/balance", authed=True)

    def get_positions(self) -> list[dict]:
        r = self._get("/portfolio/positions", authed=True, params={"limit": 200})
        return r.get("market_positions", []) or []

    def get_fills(self, ticker: str = None, limit: int = 200) -> list[dict]:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        r = self._get("/portfolio/fills", authed=True, params=params)
        return r.get("fills", []) or []

    def get_open_orders(self) -> list[dict]:
        r = self._get("/portfolio/orders", authed=True, params={"status": "resting", "limit": 200})
        return r.get("orders", []) or []

    def get_market(self, ticker: str) -> dict:
        r = self._get(f"/markets/{ticker}", authed=False)
        return r.get("market", r)

    # ── writes (forbidden in the stub) ──────────────────────────────────────────
    def post_order(self, payload: dict) -> dict:
        raise RuntimeError(
            "STUB kalshi_client cannot place real orders. The real "
            "core.kalshi_client (sibling agent) is required to trade."
        )

    def cancel_order(self, order_id: str) -> dict:
        raise RuntimeError(
            "STUB kalshi_client cannot cancel real orders. The real "
            "core.kalshi_client (sibling agent) is required."
        )


def get_client():
    """
    Return the project's Kalshi client: the real one if the sibling's module is
    importable, otherwise the read-only stub. Consumers call this so they work
    in parallel development and seamlessly upgrade when the real client lands.
    """
    try:
        from core import kalshi_client as kc  # type: ignore
    except Exception:
        try:
            import kalshi_client as kc  # type: ignore  (when live-execution is on path)
        except Exception:
            return _StubKalshiClient()

    # The real module may expose a factory or a class; try common shapes.
    for attr in ("get_client", "KalshiClient", "Client", "client"):
        obj = getattr(kc, attr, None)
        if obj is None:
            continue
        try:
            return obj() if callable(obj) else obj
        except Exception:
            continue
    # Fall back to the module itself if it exposes the functions directly.
    if hasattr(kc, "get_balance"):
        return kc
    return _StubKalshiClient()


if __name__ == "__main__":
    c = get_client()
    print("client:", getattr(c, "name", type(c).__name__))
    try:
        print("balance:", c.get_balance())
    except Exception as e:
        print("balance read failed:", e)
