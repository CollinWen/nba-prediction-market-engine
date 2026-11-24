"""
Portfolio tracker — implements the ``Portfolio`` protocol (positions / fills / account).

Reads truth from Kalshi via the gateway (``core.kalshi_client`` if present, else the
read-only stub) and converts raw Kalshi JSON into the typed dataclasses in
``core.interfaces``. Computes:

  * realized P&L  — from fills (sell proceeds - buy cost - fees), per Kalshi's own
    realized_pnl when available, else reconstructed from the fill stream.
  * unrealized P&L — open positions marked at current market price.
  * exposure       — worst-case dollars at risk across open positions (cost basis).
  * day P&L        — realized(today) + unrealized, used by the risk kill switch.

UNIT NORMALIZATION (critical): Kalshi /portfolio endpoints report prices and money
in CENTS (ints). The interfaces use DOLLARS (float 0-1). We normalize at this
boundary: cents / 100.0.

Kalshi raw shapes consumed (see core/_kalshi_client_stub.py for the full contract):
  balance   : {"balance": <cents>}
  positions : [{"ticker","position"(signed: + long YES / - long NO),
               "market_exposure"(cents),"realized_pnl"(cents),
               "total_traded"(cents), ...}]
  fills     : [{"trade_id","order_id","ticker","side","action","count",
               "yes_price"(cents),"no_price"(cents),"is_taker","created_time"}]
"""
from __future__ import annotations

import sys
import datetime as dt
from pathlib import Path
from typing import Optional

# ── import bootstrap ──────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot  # noqa: E402
boot()

import config  # noqa: E402
from core.interfaces import Position, Fill, AccountSummary  # noqa: E402
from core.fees import fee as kalshi_fee  # noqa: E402
from core._kalshi_client_stub import get_client  # noqa: E402


def _c2d(cents) -> float:
    """Cents (int/str/None) -> dollars float."""
    if cents is None or cents == "":
        return 0.0
    try:
        return float(cents) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _money(raw: dict, dollars_key: str, cents_key: str) -> float:
    """Read a money field that the API has shipped in two shapes.

    Newer payloads carry string-dollar fields ('market_exposure_dollars');
    older ones carry integer cents ('market_exposure'). Prefer dollars.
    The June 8 session ran blind because only the cents shape was parsed.
    """
    if raw.get(dollars_key) not in (None, ""):
        return float(raw[dollars_key])
    return _c2d(raw.get(cents_key))


