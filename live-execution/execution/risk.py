"""
Pre-trade risk checks for the live-execution terminal.

Every order must pass ``check_order`` BEFORE it is sent to Kalshi. The checks are
enforced against ``config.RISK`` (a ``RiskLimits`` instance). All limits are hard:
if an order would breach any of them, it is rejected with a human-readable reason.

Limits enforced (see DESIGN.md "Risk controls"):
  * kill switch ........... day P&L <= -max_daily_loss_usd halts ALL new entries
  * max_contracts_per_order  per-order contract cap
  * max_position_per_market  net contracts in a single market (post-fill)
  * max_total_exposure_usd   total $ at risk across the book (post-fill)
  * max_daily_loss_usd       feeds the kill switch

Design choices
--------------
* Exposure is measured the conservative Kalshi way: the maximum dollars you can
  LOSE on a contract is its full cost. For a YES buy at price p, worst-case loss
  is p * qty (contract settles at 0). For a NO buy at price p_no, worst-case loss
  is p_no * qty. We treat the cost of opening as the exposure added.
* SELL / closing orders REDUCE risk, so they bypass the position/exposure ceilings
  (but are still blocked by the kill switch only if they would OPEN a new opposite
  position — a pure reduction is always allowed even when halted, so you can flatten).
* This module is pure and import-light so it can be unit-tested with fake objects.

Usage::

    ok, reason = check_order(order, current_positions, account)
    if not ok:
        log.warning("order rejected: %s", reason)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# ── import bootstrap (hyphenated package dir, see _bootstrap.py) ───────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot  # noqa: E402
boot()

import config  # noqa: E402
from core.interfaces import Order, Position, AccountSummary  # noqa: E402

RISK = config.RISK


# ── helpers ───────────────────────────────────────────────────────────────────
def _is_closing(order: Order, positions: list[Position]) -> bool:
    """
    A 'closing' (risk-reducing) order is a SELL of the same side we currently hold,
    or a buy of the opposite side that nets down an existing position. We keep it
    simple and conservative: only a SELL of a side we already hold counts as a pure
    reduction. Everything else is treated as risk-adding for limit purposes.
    """
    if order.action != "sell":
        return False
    for p in positions:
        if p.ticker == order.ticker and p.side == order.side and p.qty > 0:
            return True
    return False


def _net_position_after(order: Order, positions: list[Position]) -> int:
    """
    Net contracts on this market+side after the order fills.

    YES and NO are opposite exposures on the same market. We track the net
    directional position on the order's side. A buy adds, a sell subtracts on the
    same side; a buy of the opposite side offsets.
    """
    # current signed position in 'yes contracts' terms for this market
    net_yes = 0
    for p in positions:
        if p.ticker != order.ticker:
            continue
        signed = p.qty if p.side == "yes" else -p.qty
        net_yes += signed

    delta = order.qty if order.action == "buy" else -order.qty
    if order.side == "no":
        delta = -delta  # buying NO is short-yes; selling NO is long-yes

    net_yes_after = net_yes + delta
    return abs(net_yes_after)


def _order_exposure_usd(order: Order) -> float:
    """
    Worst-case dollars this order puts at risk when it OPENS exposure.

    Order.limit_price is YES-frame (see order_manager.build_payload), so the
    premium actually paid is limit_price for YES buys and (1 - limit_price)
    for NO buys. For market orders with no known price we fall back to the
    worst case (1.0 * qty) so the check stays safe.
    """
    if order.action == "sell":
        return 0.0  # selling collects premium / reduces risk
    if order.limit_price is None:
        return 1.0 * order.qty
    yes_px = float(order.limit_price)
    paid = yes_px if order.side == "yes" else (1.0 - yes_px)
    return paid * order.qty


def _total_exposure_usd(positions: list[Position]) -> float:
    """Current worst-case dollars at risk across all open positions (cost basis)."""
    total = 0.0
    for p in positions:
        # for a long YES, max loss = avg_price * qty; for long NO, = avg_price*qty
        total += float(p.avg_price) * p.qty
    return total


# ── kill switch ───────────────────────────────────────────────────────────────
def kill_switch_active(account: AccountSummary, risk=RISK) -> bool:
    """
    True when the daily loss limit has been hit. Once active, NO new entries are
    allowed (risk-reducing closes are still permitted). We honour an explicit
    ``account.kill_switch`` flag as well, so an operator can trip it manually.
    """
    if getattr(account, "kill_switch", False):
        return True
    return account.day_pnl_usd <= -abs(risk.max_daily_loss_usd)


# ── main pre-trade check ──────────────────────────────────────────────────────
def check_order(
    order: Order,
    current_positions: Optional[list[Position]] = None,
    account: Optional[AccountSummary] = None,
    risk=RISK,
) -> tuple[bool, str]:
    """
    Validate ``order`` against all risk limits.

    Parameters
    ----------
    order : Order
        The intended order (qty in contracts, limit_price in dollars 0-1).
    current_positions : list[Position]
        The current book (from portfolio.tracker.positions()).
    account : AccountSummary
        Current account snapshot (for the kill switch / day P&L).
    risk : RiskLimits
        Defaults to config.RISK.

    Returns
    -------
    (ok, reason) : (bool, str)
        ok=True with reason="ok" if the order may be sent; otherwise ok=False
        with a human-readable rejection reason.
    """
    positions = current_positions or []

    # ── 0. sanity ─────────────────────────────────────────────────────────────
    if order.qty <= 0:
        return False, f"qty must be positive (got {order.qty})"
    if order.side not in ("yes", "no"):
        return False, f"side must be 'yes' or 'no' (got {order.side!r})"
    if order.action not in ("buy", "sell"):
        return False, f"action must be 'buy' or 'sell' (got {order.action!r})"
    if order.order_type not in ("limit", "market"):
        return False, f"order_type must be 'limit' or 'market' (got {order.order_type!r})"
    if order.order_type == "limit":
        if order.limit_price is None:
            return False, "limit order requires limit_price"
        if not (0.0 < float(order.limit_price) < 1.0):
            return False, f"limit_price must be in (0,1) dollars (got {order.limit_price})"

    closing = _is_closing(order, positions)

    # ── 1. kill switch (blocks NEW entries only; closes always allowed) ────────
    if account is not None and kill_switch_active(account, risk) and not closing:
        return (
            False,
            f"KILL SWITCH active (day P&L ${account.day_pnl_usd:.2f} "
            f"<= -${risk.max_daily_loss_usd:.2f}); new entries halted",
        )

    # ── 2. per-order contract cap ──────────────────────────────────────────────
    if order.qty > risk.max_contracts_per_order:
        return (
            False,
            f"qty {order.qty} > max_contracts_per_order {risk.max_contracts_per_order}",
        )

    # Risk-reducing closes skip the ceiling checks below.
    if closing:
        return True, "ok"

    # ── 3. per-market position cap (post-fill) ─────────────────────────────────
    net_after = _net_position_after(order, positions)
    if net_after > risk.max_position_per_market:
        return (
            False,
            f"net position {net_after} on {order.ticker} would exceed "
            f"max_position_per_market {risk.max_position_per_market}",
        )

    # ── 4. total exposure cap (post-fill) ──────────────────────────────────────
    exposure_after = _total_exposure_usd(positions) + _order_exposure_usd(order)
    if exposure_after > risk.max_total_exposure_usd:
        return (
            False,
            f"total exposure ${exposure_after:.2f} would exceed "
            f"max_total_exposure_usd ${risk.max_total_exposure_usd:.2f}",
        )

    return True, "ok"


# ── smoke test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import datetime as dt

    def acct(day_pnl=0.0, kill=False):
        return AccountSummary(
            balance_usd=44.0,
            total_exposure_usd=0.0,
            realized_pnl_usd=0.0,
            unrealized_pnl_usd=0.0,
            day_pnl_usd=day_pnl,
            kill_switch=kill,
        )

    def order(qty=3, side="yes", action="buy", otype="limit", price=0.40, ticker="KXNBAGAME-T1"):
        return Order(
            ticker=ticker, side=side, action=action, qty=qty,
            order_type=otype, limit_price=price, ts=dt.datetime.now(),
        )

    print("=" * 70)
    print("risk.py smoke test  (limits:", RISK, ")")
    print("=" * 70)

    cases = []

    # 1. normal small order -> OK
    cases.append(("normal 3-lot YES buy @0.40", order(), [], acct(), True))

    # 2. oversized per-order -> REJECT
    cases.append(("oversized 9-lot (cap 5)", order(qty=9), [], acct(), False))

    # 3. position cap breach -> REJECT  (existing 18 + new 5 = 23 > 20)
    existing = [Position(ticker="KXNBAGAME-T1", side="yes", qty=18, avg_price=0.40, mark=0.42)]
    cases.append(("position cap (18+5=23 > 20)", order(qty=5), existing, acct(), False))

    # 4. exposure cap breach -> REJECT  (5 contracts @0.95 = $4.75; plus big book)
    big_book = [Position(ticker="KXNBAGAME-OTHER", side="yes", qty=20, avg_price=0.95, mark=0.95)]
    cases.append(("exposure cap ($19 book + $4.75)", order(qty=5, price=0.95), big_book, acct(), False))

    # 5. kill switch active -> REJECT new entry
    cases.append(("kill switch (day P&L -$6)", order(), [], acct(day_pnl=-6.0), False))

    # 6. kill switch active but CLOSING (sell held side) -> ALLOW
    held = [Position(ticker="KXNBAGAME-T1", side="yes", qty=5, avg_price=0.40, mark=0.42)]
    cases.append(("close during kill switch (sell held)", order(action="sell"), held, acct(day_pnl=-6.0), True))

    # 7. manual kill switch flag -> REJECT
    cases.append(("manual kill flag", order(), [], acct(kill=True), False))

    # 8. limit order missing price -> REJECT
    bad = order(); bad.limit_price = None
    cases.append(("limit w/o price", bad, [], acct(), False))

    # 9. bad price out of (0,1) -> REJECT
    cases.append(("price 1.50 out of range", order(price=1.50), [], acct(), False))

    passed = 0
    for name, o, pos, a, expect_ok in cases:
        ok, reason = check_order(o, pos, a)
        status = "PASS" if ok == expect_ok else "FAIL"
        if ok == expect_ok:
            passed += 1
        print(f"[{status}] {name:40s} -> ok={ok!s:5s}  ({reason})")

    print("-" * 70)
    print(f"{passed}/{len(cases)} cases passed")
    sys.exit(0 if passed == len(cases) else 1)
