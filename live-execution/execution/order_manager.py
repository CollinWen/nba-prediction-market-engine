"""
Order manager — implements the ``Executor`` protocol (place / cancel / open_orders).

Responsibilities
----------------
* Run pre-trade RISK checks (execution.risk.check_order) before anything is sent.
* Honour ``config.RISK.dry_run`` (default TRUE): when dry, the intended order is
  LOGGED and returned with status='dry_run' WITHOUT touching Kalshi.
* Default to LIMIT (maker) orders for low fees (config.RISK.default_order_type).
* Assign and track OUR OWN client_order_ids so we only ever cancel orders that this
  system placed (the account may hold unrelated resting orders — never touch them).
* Build the correct ``POST /portfolio/orders`` payload.

Kalshi POST /portfolio/orders payload (cents for prices):
    {
      "ticker":          "<market ticker>",
      "client_order_id": "<our uuid>",        # idempotency + ownership tag
      "side":            "yes" | "no",
      "action":          "buy" | "sell",
      "count":           <int contracts>,
      "type":            "limit" | "market",
      "yes_price":       <int cents>,         # limit only, when side == yes
      "no_price":        <int cents>,         # limit only, when side == no
      "time_in_force":   "fill_or_kill" (optional; omit for resting maker)
    }
Response: {"order": {"order_id", "status", ...}}.

The order_manager calls ``client.post_order(payload)`` / ``client.cancel_order(id)``
on the gateway returned by ``core._kalshi_client_stub.get_client()`` — which is the
sibling's real ``core.kalshi_client`` if present, else a read-only stub that REFUSES
to place real orders. This keeps parallel development safe.
"""
from __future__ import annotations

import sys
import uuid
import logging
import datetime as dt
from pathlib import Path
from typing import Optional

# ── import bootstrap ──────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot  # noqa: E402
boot()

import config  # noqa: E402
from core.interfaces import Order, Position, AccountSummary  # noqa: E402
from core.fees import fee as kalshi_fee  # noqa: E402
from core._kalshi_client_stub import get_client  # noqa: E402
from execution.risk import check_order  # noqa: E402

log = logging.getLogger("order_manager")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

RISK = config.RISK
CLIENT_ID_PREFIX = "nbaterm-"   # tags every order this system owns


def _dollars_to_cents(p: float) -> int:
    """Convert a 0-1 dollar price to an int cent price (1-99), as Kalshi expects."""
    return int(round(float(p) * 100))


def _new_client_order_id() -> str:
    return CLIENT_ID_PREFIX + uuid.uuid4().hex[:16]


def build_payload(order: Order, client_order_id: str) -> dict:
    """
    Build the exact ``POST /portfolio/orders`` body for an Order.

    PRICE CONVENTION: ``Order.limit_price`` is ALWAYS the YES-frame price
    (P(home win) in dollars), regardless of side. For NO orders the payload's
    no_price is the complement: no_price = 100 - yes_cents.

    On June 8 the YES price was written into no_price unconverted — every NO
    buy was bid ~15c below market (rested stale, filled later in adverse
    cascades) and every NO sell crossed ~25c through the market, silently
    accumulating a 47-contract position. Do not change this convention
    without auditing every caller.
    """
    payload = {
        "ticker": order.ticker,
        "client_order_id": client_order_id,
        "side": order.side,
        "action": order.action,
        "count": int(order.qty),
        "type": order.order_type,
    }
    if order.order_type == "limit":
        yes_cents = _dollars_to_cents(order.limit_price)
        if order.side == "yes":
            payload["yes_price"] = max(1, min(99, yes_cents))
        else:
            payload["no_price"] = max(1, min(99, 100 - yes_cents))
    else:
        # market (taker) — fill_or_kill avoids dangling partials; takers cost more.
        payload["time_in_force"] = "fill_or_kill"
    return payload