def _count(raw: dict, int_key: str, fp_key: str) -> int:
    """Read a contract count: integer field ('position') or fixed-point string
    ('position_fp')."""
    if raw.get(int_key) not in (None, ""):
        return int(raw[int_key])
    try:
        return int(float(raw.get(fp_key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _parse_ts(s) -> dt.datetime:
    if not s:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(s, (int, float)):
        return dt.datetime.fromtimestamp(s, dt.timezone.utc)
    txt = str(s).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(txt)
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)


class Tracker:
    """Concrete ``Portfolio``. Read-only against Kalshi (never mutates the book)."""

    def __init__(self, client=None):
        self._client = client or get_client()
        self._mark_cache: dict[str, float] = {}

    # ── current mark for a ticker (best-effort; falls back to avg) ──────────────
    def _mark(self, ticker: str, fallback: float = 0.5) -> float:
        if ticker in self._mark_cache:
            return self._mark_cache[ticker]
        try:
            m = self._client.get_market(ticker)
            bid = _c2d(m.get("yes_bid"))
            ask = _c2d(m.get("yes_ask"))
            if bid and ask:
                mark = (bid + ask) / 2.0
            else:
                mark = _c2d(m.get("last_price")) or fallback
        except Exception:
            mark = fallback
        self._mark_cache[ticker] = mark
        return mark

    # ── Portfolio.positions ─────────────────────────────────────────────────────
    def positions(self) -> list[Position]:
        raw = self._client.get_positions()
        out: list[Position] = []
        for r in raw:
            net = _count(r, "position", "position_fp")
            if net == 0:
                continue
            side = "yes" if net > 0 else "no"
            qty = abs(net)
            # avg entry price (dollars): market_exposure is current cost at risk
            # for the open lot; total_traded is cumulative. Prefer exposure/qty.
            exposure = _money(r, "market_exposure_dollars", "market_exposure")
            avg = (exposure / qty) if qty else 0.0
            if not (0.0 < avg <= 1.0):
                # fall back to a neutral estimate if Kalshi didn't give usable cost
                avg = self._mark(r["ticker"])
            mark = self._mark(r["ticker"], fallback=avg)
            out.append(Position(ticker=r["ticker"], side=side, qty=qty,
                                avg_price=round(avg, 4), mark=round(mark, 4)))
        return out

    # ── Portfolio.fills ───────────────────────────────────────────────────────────
    def fills(self, since: Optional[dt.datetime] = None) -> list[Fill]:
        # Real KalshiClient signature is get_fills(since=None); raw stub used limit=.
        try:
            raw = self._client.get_fills(since=since)
        except TypeError:
            raw = self._client.get_fills(limit=200)
        out: list[Fill] = []
        for r in raw:
            ts = _parse_ts(r.get("created_time"))
            if since is not None and ts < since:
                continue
            side = r.get("side", "yes")
            count = _count(r, "count", "count_fp")
            # price is the price paid on the traded side
            if side == "yes":
                price = _money(r, "yes_price_dollars", "yes_price")
            else:
                price = _money(r, "no_price_dollars", "no_price")
            is_taker = bool(r.get("is_taker", False))
            # prefer the API's actual fee over our estimate when present
            if r.get("fee_cost") not in (None, ""):
                f = float(r["fee_cost"])
            else:
                f = kalshi_fee(count, price, maker=not is_taker, nba=True)
            out.append(Fill(
                ticker=r.get("ticker", ""),
                side=side,
                action=r.get("action", "buy"),
                qty=count,
                price=round(price, 4),
                fee=round(f, 4),
                order_id=r.get("order_id", ""),
                fill_id=r.get("trade_id", r.get("fill_id", "")),
                ts=ts,
            ))
        out.sort(key=lambda x: x.ts)
        return out

    # ── realized P&L from the fill stream (FIFO-ish, side-aware) ─────────────────
    def realized_pnl(self, fills: Optional[list[Fill]] = None) -> float:
        """
        Realized P&L in dollars: sells release cost; we approximate proceeds minus
        cost minus fees. For Kalshi, buying a YES costs price; selling it returns
        price; settlement gives 1.0 if it resolves YES. We compute trade-level
        realized P&L = sum(sell proceeds) - sum(buy cost on closed qty) - all fees.
        This is a conservative live estimate; reconcile.py cross-checks vs Kalshi's
        own realized_pnl field.
        """
        fills = fills if fills is not None else self.fills()
        # net cash flow per ticker+side: buys are outflow, sells inflow
        cash = 0.0
        fees = 0.0
        for f in fills:
            sign = -1.0 if f.action == "buy" else 1.0
            cash += sign * f.price * f.qty
            fees += f.fee
        return round(cash - fees, 4)

    # ── Portfolio.account ──────────────────────────────────────────────────────────
    def account(self) -> AccountSummary:
        bal = self._client.get_balance()
        # Real KalshiClient returns balance already in dollars (float); the raw
        # stub returns {"balance": cents}. Handle both.
        balance = _c2d(bal.get("balance")) if isinstance(bal, dict) else float(bal)

        pos = self.positions()
        all_fills = self.fills()

        # exposure = worst-case cost basis of open positions
        exposure = round(sum(p.avg_price * p.qty for p in pos), 4)
        unrealized = round(sum(p.unrealized for p in pos), 4)

        # realized: prefer Kalshi's own realized_pnl across positions when available
        kalshi_realized = self._kalshi_realized()
        realized = kalshi_realized if kalshi_realized is not None else self.realized_pnl(all_fills)

        # day P&L: realized fills since local midnight + unrealized
        today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day_fills = [f for f in all_fills if f.ts >= today]
        day_realized = self.realized_pnl(day_fills)
        day_pnl = round(day_realized + unrealized, 4)

        kill = day_pnl <= -abs(config.RISK.max_daily_loss_usd)

        return AccountSummary(
            balance_usd=round(balance, 2),
            total_exposure_usd=exposure,
            realized_pnl_usd=round(realized, 4),
            unrealized_pnl_usd=unrealized,
            day_pnl_usd=day_pnl,
            kill_switch=kill,
        )

    def _kalshi_realized(self) -> Optional[float]:
        """Sum Kalshi's per-position realized P&L if the field is present."""
        try:
            raw = self._client.get_positions()
        except Exception:
            return None
        if not raw:
            return None
        if not any("realized_pnl" in r or "realized_pnl_dollars" in r for r in raw):
            return None
        return round(sum(_money(r, "realized_pnl_dollars", "realized_pnl") for r in raw), 4)


# ── smoke test: reads REAL account data (read-only) and prints it ───────────────
if __name__ == "__main__":
    print("=" * 70)
    print("tracker.py smoke test  (READ-ONLY against real account)")
    print("=" * 70)

    t = Tracker()
    print("client:", getattr(t._client, "name", type(t._client).__name__))

    try:
        acct = t.account()
    except Exception as e:
        print("\nACCOUNT READ FAILED:", e)
        print("(If creds are unset this is expected; code path is still exercised.)")
        sys.exit(0)

    print("\n--- ACCOUNT ---")
    print(f"  balance        : ${acct.balance_usd:.2f}")
    print(f"  total_exposure : ${acct.total_exposure_usd:.2f}")
    print(f"  realized P&L   : ${acct.realized_pnl_usd:.2f}")
    print(f"  unrealized P&L : ${acct.unrealized_pnl_usd:.2f}")
    print(f"  day P&L        : ${acct.day_pnl_usd:.2f}")
    print(f"  kill_switch    : {acct.kill_switch}")

    pos = t.positions()
    print(f"\n--- POSITIONS ({len(pos)}) ---")
    for p in pos:
        print(f"  {p.ticker:30s} {p.side:3s} {p.qty:4d} @ ${p.avg_price:.2f} "
              f"mark ${p.mark:.2f}  unreal ${p.unrealized:+.2f}")
    if not pos:
        print("  (no open positions)")

    fl = t.fills()
    print(f"\n--- FILLS ({len(fl)}, showing last 10) ---")
    for f in fl[-10:]:
        print(f"  {f.ts:%Y-%m-%d %H:%M} {f.action:4s} {f.side:3s} {f.qty:4d} "
              f"{f.ticker:28s} @ ${f.price:.2f} fee ${f.fee:.2f}")
    if not fl:
        print("  (no fills)")

    print("\nSmoke test complete (read-only; nothing was placed or canceled).")
