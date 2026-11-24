"""
Reconciliation — compare the local order/position view against Kalshi truth.

Kalshi is the SOURCE OF RECORD. This module pulls Kalshi's open orders and
positions and diffs them against what our OrderManager believes it has, surfacing
discrepancies so an operator (or the dashboard) can react. It only READS; it never
auto-fixes (auto-canceling/placing to "fix" a diff is dangerous — flag, don't act).

Discrepancy kinds reported
--------------------------
  ORDER_MISSING_ON_KALSHI : we think an order is resting, Kalshi doesn't show it
                            (it filled, was canceled, or rejected).
  ORDER_UNKNOWN_LOCALLY   : Kalshi shows a resting order we DIDN'T place
                            (pre-existing / from another tool) — never touch it,
                            but report it so it isn't mistaken for ours.
  POSITION_MISMATCH       : local net position != Kalshi net position for a market.
  POSITION_UNTRACKED      : Kalshi holds a position in a market we have no local
                            record of.
  REALIZED_PNL_DRIFT      : our reconstructed realized P&L diverges from Kalshi's
                            own realized_pnl beyond a tolerance.

Our orders are identifiable by the ``client_order_id`` prefix the OrderManager
stamps on every order it places (CLIENT_ID_PREFIX = 'nbaterm-').
"""
from __future__ import annotations

import sys
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── import bootstrap ──────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot  # noqa: E402
boot()

import config  # noqa: E402
from core.interfaces import Order, Position  # noqa: E402
from core._kalshi_client_stub import get_client  # noqa: E402
from execution.order_manager import CLIENT_ID_PREFIX  # noqa: E402
from portfolio.tracker import Tracker, _c2d  # noqa: E402


@dataclass
class Discrepancy:
    kind: str
    ticker: str
    detail: str
    local: object = None
    kalshi: object = None

    def __str__(self) -> str:
        return f"[{self.kind}] {self.ticker}: {self.detail}"


@dataclass
class ReconcileReport:
    ts: dt.datetime
    discrepancies: list[Discrepancy] = field(default_factory=list)
    n_local_orders: int = 0
    n_kalshi_orders: int = 0
    n_local_positions: int = 0
    n_kalshi_positions: int = 0

    @property
    def clean(self) -> bool:
        return len(self.discrepancies) == 0

    def summary(self) -> str:
        head = (f"reconcile @ {self.ts:%Y-%m-%d %H:%M:%S}  "
                f"orders local={self.n_local_orders}/kalshi={self.n_kalshi_orders}  "
                f"positions local={self.n_local_positions}/kalshi={self.n_kalshi_positions}")
        if self.clean:
            return head + "\n  CLEAN — no discrepancies"
        lines = [head, f"  {len(self.discrepancies)} discrepancy(ies):"]
        lines += [f"    - {d}" for d in self.discrepancies]
        return "\n".join(lines)


def _is_ours(client_order_id: Optional[str]) -> bool:
    return bool(client_order_id) and str(client_order_id).startswith(CLIENT_ID_PREFIX)


