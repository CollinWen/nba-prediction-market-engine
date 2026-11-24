"""
run.py
======
Game-day launcher for the NBA Kalshi trading terminal.

Starts three components:
  1. Tick collector  — streams Kalshi trades to parquet (subprocess)
  2. Dashboard       — Plotly Dash on http://127.0.0.1:8050 (subprocess, optional)
  3. ExecutionEngine — runs a registered strategy (main thread)

Default mode: dry_run=True (paper trading). Pass --live to place real orders.

USAGE
-----
  # Auto-discover tonight's game:
  python live-execution/run.py

  # Explicit game params (recommended):
  python live-execution/run.py \\
    --game_id 0042500404 \\
    --home SAS --away NYK \\
    --home_ticker KXNBAGAME-26JUN08NYKSAS-SAS \\
    --until "2026-06-09 01:30"

  # Skip the Dash dashboard (headless / terminal-only):
  python live-execution/run.py --game_id ... --no_dashboard

  # Pick any strategy from strategy/registry.py:
  python live-execution/run.py --game_id ... --strategy model_edge_tight

  # LIVE (real orders — USE WITH EXTREME CARE):
  python live-execution/run.py --game_id ... --live
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import time
from pathlib import Path

# ── Bootstrap ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent        # .../live-execution
_ROOT = _HERE.parent                           # repo root
sys.path.insert(0, str(_HERE))
from _bootstrap import boot
boot()

# Windows consoles default to cp1252, which can't encode arrows/em-dashes and
# crashed the June 8 end-of-session summary. Force UTF-8, never crash on print.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config                                    # noqa: E402
from core.kalshi_client import KalshiClient      # noqa: E402
from core.nba_feed import NBAFeed                # noqa: E402
from core.model_signal import ModelSignalEngine  # noqa: E402
from execution.order_manager import OrderManager # noqa: E402
from execution.engine import ExecutionEngine     # noqa: E402
from portfolio.tracker import Tracker            # noqa: E402
from strategy import registry                    # noqa: E402
from blotter.session_log import SessionLog       # noqa: E402


# ── Python executable ─────────────────────────────────────────────────────────

def _python_exe() -> str:
    """Return path to venv python if it exists, else current interpreter."""
    venv_py = _ROOT / "venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


# ── Auto-discovery ─────────────────────────────────────────────────────────────

def auto_discover() -> dict | None:
    """
    Find tonight's NBA game and its Kalshi home-side ticker.
    Returns {game_id, home, away, home_ticker} or None.
    """
    try:
        feed   = NBAFeed()
        client = KalshiClient()
        games  = feed.todays_games()
        open_tickers = client.list_nba_markets("open")
    except Exception as e:
        print(f"[auto_discover] failed: {e}")
        return None

    if not games:
        print("[auto_discover] NBA S3 shows no games today.")
        return None

    today    = dt.date.today()
    date_str = today.strftime("%y%b%d").upper()   # e.g. 26JUN08

    for game in games:
        home = game["home"]
        away = game["away"]
        if not home or not away:
            continue
        prefix = f"{config.NBA_SERIES}-{date_str}{away}{home}"
        home_ticker = next(
            (t for t in open_tickers
             if t.upper().startswith(prefix.upper()) and t.upper().endswith(f"-{home}")),
            None,
        )
        if home_ticker:
            print(
                f"[auto_discover] {away} @ {home}  game_id={game['game_id']}\n"
                f"[auto_discover] home_ticker: {home_ticker}"
            )
            return {
                "game_id":     game["game_id"],
                "home":        home,
                "away":        away,
                "home_ticker": home_ticker,
            }

    print(
        f"[auto_discover] no open Kalshi ticker matched today's games.\n"
        f"  Games:   {[(g['away'], g['home']) for g in games]}\n"
        f"  Kalshi:  {open_tickers[:6]}\n"
        f"  Try passing --game_id --home --away --home_ticker manually."
    )
    return None


def _infer_away(home_ticker: str) -> str | None:
    """Extract away team from ticker: KXNBAGAME-26JUN08NYKSAS-SAS -> NYK."""
    m = re.search(r"\d{2}[A-Z]{3}\d{2}([A-Z]{3})[A-Z]{3}-[A-Z]{3}$", home_ticker)
    return m.group(1) if m else None


def _away_ticker(home_ticker: str, away: str) -> str:
    """Replace the trailing team suffix: ...-SAS -> ...-NYK."""
    return re.sub(r"-[A-Z]{3}$", f"-{away}", home_ticker)


# ── Subprocess launchers ───────────────────────────────────────────────────────

def start_tick_collector(tickers: list[str], until: str) -> subprocess.Popen:
    script = _HERE / "feeds" / "tick_collector.py"
    cmd = [_python_exe(), str(script), "--tickers", *tickers, "--until", until]
    print(f"[run] tick collector: {' '.join(cmd[:3])} ... --until {until}")
    return subprocess.Popen(cmd, cwd=str(_ROOT))


def start_dashboard() -> subprocess.Popen:
    script = _HERE / "run_terminal.py"
    cmd = [_python_exe(), str(script)]
    print(f"[run] dashboard: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(_ROOT))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="NBA Kalshi game-day launcher (dry_run=True by default)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--game_id",     help="NBA game ID, e.g. 0042500404")
    ap.add_argument("--home",        help="Home team Kalshi abbr, e.g. SAS")
    ap.add_argument("--away",        help="Away team Kalshi abbr, e.g. NYK")
    ap.add_argument("--home_ticker",
                    help="Kalshi home-side ticker, e.g. KXNBAGAME-26JUN08NYKSAS-SAS")
    # nargs="+" so an unquoted datetime ('--until 2026-06-09 01:30') still parses —
    # the unquoted form killed four launch attempts on June 8 and cost all of Q1.
    ap.add_argument("--until", nargs="+",
                    help="Stop at this datetime, e.g. '2026-06-09 01:30'")
    ap.add_argument("--pregame", type=float, default=None,
                    help="Override pregame P(home win), e.g. 0.62 — use when restarting "
                         "mid-game so the model feature isn't locked from a live price")
    ap.add_argument("--qty", type=int, default=1,
                    help="Contracts per trade (default: 1)")
    ap.add_argument("--strategy", choices=[sp.key for sp in registry.all_specs()],
                    default="model_edge",
                    help="Strategy key from strategy/registry.py (default: model_edge)")
    ap.add_argument("--no_dashboard", action="store_true",
                    help="Skip the Plotly Dash dashboard")
    ap.add_argument("--live", action="store_true",
                    help="LIVE mode: actually place real orders (default: dry_run=True)")
    args = ap.parse_args()

    dry_run = not args.live
    if not dry_run:
        print("\n" + "!" * 64)
        print("  *** LIVE MODE — REAL ORDERS WILL BE PLACED ***")
        print("  Make sure config.RISK limits are correct.")
        print("!" * 64 + "\n")
        resp = input("Type YES to confirm live trading: ")
        if resp.strip().upper() != "YES":
            print("Aborted.")
            sys.exit(0)

    # ── Resolve game params ───────────────────────────────────────────────────
    if args.game_id and args.home and args.home_ticker:
        away = args.away or _infer_away(args.home_ticker) or "???"
        game_params = {
            "game_id":     args.game_id,
            "home":        args.home,
            "away":        away,
            "home_ticker": args.home_ticker,
        }
    else:
        print("[run] Auto-discovering tonight's game...")
        game_params = auto_discover()
        if game_params is None:
            print(
                "[run] Could not auto-discover. Provide --game_id --home --away --home_ticker."
            )
            sys.exit(1)

    game_id     = game_params["game_id"]
    home        = game_params["home"]
    away        = game_params["away"]
    home_ticker = game_params["home_ticker"]
    away_t      = _away_ticker(home_ticker, away)

    # ── Resolve --until ───────────────────────────────────────────────────────
    if args.until:
        until_dt = dt.datetime.fromisoformat(" ".join(args.until))
    else:
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        until_dt = dt.datetime.combine(tomorrow, dt.time(1, 30))
        print(f"[run] --until not set; defaulting to {until_dt:%Y-%m-%d %H:%M}")
    until_str = until_dt.strftime("%Y-%m-%d %H:%M")

    # ── Startup banner ────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("  NBA KALSHI GAME-DAY TERMINAL")
    print("=" * 64)
    print(f"  Game     : {away} @ {home}  (id={game_id})")
    print(f"  Ticker   : {home_ticker}")
    print(f"  Strategy : {args.strategy.upper()}")
    print(f"  Mode     : {'*** LIVE ***' if not dry_run else 'DRY-RUN (paper trading)'}")
    print(f"  Qty      : {args.qty} contract(s)")
    print(f"  Until    : {until_str}")
    if not args.no_dashboard:
        print(f"  Dashboard: http://127.0.0.1:{config.DASH_PORT}")
    print("=" * 64)
    print()

    # ── Wire providers ────────────────────────────────────────────────────────
    config.RISK.dry_run = dry_run
    client      = KalshiClient()
    feed        = NBAFeed()
    signal_eng  = ModelSignalEngine()
    tracker     = Tracker(client=client)
    order_mgr   = OrderManager(client=client, risk=config.RISK, portfolio=tracker)

    # ── Session blotter ───────────────────────────────────────────────────────
    blotter = SessionLog(game_id, dry_run=dry_run)
    print(f"[run] blotter: {blotter.path()}")

    # ── Start tick collector ──────────────────────────────────────────────────
    tickers = [home_ticker, away_t]
    tick_proc = start_tick_collector(tickers, until_str)
    print(f"[run] tick collector PID={tick_proc.pid}")

    # ── Start dashboard ───────────────────────────────────────────────────────
    dash_proc = None
    if not args.no_dashboard:
        time.sleep(1.5)
        dash_proc = start_dashboard()
        print(f"[run] dashboard PID={dash_proc.pid}  -> http://127.0.0.1:{config.DASH_PORT}")
        time.sleep(1.5)

    # ── Run strategy (main thread) ────────────────────────────────────────────
    spec = registry.get(args.strategy)
    strategy = spec.factory(signal_eng)
    strategy.verbose = True
    print(f"\n[run] Starting execution engine + {spec.key} (main thread)...")
    machine = ExecutionEngine(
        game_id=game_id,
        home=home,
        home_ticker=home_ticker,
        strategy=strategy,
        feed=feed,
        client=client,
        order_manager=order_mgr,
        tracker=tracker,
        blotter=blotter,
        qty=args.qty,
        dry_run=dry_run,
        pregame_override=args.pregame,
    )
    try:
        machine.run(until_dt)
    except KeyboardInterrupt:
        print("\n[run] Ctrl-C — shutting down...")

    # ── Shutdown ──────────────────────────────────────────────────────────────
    blotter.close()
    print(f"\n[run] Blotter saved: {blotter.path()}")

    if machine:
        trades = machine.trades
        if trades:
            import pandas as pd
            df = pd.DataFrame(trades)
            out = config.LIVE_DIR / f"trades_{game_id}_{dt.date.today()}.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out, index=False)
            print(f"[run] Trades: {out}")

    for label, proc in [("tick collector", tick_proc), ("dashboard", dash_proc)]:
        if proc is not None:
            try:
                proc.terminate()
                print(f"[run] stopped {label} (PID={proc.pid})")
            except Exception:
                pass

    print("[run] done.")


if __name__ == "__main__":
    main()
