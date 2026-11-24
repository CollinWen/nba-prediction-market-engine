"""
reconcile_fills.py
==================
Post-game audit: join a session blotter CSV against Kalshi's actual fills so we
know what REALLY happened, instead of the strategy log's assumed fills.

For every ORDER row in the blotter it reports whether the order filled (and at
what price/fee); then it reconstructs realized P&L from the actual fill stream
and flags any residual position that rode into settlement.

USAGE
-----
  # Most recent session for a game:
  python live-execution/blotter/reconcile_fills.py --game_id 0042500403

  # Explicit CSV:
  python live-execution/blotter/reconcile_fills.py \
      --csv <DATA_DIR>/live/blotter/session_<game_id>_<run_ts>.csv

Read-only: never places or cancels anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot  # noqa: E402
boot()

from core.kalshi_client import KalshiClient        # noqa: E402
from core.fees import fee as kalshi_fee            # noqa: E402
from blotter.session_log import BLOTTER_DIR        # noqa: E402


def _find_latest_csv(game_id: str) -> Path | None:
    candidates = sorted(BLOTTER_DIR.glob(f"session_{game_id}_*.csv"))
    return candidates[-1] if candidates else None


def _load_blotter_orders(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, dtype={"order_id": str})
    orders = df[df["event_type"] == "ORDER"].copy()
    orders = orders[orders["order_id"].notna() & (orders["order_id"] != "")]
    return orders


def _fetch_fills(client: KalshiClient, since: dt.datetime) -> pd.DataFrame:
    raw = client.get_fills(since=since)
    if not raw:
        return pd.DataFrame()
    rows = []
    for f in raw:
        side = f.get("side", "yes")
        action = f.get("action", "buy")
        # API has shipped two shapes: integer-cent fields (yes_price, count) and
        # string-dollar fields (yes_price_dollars, count_fp, fee_cost). Prefer dollars.
        dollars_key = f"{side}_price_dollars"
        if f.get(dollars_key) not in (None, ""):
            price = float(f[dollars_key])
        else:
            price_cents = f.get("yes_price") if side == "yes" else f.get("no_price")
            price = float(price_cents or 0) / 100.0
        if f.get("count") not in (None, ""):
            count = int(f["count"])
        else:
            count = int(float(f.get("count_fp", 0) or 0))
        is_taker = bool(f.get("is_taker", False))
        if f.get("fee_cost") not in (None, ""):
            fee = float(f["fee_cost"])
        else:
            fee = kalshi_fee(count, price, maker=not is_taker, nba=True)
        # Everything in net-YES terms so positions/cash are unambiguous:
        # buy yes / sell no  -> +qty net YES;  sell yes / buy no -> -qty net YES.
        yes_delta = count if (side, action) in (("yes", "buy"), ("no", "sell")) else -count
        # YES-frame cash: -delta * yes_price. This treats buying NO at p as
        # shorting YES at (1-p), so P&L closes out correctly against a
        # settlement leg of net_yes * settle_value — collateral posted on
        # shorts never appears as fill cash and must not be double counted.
        if f.get("yes_price_dollars") not in (None, ""):
            yes_price = float(f["yes_price_dollars"])
        elif side == "yes":
            yes_price = price
        else:
            yes_price = 1.0 - price
        cash = -yes_delta * yes_price
        rows.append({
            "fill_ts": f.get("created_time", ""),
            "ticker": f.get("ticker", ""),
            "order_id": str(f.get("order_id", "")),
            "side": side,
            "action": action,
            "count": count,
            "price": price,
            "is_taker": is_taker,
            "fee": fee,
            "yes_delta": yes_delta,
            "cash": cash,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("fill_ts").reset_index(drop=True)
    return df


def reconcile_session(csv_path: Path, client: KalshiClient | None = None) -> dict:
    """Join blotter orders vs Kalshi fills. Returns a summary dict; prints a report."""
    client = client or KalshiClient()
    orders = _load_blotter_orders(csv_path)
    if orders.empty:
        print(f"No ORDER rows in {csv_path}")
        return {}

    session_start = pd.to_datetime(orders["ts"].iloc[0]) - pd.Timedelta(minutes=5)
    fills = _fetch_fills(client, session_start.to_pydatetime())

    fills_by_oid: dict[str, pd.DataFrame] = {}
    if not fills.empty:
        fills_by_oid = {oid: g for oid, g in fills.groupby("order_id")}

    print("=" * 104)
    print(f"SESSION FILL RECONCILIATION: {csv_path.name}")
    print("=" * 104)

    # ── Section A: order-by-order — did each blotter order actually fill? ──────
    print("\n[A] BLOTTER ORDERS vs FILLS  (intended = what the strategy sent)")
    print(f"{'sent (UTC)':<11} {'intended':<16} {'limit':>6} {'ack':<9} "
          f"{'fill (Kalshi truth)':<22} {'fee':>7}  note")
    print("-" * 104)

    n_filled = n_unfilled = 0
    for _, o in orders.iterrows():
        oid = str(o["order_id"])
        f = fills_by_oid.get(oid)
        sent_ts = str(o["ts"])[11:19]
        intended = f"{o['order_action']} {o['order_side']}"
        limit = o.get("entry_price", "")
        if f is not None and not f.empty:
            qty = int(f["count"].sum())
            avg = float((f["price"] * f["count"]).sum() / qty) if qty else 0.0
            fee = float(f["fee"].sum())
            fside, fact = f["side"].iloc[0], f["action"].iloc[0]
            n_filled += 1
            note = ""
            if (fside, fact) != (o["order_side"], o["order_action"]):
                note = "Kalshi recorded opposite-side trade"
            print(f"{sent_ts:<11} {intended:<16} {limit!s:>6} {o['order_status']:<9} "
                  f"{f'{fact} {fside} x{qty} @ {avg:.2f}':<22} {fee:>7.3f}  {note}")
        else:
            n_unfilled += 1
            note = "NO FILL — order never executed"
            print(f"{sent_ts:<11} {intended:<16} {limit!s:>6} {o['order_status']:<9} "
                  f"{'—':<22} {'—':>7}  {note}")

    # ── Section B: chronological fill stream with running net-YES position ─────
    print(f"\n[B] FILL STREAM (ground truth, net-YES frame)")
    print(f"{'fill (UTC)':<11} {'trade':<16} {'px':>5} {'cash':>7} "
          f"{'net YES pos':>12}  {'taker':<5}")
    print("-" * 70)
    running = 0
    cash = fees = 0.0
    last_ticker = ""
    if not fills.empty:
        for _, f in fills.iterrows():
            running += int(f["yes_delta"])
            cash += f["cash"]
            fees += f["fee"]
            last_ticker = f["ticker"]
            trade = f"{f['action']} {f['side']} x{int(f['count'])}"
            taker = "Y" if f["is_taker"] else ""
            print(f"{str(f['fill_ts'])[11:19]:<11} {trade:<16} "
                  f"{f['price']:>5.2f} {f['cash']:>+7.2f} {running:>+12d}  {taker:<5}")

    print("-" * 70)

    # Fills on the account that match NO order in this blotter
    blotter_oids = set(orders["order_id"].astype(str))
    strays = fills[~fills["order_id"].isin(blotter_oids)] if not fills.empty else pd.DataFrame()
    if not strays.empty:
        print(f"\n[!] {len(strays)} fill(s) NOT from this session's orders "
              f"(other session / manual / stale resting):")
        for _, f in strays.iterrows():
            print(f"    {str(f['fill_ts'])[11:19]}  {f['action']} {f['side']} "
                  f"x{int(f['count'])} @ {f['price']:.2f}  order_id={f['order_id']}")

    print(f"\nOrders in blotter: {len(orders)}   filled: {n_filled}   no-fill: {n_unfilled}")
    print(f"YES-frame fill cash: {cash:+.2f}   fees: {fees:.2f}")

    # ── Settlement of whatever rode to the end ──────────────────────────────────
    # YES-frame: settlement closes net_yes at settle value (1 if YES, 0 if NO).
    settle_pnl = 0.0
    if running != 0 and last_ticker:
        mkt = {}
        try:
            mkt = client.get_market(last_ticker)
        except Exception:
            pass
        result = (mkt.get("result") or "").lower()       # 'yes' | 'no' | ''
        side = "YES" if running > 0 else "NO"
        if result:
            settle_val = 1.0 if result == "yes" else 0.0
            settle_pnl = running * settle_val
            print(f"\nRESIDUAL: net {running:+d} YES ({side} x{abs(running)}) "
                  f"{last_ticker} rode into settlement -> settled "
                  f"'{result.upper()}' -> settlement leg {settle_pnl:+.2f}")
        else:
            print(f"\nRESIDUAL: net {running:+d} YES ({side} x{abs(running)}) "
                  f"{last_ticker} — settlement unknown "
                  f"(market status: {mkt.get('status', '?')})")

    realized = cash + settle_pnl - fees
    print(f"\nREALIZED P&L (fills + settlement - fees): {realized:+.2f} USD")
    print("=" * 104)

    return {
        "orders": len(orders), "filled": n_filled, "unfilled": n_unfilled,
        "cash": cash, "fees": fees, "settlement": settle_pnl, "realized": realized,
        "residual_net_yes": running,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Reconcile a session blotter vs Kalshi fills")
    ap.add_argument("--csv", help="Path to a session blotter CSV")
    ap.add_argument("--game_id", help="Find the most recent session CSV for this game")
    args = ap.parse_args()

    if args.csv:
        path = Path(args.csv)
    elif args.game_id:
        path = _find_latest_csv(args.game_id)
        if path is None:
            sys.exit(f"No blotter CSV found for game {args.game_id} in {BLOTTER_DIR}")
    else:
        sys.exit("Pass --csv or --game_id")

    reconcile_session(path)