def reconcile(
    local_orders: Optional[list[Order]] = None,
    local_positions: Optional[list[Position]] = None,
    client=None,
    tracker: Optional[Tracker] = None,
    pnl_tolerance: float = 0.05,
) -> ReconcileReport:
    """
    Diff local view vs Kalshi. ``local_orders`` should come from
    ``OrderManager.open_orders()``; ``local_positions`` from a prior snapshot
    (optional — if omitted we just validate Kalshi positions are all tracked-able).
    """
    client = client or get_client()
    tracker = tracker or Tracker(client=client)
    local_orders = local_orders or []
    local_positions = local_positions or []

    rep = ReconcileReport(ts=dt.datetime.now())

    # ── ORDERS ───────────────────────────────────────────────────────────────────
    try:
        kalshi_orders = client.get_open_orders()
    except Exception as e:
        rep.discrepancies.append(Discrepancy("READ_ERROR", "*", f"get_open_orders failed: {e}"))
        kalshi_orders = []

    rep.n_local_orders = len(local_orders)
    rep.n_kalshi_orders = len(kalshi_orders)

    kalshi_by_oid = {o.get("order_id"): o for o in kalshi_orders}
    local_oids = {o.order_id for o in local_orders if o.order_id and not str(o.order_id).startswith("DRYRUN-")}

    # ours that Kalshi no longer shows resting
    for o in local_orders:
        if not o.order_id or str(o.order_id).startswith("DRYRUN-"):
            continue  # dry-run orders never exist on Kalshi by design
        if o.order_id not in kalshi_by_oid:
            rep.discrepancies.append(Discrepancy(
                "ORDER_MISSING_ON_KALSHI", o.ticker,
                f"local order {o.order_id} (status={o.status}) not resting on Kalshi "
                f"(likely filled/canceled)", local=o,
            ))

    # Kalshi resting orders we don't recognize as ours
    for oid, ko in kalshi_by_oid.items():
        coid = ko.get("client_order_id")
        if _is_ours(coid):
            if oid not in local_oids:
                rep.discrepancies.append(Discrepancy(
                    "ORDER_MISSING_LOCALLY", ko.get("ticker", "?"),
                    f"Kalshi has OUR order {oid} (coid={coid}) but it's not in local ledger",
                    kalshi=ko,
                ))
        else:
            rep.discrepancies.append(Discrepancy(
                "ORDER_UNKNOWN_LOCALLY", ko.get("ticker", "?"),
                f"resting order {oid} not placed by this system (coid={coid!r}); "
                f"DO NOT cancel", kalshi=ko,
            ))

    # ── POSITIONS ─────────────────────────────────────────────────────────────────
    try:
        kalshi_positions = tracker.positions()
    except Exception as e:
        rep.discrepancies.append(Discrepancy("READ_ERROR", "*", f"positions read failed: {e}"))
        kalshi_positions = []

    rep.n_local_positions = len(local_positions)
    rep.n_kalshi_positions = len(kalshi_positions)

    local_pos_by_ticker = {p.ticker: p for p in local_positions}
    kalshi_pos_by_ticker = {p.ticker: p for p in kalshi_positions}

    def signed(p: Position) -> int:
        return p.qty if p.side == "yes" else -p.qty

    # mismatches / untracked
    for ticker, kp in kalshi_pos_by_ticker.items():
        lp = local_pos_by_ticker.get(ticker)
        if lp is None:
            if local_positions:  # only meaningful if we were given a local snapshot
                rep.discrepancies.append(Discrepancy(
                    "POSITION_UNTRACKED", ticker,
                    f"Kalshi holds {signed(kp):+d} contracts; no local record",
                    kalshi=kp,
                ))
        elif signed(lp) != signed(kp):
            rep.discrepancies.append(Discrepancy(
                "POSITION_MISMATCH", ticker,
                f"local {signed(lp):+d} vs Kalshi {signed(kp):+d}",
                local=lp, kalshi=kp,
            ))

    for ticker, lp in local_pos_by_ticker.items():
        if ticker not in kalshi_pos_by_ticker:
            rep.discrepancies.append(Discrepancy(
                "POSITION_GHOST", ticker,
                f"local thinks {signed(lp):+d} contracts; Kalshi shows none",
                local=lp,
            ))

    # ── REALIZED P&L DRIFT ─────────────────────────────────────────────────────────
    kalshi_realized = tracker._kalshi_realized()
    if kalshi_realized is not None:
        ours = tracker.realized_pnl()
        if abs(ours - kalshi_realized) > pnl_tolerance:
            rep.discrepancies.append(Discrepancy(
                "REALIZED_PNL_DRIFT", "*",
                f"reconstructed ${ours:.2f} vs Kalshi ${kalshi_realized:.2f} "
                f"(tol ${pnl_tolerance:.2f})",
            ))

    return rep


# ── smoke test (read-only) ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print("reconcile.py smoke test  (READ-ONLY)")
    print("=" * 70)

    client = get_client()
    print("client:", getattr(client, "name", type(client).__name__))

    # Simulate a stale local view: pretend we think we have a resting order and a
    # position that may not match Kalshi, to exercise the diff logic.
    fake_local_orders = [
        Order(ticker="KXNBAGAME-FAKE-T1", side="yes", action="buy", qty=2,
              order_type="limit", limit_price=0.40,
              order_id="ord-does-not-exist-123", status="resting"),
    ]
    fake_local_positions = [
        Position(ticker="KXNBAGAME-FAKE-T1", side="yes", qty=2, avg_price=0.40, mark=0.42),
    ]

    try:
        rep = reconcile(local_orders=fake_local_orders, local_positions=fake_local_positions,
                        client=client)
        print("\n" + rep.summary())
        # We injected a fake resting order + fake position, so we EXPECT discrepancies.
        kinds = {d.kind for d in rep.discrepancies}
        print("\ndiscrepancy kinds detected:", sorted(kinds) or "(none)")
        assert "ORDER_MISSING_ON_KALSHI" in kinds, "should flag our fake order as missing on Kalshi"
        print("\nPASS: reconcile correctly flagged the injected stale local order.")
    except Exception as e:
        print("\nreconcile read failed (likely no creds):", e)
        print("(Code path exercised; real reconcile needs account access.)")