class OrderManager:
    """Concrete ``Executor``. Owns client_order_id -> Order bookkeeping."""

    def __init__(self, client=None, risk=RISK, portfolio=None):
        self._client = client or get_client()
        self.risk = risk
        # our own ledger of orders we placed, keyed by client_order_id
        self._orders: dict[str, Order] = {}
        # client_order_id -> kalshi order_id once acked
        self._coid_to_oid: dict[str, str] = {}
        # optional Portfolio for live risk context (positions/account)
        self._portfolio = portfolio
        log.info("OrderManager ready  (client=%s, dry_run=%s, default_type=%s)",
                 getattr(self._client, "name", type(self._client).__name__),
                 self.risk.dry_run, self.risk.default_order_type)

    # ── risk context ────────────────────────────────────────────────────────────
    def _risk_context(self) -> tuple[list[Position], Optional[AccountSummary]]:
        if self._portfolio is None:
            return [], None
        try:
            return self._portfolio.positions(), self._portfolio.account()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("could not load risk context: %s", e)
            return [], None

    # ── Executor.place ──────────────────────────────────────────────────────────
    def place(self, order: Order) -> Order:
        """
        Validate, then either log (dry_run) or send the order. Returns the updated
        Order (status + order_id). On rejection, status='rejected' and the reason is
        stuffed into order_id's place via logging (status conveys outcome).
        """
        # default to maker limit if no type given
        if not order.order_type:
            order.order_type = self.risk.default_order_type
        if order.ts is None:
            order.ts = dt.datetime.now()

        positions, account = self._risk_context()

        ok, reason = check_order(order, positions, account, self.risk)
        if not ok:
            order.status = "rejected"
            log.warning("REJECTED %s %s %dx %s @%s : %s",
                        order.action, order.side, order.qty, order.ticker,
                        order.limit_price, reason)
            return order

        coid = _new_client_order_id()
        # stash our client_order_id on the Order (dynamic attr; not in the dataclass
        # schema but useful for ownership tracking / reconcile)
        order.client_order_id = coid
        self._orders[coid] = order

        est_fee = kalshi_fee(
            order.qty,
            order.limit_price if order.limit_price is not None else 0.5,
            maker=(order.order_type == "limit"),
            nba=True,
        )

        # ── DRY RUN: log intent, do NOT send ─────────────────────────────────────
        if self.risk.dry_run:
            order.status = "dry_run"
            order.order_id = "DRYRUN-" + coid
            self._coid_to_oid[coid] = order.order_id
            log.info(
                "DRY_RUN order (NOT sent): %s %s %dx %s @%s [%s]  coid=%s  est_fee=$%.2f",
                order.action, order.side, order.qty, order.ticker,
                order.limit_price, order.order_type, coid, est_fee,
            )
            log.info("DRY_RUN payload: %s", build_payload(order, coid))
            return order

        # ── LIVE: send to Kalshi ─────────────────────────────────────────────────
        payload = build_payload(order, coid)
        log.info("SENDING order: %s", payload)
        try:
            resp = self._client.post_order(payload)
        except Exception as e:
            order.status = "rejected"
            log.error("post_order failed: %s", e)
            return order

        ack = resp.get("order", resp) if isinstance(resp, dict) else {}
        order.order_id = ack.get("order_id")
        order.status = ack.get("status", "resting") or "resting"
        if order.order_id:
            self._coid_to_oid[coid] = order.order_id
        log.info("ACK order_id=%s status=%s", order.order_id, order.status)
        return order

    # ── Executor.cancel ──────────────────────────────────────────────────────────
    def cancel(self, order_id: str) -> bool:
        """
        Cancel an order BY KALSHI order_id, but ONLY if this system placed it
        (i.e. we have a matching client_order_id in our ledger). Refuses to cancel
        unknown / unrelated orders so we never disturb pre-existing resting orders.
        """
        owned = order_id in self._coid_to_oid.values() or order_id in self._orders
        if not owned:
            log.warning("REFUSING to cancel %s : not placed by this system", order_id)
            return False

        if self.risk.dry_run:
            log.info("DRY_RUN cancel (NOT sent): %s", order_id)
            self._mark_canceled(order_id)
            return True

        try:
            self._client.cancel_order(order_id)
        except Exception as e:
            log.error("cancel_order failed for %s: %s", order_id, e)
            return False
        self._mark_canceled(order_id)
        log.info("CANCELED %s", order_id)
        return True

    def _mark_canceled(self, order_id: str) -> None:
        for coid, oid in self._coid_to_oid.items():
            if oid == order_id and coid in self._orders:
                self._orders[coid].status = "canceled"

    # ── Executor.open_orders ──────────────────────────────────────────────────────
    def open_orders(self) -> list[Order]:
        """
        Orders this system placed that are still live (resting/pending/dry_run).
        Does NOT include unrelated resting orders on the account.
        """
        live = {"pending", "resting", "dry_run"}
        return [o for o in self._orders.values() if o.status in live]


# ── smoke test (must run in dry_run; never sends a real order) ───────────────────
if __name__ == "__main__":
    print("=" * 70)
    print("order_manager.py smoke test")
    print("dry_run =", RISK.dry_run, " (MUST be True for this test)")
    print("=" * 70)

    assert RISK.dry_run, "Refusing to run smoke test with dry_run=False (real money!)"

    om = OrderManager()  # uses stub client; will not place real orders in dry_run

    # 1. a valid small order -> should be LOGGED as dry_run, NOT sent
    good = Order(ticker="KXNBAGAME-26JUN05-T1", side="yes", action="buy",
                 qty=3, order_type="limit", limit_price=0.40)
    res = om.place(good)
    print(f"\n[1] valid order  -> status={res.status!r}  order_id={res.order_id!r}")
    assert res.status == "dry_run", "valid order should be dry_run, not sent"
    assert res.order_id and res.order_id.startswith("DRYRUN-")

    # 2. an OVERSIZED order -> should be REJECTED by risk (cap 5), never reaching send
    big = Order(ticker="KXNBAGAME-26JUN05-T1", side="yes", action="buy",
                qty=50, order_type="limit", limit_price=0.40)
    res2 = om.place(big)
    print(f"[2] oversized 50  -> status={res2.status!r}  (expect 'rejected')")
    assert res2.status == "rejected", "oversized order must be rejected by risk"

    # 3. open_orders reflects only the dry_run order we placed
    oo = om.open_orders()
    print(f"[3] open_orders   -> {len(oo)} live order(s) tracked")
    assert len(oo) == 1

    # 4. cancel an unrelated order id -> must REFUSE
    refused = om.cancel("SOME-UNRELATED-ORDER-ID")
    print(f"[4] cancel foreign-> returned {refused} (expect False; refuses unowned)")
    assert refused is False

    # 5. cancel our own dry_run order -> allowed (dry, just marks canceled)
    ok = om.cancel(res.order_id)
    print(f"[5] cancel own    -> returned {ok} (expect True)")
    assert ok is True

    # 6. show the payload that WOULD be sent (for schema review)
    print("\n[6] example payload that would POST /portfolio/orders:")
    print("   ", build_payload(good, "nbaterm-EXAMPLE0000000000"))

    print("\nALL ASSERTIONS PASSED — no real orders were sent (dry_run honored).")
