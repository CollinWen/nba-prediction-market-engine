"""
feeds/tick_collector.py
=======================
Kalshi WebSocket tick collector. Streams every trade for one or more market
tickers and saves to data/kalshi/ticks/{ticker}.parquet in the standard format.

Refactored from scripts/live/14_tick_collector.py for the live-execution tree.

Two modes
---------
  --tickers  KXNBAGAME-... ...   watch specific tickers only (used by run.py)
  (no --tickers)                 auto-discover all active NBA markets

USAGE
-----
  # Specific tickers (game-day launch via run.py):
  python live-execution/feeds/tick_collector.py \\
      --tickers KXNBAGAME-26JUN08NYKSAS-SAS KXNBAGAME-26JUN08NYKSAS-NYK \\
      --until "2026-06-09 01:00"

  # Auto-discover all active NBA markets:
  python live-execution/feeds/tick_collector.py --until "2026-06-09 01:00"

  # Dry-run (discover/print but don't stream):
  python live-execution/feeds/tick_collector.py --dry-run
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
import argparse
import threading
import datetime as dt
from pathlib import Path
from collections import defaultdict

try:
    import websockets
except ImportError:
    print("websockets not installed. Run: pip install websockets")
    sys.exit(1)

import pandas as pd

# ── Import bootstrap ──────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot
boot()

from kalshi_auth import auth_headers, get as kalshi_get  # noqa: E402
import config  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
TICK_DIR   = config.TICK_DIR
WS_URL     = config.WS_URL
LOG_FILE   = config.DATA_DIR / "logs/tick_collector_log.csv"
FLUSH_SECS = 60

LEAGUE_SERIES = {
    "NBA":  ["KXNBAGAME"],
    "WNBA": ["KXWNBAGAME"],
}

TICK_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


# ── Market discovery ──────────────────────────────────────────────────────────

def discover_active_markets(leagues: list[str]) -> list[dict]:
    """Query Kalshi REST for all active basketball markets matching leagues."""
    active = []
    for league in leagues:
        for series in LEAGUE_SERIES.get(league.upper(), []):
            try:
                resp = kalshi_get(
                    "/markets",
                    params={"series_ticker": series, "status": "open", "limit": 200},
                )
                for m in resp.get("markets", []):
                    bid = float(m.get("yes_bid_dollars") or 0)
                    ask = float(m.get("yes_ask_dollars") or 0)
                    mid = (bid + ask) / 2
                    active.append({
                        "ticker":       m["ticker"],
                        "event_ticker": m.get("event_ticker", ""),
                        "title":        m.get("title", ""),
                        "implied_prob": round(mid, 4),
                        "league":       league,
                    })
                time.sleep(0.2)
            except Exception as e:
                print(f"  [WARN] Could not fetch {series}: {e}")
    return active


# ── Tick buffer ───────────────────────────────────────────────────────────────

def _load_existing_ts(ticker: str) -> set:
    path = TICK_DIR / f"{ticker}.parquet"
    if not path.exists():
        return set()
    try:
        df = pd.read_parquet(path)
        return set(df["ts"].astype("int64").tolist())
    except Exception:
        return set()


class TickBuffer:
    """Thread-safe in-memory buffer; flushes to parquet every FLUSH_SECS."""

    def __init__(self):
        self._lock     = threading.Lock()
        self._buffers  = defaultdict(list)
        self._counts   = defaultdict(int)
        self._existing = {}

    def preload(self, ticker: str):
        self._existing[ticker] = _load_existing_ts(ticker)

    def add(self, ticker: str, ts_utc: pd.Timestamp, price: float):
        ts_int = ts_utc.value
        if ts_int in self._existing.get(ticker, set()):
            return
        with self._lock:
            self._buffers[ticker].append({"ts": ts_utc, "price": price})
            self._counts[ticker] += 1

    def flush(self, ticker: str | None = None) -> int:
        tickers = [ticker] if ticker else list(self._buffers.keys())
        flushed = 0
        with self._lock:
            for t in tickers:
                rows = self._buffers.get(t, [])
                if not rows:
                    continue
                df_new = pd.DataFrame(rows)
                df_new["ts"] = pd.to_datetime(df_new["ts"], utc=True)
                path = TICK_DIR / f"{t}.parquet"
                if path.exists():
                    df_old = pd.read_parquet(path)
                    df_old["ts"] = pd.to_datetime(df_old["ts"], utc=True)
                    df_merged = pd.concat([df_old, df_new], ignore_index=True)
                    df_merged = df_merged.drop_duplicates("ts").sort_values("ts")
                else:
                    df_merged = df_new.sort_values("ts")
                df_merged.to_parquet(path, index=False)
                if t not in self._existing:
                    self._existing[t] = set()
                self._existing[t].update(df_new["ts"].astype("int64").tolist())
                self._buffers[t] = []
                flushed += len(df_new)
        return flushed

    def summary(self) -> dict:
        with self._lock:
            return {t: n for t, n in self._counts.items() if n > 0}


_buffer = TickBuffer()


# ── WebSocket streaming ───────────────────────────────────────────────────────

async def _stream_all(tickers: list[str], stop_event: asyncio.Event):
    hdrs = auth_headers("GET", "/trade-api/ws/v2")

    while not stop_event.is_set():
        try:
            async with websockets.connect(
                WS_URL,
                additional_headers=hdrs,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                batch_size = 50
                for i in range(0, len(tickers), batch_size):
                    batch = tickers[i: i + batch_size]
                    await ws.send(json.dumps({
                        "id":     i + 1,
                        "cmd":    "subscribe",
                        "params": {
                            "channels":       ["trade"],
                            "market_tickers": batch,
                        },
                    }))
                print(f"  [WS] Subscribed to {len(tickers)} tickers", flush=True)

                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if msg.get("type") == "trade":
                        d      = msg.get("msg", {})
                        ticker = d.get("market_ticker", "")
                        price  = d.get("yes_price_dollars") or d.get("yes_price")
                        ts_ms  = d.get("ts_ms") or d.get("ts")
                        if price is not None and ts_ms and ticker:
                            try:
                                ts_ns = int(ts_ms) * (
                                    1_000_000 if d.get("ts_ms") else 1_000_000_000
                                )
                                ts = pd.Timestamp(ts_ns, tz="UTC")
                                p  = float(price)
                                if p > 1.0:
                                    p /= 100.0
                                _buffer.add(ticker, ts, p)
                            except Exception:
                                pass

        except websockets.exceptions.ConnectionClosedError as e:
            if stop_event.is_set():
                break
            print(f"  [WS] Disconnected ({e}), reconnecting in 5s...", flush=True)
            await asyncio.sleep(5)
            hdrs = auth_headers("GET", "/trade-api/ws/v2")
        except Exception as e:
            if stop_event.is_set():
                break
            print(f"  [WS] Error: {e}, reconnecting in 10s...", flush=True)
            await asyncio.sleep(10)
            hdrs = auth_headers("GET", "/trade-api/ws/v2")


async def _flush_loop(stop_event: asyncio.Event):
    while not stop_event.is_set():
        await asyncio.sleep(FLUSH_SECS)
        n = _buffer.flush()
        if n > 0:
            ts_now = dt.datetime.now().strftime("%H:%M:%S")
            total  = sum(_buffer.summary().values())
            print(f"  [{ts_now}] Flushed {n} trades  (session total: {total:,})", flush=True)


async def _run_collector(tickers: list[str], until: dt.datetime | None):
    stop_event = asyncio.Event()

    def _handle_signal():
        print("\nStopping tick collector...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, OSError):
            pass

    async def _stop_at_deadline():
        if until:
            delta = (until - dt.datetime.now()).total_seconds()
            if delta > 0:
                await asyncio.sleep(delta)
        stop_event.set()

    await asyncio.gather(
        _stream_all(tickers, stop_event),
        _flush_loop(stop_event),
        _stop_at_deadline(),
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalshi tick collector")
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="Watch specific tickers (space-separated). "
             "If omitted, auto-discovers active NBA markets.",
    )
    parser.add_argument(
        "--leagues", nargs="+", default=["NBA"],
        help="Leagues for auto-discovery (default: NBA). Ignored if --tickers given.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Discover/print tickers but don't stream.",
    )
    parser.add_argument(
        "--until", type=str, default=None,
        help="Stop at this datetime, e.g. '2026-06-09 01:00'.",
    )
    args = parser.parse_args()

    until = pd.Timestamp(args.until).to_pydatetime() if args.until else None

    print("=" * 60, flush=True)
    print("Kalshi Tick Collector", flush=True)
    print(f"Until   : {until or 'Ctrl-C'}", flush=True)
    print("=" * 60, flush=True)

    # ── Resolve tickers ──────────────────────────────────────────────────────
    if args.tickers:
        tickers = args.tickers
        print(f"\nWatching {len(tickers)} specified tickers:", flush=True)
        for t in tickers:
            print(f"  {t}", flush=True)
    else:
        leagues = [l.upper() for l in args.leagues]
        print(f"\nDiscovering active {'/'.join(leagues)} markets...", flush=True)
        markets = discover_active_markets(leagues)
        if not markets:
            print("No active markets found.", flush=True)
            return
        tickers = [m["ticker"] for m in markets]
        print(f"Found {len(markets)} tickers:", flush=True)
        for m in markets:
            print(f"  {m['ticker']:<50} mid={m['implied_prob']:.1%}", flush=True)

    if args.dry_run:
        print("\n--dry-run: not connecting WebSocket.", flush=True)
        return

    # ── Pre-load + stream ────────────────────────────────────────────────────
    print("\nPre-loading existing tick files...", flush=True)
    for t in tickers:
        _buffer.preload(t)

    print(f"\nStreaming {len(tickers)} tickers. Press Ctrl-C to stop.\n", flush=True)
    start_time = dt.datetime.now()

    try:
        asyncio.run(_run_collector(tickers, until))
    except KeyboardInterrupt:
        pass

    # ── Final flush + summary ────────────────────────────────────────────────
    print("\nFinal flush...", flush=True)
    _buffer.flush()

    summary = _buffer.summary()
    elapsed = (dt.datetime.now() - start_time).total_seconds() / 60
    total   = sum(summary.values())

    print("\n=== Tick Collector Session Summary ===", flush=True)
    print(f"Duration : {elapsed:.1f} min", flush=True)
    print(f"Total    : {total:,} trades", flush=True)
    for ticker, n in sorted(summary.items(), key=lambda x: -x[1]):
        if n > 0:
            path = TICK_DIR / f"{ticker}.parquet"
            rows = len(pd.read_parquet(path)) if path.exists() else n
            print(f"  {ticker:<50} {n:>6} new  ({rows:,} total)", flush=True)

    # ── Log run ──────────────────────────────────────────────────────────────
    log_row = pd.DataFrame([{
        "run_date":          start_time.strftime("%Y-%m-%d %H:%M"),
        "tickers":           ",".join(tickers[:5]) + ("..." if len(tickers) > 5 else ""),
        "tickers_collected": len([k for k, v in summary.items() if v > 0]),
        "total_trades":      total,
        "duration_min":      round(elapsed, 1),
    }])
    if LOG_FILE.exists():
        log_row = pd.concat([pd.read_csv(LOG_FILE), log_row], ignore_index=True)
    log_row.to_csv(LOG_FILE, index=False)


if __name__ == "__main__":
    main()
