"""
session_log.py
==============
Structured CSV audit trail for one game session.

One row per event — captures every signal fire, order placement, position
entry/exit, with full game state + model context at the moment it happened.
Use this for post-game review of what the system saw and did.

Event types
-----------
SIGNAL  — fade-the-over-reaction signal detector fired
ENTRY   — strategy opened a position (confirmed fill)
EXIT    — strategy closed a position (confirmed fill)
ORDER   — order placed or cancelled (execution layer)
GUARD   — engine guard fired (entry suppressed, halt, flatten)
"""
from __future__ import annotations

import csv
import datetime as dt
import threading
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot
boot()

import config  # noqa: E402

BLOTTER_DIR = config.LIVE_DIR / "blotter"

FIELDNAMES = [
    "ts",              # ISO-8601 UTC timestamp
    "event_type",      # SIGNAL | ENTRY | EXIT | ORDER
    "ticker",          # Kalshi market ticker

    # game state at event time
    "period",          # quarter (1-4, 5+ = OT)
    "secs_remaining",  # regulation seconds remaining
    "score_diff",      # home - away

    # model / edge
    "model_prob",      # model P(home win)
    "kalshi_price",    # Kalshi home-side mid price (0-1)
    "edge",            # model_prob - kalshi_price

    # fade-the-over-reaction signal fields (SIGNAL events)
    "dyn_thresh",      # dynamic threshold fraction
    "p_peak",          # price peak before the drop
    "p_entry",         # entry price (drop bottom)
    "drop_pct",        # drop magnitude %
    "drop_secs",       # drop duration in seconds

    # position fields (ENTRY / EXIT events)
    "direction",       # YES | NO
    "qty",             # contracts
    "entry_price",     # price on open (0-1)
    "exit_price",      # price on close (0-1)

    # P&L (EXIT events)
    "gross",           # price-delta gain × qty (0-1 per contract)
    "net_pnl",         # gross - spread_cost
    "hold_secs",       # seconds held
    "reason",          # converge | tp | stop | time

    # order fields (ORDER events)
    "order_id",        # Kalshi order_id or DRYRUN-xxx
    "order_side",      # yes | no
    "order_action",    # buy | sell
    "order_type",      # limit | market
    "order_status",    # dry_run | resting | filled | rejected | canceled
    "fee_in",
    "fee_out",

    # meta
    "dry_run",         # True | False
]


class SessionLog:
    """Thread-safe CSV audit trail. Open at session start, close on shutdown."""

    def __init__(self, game_id: str, dry_run: bool = True):
        BLOTTER_DIR.mkdir(parents=True, exist_ok=True)
        run_ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = BLOTTER_DIR / f"session_{game_id}_{run_ts}.csv"
        self._lock = threading.Lock()
        self._dry_run = dry_run
        self._fh = open(self._path, "w", newline="", buffering=1)
        self._writer = csv.DictWriter(
            self._fh, fieldnames=FIELDNAMES, extrasaction="ignore"
        )
        self._writer.writeheader()
        self._fh.flush()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _base_row(self, event_type: str) -> dict:
        row = {k: "" for k in FIELDNAMES}
        row["ts"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
        row["event_type"] = event_type
        row["dry_run"] = self._dry_run
        return row

    def _game_state_fields(self, game_state) -> dict:
        if game_state is None:
            return {}
        return {
            "period":         getattr(game_state, "period", ""),
            "secs_remaining": getattr(game_state, "secs_remaining", ""),
            "score_diff":     getattr(game_state, "score_diff", ""),
        }

    def _write(self, row: dict):
        with self._lock:
            self._writer.writerow(row)
            self._fh.flush()

    # ── public API ────────────────────────────────────────────────────────────

    def log_signal(self, ticker: str, game_state, signal_dict: dict):
        """Fade-the-over-reaction signal detector fired."""
        row = self._base_row("SIGNAL")
        row["ticker"] = ticker
        row.update(self._game_state_fields(game_state))
        row.update({
            "kalshi_price": signal_dict.get("p_entry", ""),
            "dyn_thresh":   signal_dict.get("dyn_thresh", ""),
            "p_peak":       signal_dict.get("p_peak", ""),
            "p_entry":      signal_dict.get("p_entry", ""),
            "drop_pct":     signal_dict.get("drop_pct", ""),
            "drop_secs":    signal_dict.get("drop_secs", ""),
        })
        self._write(row)

    def log_entry(
        self,
        ticker: str,
        game_state,
        direction: int,      # +1 = YES, -1 = NO
        entry_price: float,
        model_prob: float,
        edge: float,
        qty: int,
    ):
        """Exit-machine opened a position."""
        row = self._base_row("ENTRY")
        row["ticker"] = ticker
        row.update(self._game_state_fields(game_state))
        row.update({
            "model_prob":  round(model_prob, 4),
            "kalshi_price": round(entry_price, 4),
            "edge":         round(edge, 4),
            "direction":    "YES" if direction > 0 else "NO",
            "qty":          qty,
            "entry_price":  round(entry_price, 4),
        })
        self._write(row)

    def log_exit(
        self,
        ticker: str,
        game_state,
        pos: dict,
        exit_price: float,
        reason: str,
        gross: float,
        net_pnl: float,
        hold_secs: float,
        qty: int,
    ):
        """Exit-machine closed a position."""
        row = self._base_row("EXIT")
        row["ticker"] = ticker
        row.update(self._game_state_fields(game_state))
        row.update({
            "direction":   "YES" if pos.get("direction", 1) > 0 else "NO",
            "qty":         qty,
            "entry_price": round(pos.get("entry_price", 0), 4),
            "exit_price":  round(exit_price, 4),
            "gross":       round(gross, 4),
            "net_pnl":     round(net_pnl, 4),
            "hold_secs":   round(hold_secs),
            "reason":      reason,
        })
        self._write(row)

    def log_guard(self, ticker: str, game_state, reason: str):
        """Engine guard event: suppressed entry, halt, forced flatten."""
        row = self._base_row("GUARD")
        row["ticker"] = ticker
        row.update(self._game_state_fields(game_state))
        row["reason"] = reason
        self._write(row)

    def log_order(self, order, game_state=None):
        """Order placed or cancelled by the execution layer."""
        row = self._base_row("ORDER")
        row.update(self._game_state_fields(game_state))
        row.update({
            "ticker":       getattr(order, "ticker", ""),
            "order_id":     getattr(order, "order_id", "") or "",
            "order_side":   getattr(order, "side", ""),
            "order_action": getattr(order, "action", ""),
            "order_type":   getattr(order, "order_type", ""),
            "order_status": getattr(order, "status", ""),
            "qty":          getattr(order, "qty", ""),
            "entry_price":  getattr(order, "limit_price", "") or "",
        })
        self._write(row)

    def path(self) -> Path:
        return self._path

    def close(self):
        with self._lock:
            self._fh.flush()
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
