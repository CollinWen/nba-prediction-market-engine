"""
engine.py
=========
Centralized execution engine: one engine, pluggable strategies.

The engine owns everything a strategy should not have to think about:

  * the poll loop (game feed + tick prices + REST quotes)
  * pregame-probability persistence across restarts
  * trading guards   — stale PBP clock, game over, market closed, order failures
  * order routing    — YES-frame pricing, fill confirmation before any position
                       is booked, cancel/reprice of unfilled limits
  * position state   — from confirmed fills only (Kalshi is the source of record)
  * risk             — via execution.risk through the OrderManager
  * the blotter      — every order, fill, guard event and round trip

A strategy (core.interfaces.Strategy) is a pure decision layer: it gets a
StrategyContext snapshot each poll and returns Intents. strategy/model_edge.py
is the first implementation; backtests can drive the same interface offline.

June 8 post-mortem informed every guard here: the session churned through
halftime on a frozen clock, zombie-traded for 2.5 hours after the final
buzzer, kept sending orders into a settled market (404 storm), and booked
P&L for orders that never filled while 47 real contracts accumulated unseen.
"""
from __future__ import annotations

import json
import time
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot
boot()

import config                                      # noqa: E402
from core.interfaces import (                      # noqa: E402
    GameState, Order, Intent, PositionState, StrategyContext, Strategy,
)

ENG = config.ENGINE


class ExecutionEngine:
    """Drives one game session for one strategy on one market."""

    def __init__(
        self,
        game_id: str,
        home: str,
        home_ticker: str,
        strategy: Strategy,
        feed,                       # core.nba_feed.NBAFeed
        client,                     # core.kalshi_client.KalshiClient
        order_manager,              # execution.order_manager.OrderManager
        tracker=None,               # portfolio.tracker.Tracker (reconcile)
        blotter=None,               # blotter.session_log.SessionLog
        qty: int = 1,
        dry_run: bool = True,
        pregame_override: float | None = None,
    ):
        self.game_id = game_id
        self.home = home
        self.home_ticker = home_ticker
        self.strategy = strategy
        self.feed = feed
        self.client = client
        self.om = order_manager
        self.tracker = tracker
        self.blotter = blotter
        self.qty = qty
        self.dry_run = dry_run

        self.tick_path = config.TICK_DIR / f"{home_ticker}.parquet"
        self.state_path = config.STATE_DIR / f"pregame_{game_id}.json"

        self._pregame: float | None = None
        self._pregame_override = pregame_override
        self._pos: PositionState | None = None
        self._trades: list[dict] = []

        # guard state
        self._last_clock: tuple | None = None
        self._stale_polls = 0
        self._game_over_polls = 0
        self._order_failures = 0
        self._poll_count = 0
        self._halted = False
        self._halt_reason = ""
        self._seen_pre_tip = False

    # ── market data ──────────────────────────────────────────────────────────

    def _latest_price(self) -> float | None:
        if not self.tick_path.exists():
            return None
        try:
            df = pd.read_parquet(self.tick_path)
            if df.empty:
                return None
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            return float(df.sort_values("ts")["price"].iloc[-1])
        except Exception:
            return None

    def _quote(self):
        try:
            return self.client.quote(self.home_ticker)
        except Exception:
            return None

    # ── pregame probability (persisted; June 8 contamination fix) ────────────

    def _load_or_lock_pregame(self, price: float, in_game: bool) -> float:
        if self._pregame is not None:
            return self._pregame

        if self._pregame_override is not None:
            self._pregame = round(round(self._pregame_override / 0.05) * 0.05, 2)
            self._save_pregame("cli_override")
            print(f"  [engine] pregame {self.home} prob = {self._pregame:.2f} (CLI override)",
                  flush=True)
            return self._pregame

        if self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                self._pregame = float(saved["pregame_prob"])
                print(f"  [engine] pregame {self.home} prob = {self._pregame:.2f} "
                      f"(restored from {self.state_path.name}, "
                      f"locked {saved.get('locked_at', '?')})", flush=True)
                return self._pregame
            except Exception as e:
                print(f"  [engine] pregame state file unreadable ({e}); re-deriving", flush=True)

        if in_game and not self._seen_pre_tip:
            # Restarted mid-game with no saved state: the current price is
            # contaminated by in-game information (this corrupted the model
            # all night on June 8). Try the tick history before tip-off.
            recovered = self._recover_pregame_from_ticks()
            if recovered is not None:
                self._pregame = recovered
                self._save_pregame("tick_recovery")
                print(f"  [engine] pregame {self.home} prob = {self._pregame:.2f} "
                      f"(recovered from pre-tip ticks)", flush=True)
                return self._pregame
            print("  [engine] WARNING: restarted mid-game, no saved pregame prob and "
                  "no pre-tip ticks. Falling back to CURRENT price — model feature "
                  "is suspect. Pass --pregame to fix.", flush=True)

        self._pregame = round(round(price / 0.05) * 0.05, 2)
        self._save_pregame("tipoff_lock")
        print(f"  [engine] pregame {self.home} prob locked at {self._pregame:.2f}", flush=True)
        return self._pregame

    def _save_pregame(self, source: str):
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "game_id": self.game_id,
                "pregame_prob": self._pregame,
                "locked_at": dt.datetime.now().isoformat(timespec="seconds"),
                "source": source,
            }))
        except Exception as e:
            print(f"  [engine] could not persist pregame prob: {e}", flush=True)

    def _recover_pregame_from_ticks(self) -> float | None:
        """Last traded price before the first PBP action = the true pregame line."""
        try:
            import requests
            url = config.NBA_S3_PBP.format(self.game_id)
            r = requests.get(url, headers=config.HTTP_HEADERS, timeout=10)
            if r.status_code != 200:
                return None
            acts = [a for a in r.json().get("game", {}).get("actions", [])
                    if a.get("timeActual")]
            if not acts:
                return None
            tip = pd.to_datetime(acts[0]["timeActual"], utc=True)

            df = pd.read_parquet(self.tick_path)
            if df.empty:
                return None
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            pre = df[df["ts"] < tip]
            if pre.empty:
                return None
            last = float(pre.sort_values("ts")["price"].iloc[-1])
            return round(round(last / 0.05) * 0.05, 2)
        except Exception:
            return None

    # ── startup reconciliation (Kalshi truth -> position state) ──────────────

    def reconcile(self):
        if self.tracker is None:
            print("  [engine] RECONCILE: no tracker — starting FLAT", flush=True)
            return
        try:
            positions = self.tracker.positions()
        except Exception as e:
            print(f"  [engine] RECONCILE failed ({e}) — starting FLAT", flush=True)
            return
        for p in positions:
            if p.ticker != self.home_ticker:
                continue
            direction = 1 if p.side == "yes" else -1
            entry_yes = p.avg_price if p.side == "yes" else (1.0 - p.avg_price)
            self._pos = PositionState(
                direction=direction, qty=p.qty,
                entry_price=round(entry_yes, 4), entry_ts=time.time(),
            )
            print(f"  [engine] RECONCILE: holding {'YES' if direction > 0 else 'NO'} "
                  f"x{p.qty} @ yes-frame {entry_yes:.2f} — starting in HOLDING",
                  flush=True)
            return
        print("  [engine] RECONCILE: no open position — starting FLAT", flush=True)

    # ── guards ────────────────────────────────────────────────────────────────

    def _update_guards(self, gs: GameState | None) -> tuple[bool, str]:
        """Returns (entries_blocked, reason). May set self._halted."""
        if gs is None:
            return True, "pre-tip"

        clock = (gs.period, gs.secs_remaining)
        if clock == self._last_clock:
            self._stale_polls += 1
        else:
            self._stale_polls = 0
        self._last_clock = clock

        if gs.period >= 4 and gs.secs_remaining <= 0:
            self._game_over_polls += 1
        else:
            self._game_over_polls = 0

        if self._game_over_polls >= ENG.game_over_polls:
            self._halt("game over (Q4+ 0:00 sustained)")
            return True, "game over"

        # periodic market-status check (cheap insurance against zombie sessions)
        if self._poll_count % ENG.market_check_polls == 0 and self._poll_count > 0:
            if not self._market_active():
                self._halt("market no longer active on Kalshi")
                return True, "market closed"

        if self._stale_polls >= ENG.stale_clock_polls:
            mins = self._stale_polls * ENG.poll_secs / 60
            return True, f"clock frozen {mins:.0f}m (halftime/timeout/feed stall)"

        return False, ""

    def _market_active(self) -> bool:
        try:
            m = self.client.get_market(self.home_ticker)
            return (m.get("status") or "").lower() in ("active", "open", "")
        except Exception:
            return True   # don't halt on a read hiccup

    def _halt(self, reason: str):
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            print(f"  [engine] HALT: {reason}", flush=True)
            if self.blotter:
                self.blotter.log_guard(self.home_ticker, None, f"HALT: {reason}")

    # ── order execution with fill confirmation ────────────────────────────────

    def _await_fill(self, order: Order) -> int:
        """Poll order status until filled, canceled, or timeout. Returns filled qty."""
        if order.status in ("rejected", None):
            return 0
        if self.dry_run:
            return order.qty   # paper mode: assume the limit fills

        deadline = time.time() + ENG.fill_timeout_secs
        while time.time() < deadline:
            try:
                raw = self.client.get_order(order.order_id)
            except Exception:
                time.sleep(ENG.fill_poll_secs)
                continue
            filled = self.client.filled_qty(raw)
            status = (raw.get("status") or "").lower()
            if filled >= order.qty or status == "executed":
                return max(filled, order.qty if status == "executed" else filled)
            if status in ("canceled", "cancelled"):
                return filled
            time.sleep(ENG.fill_poll_secs)

        # timeout: cancel whatever is still resting
        try:
            self.om.cancel(order.order_id)
        except Exception:
            pass
        try:
            raw = self.client.get_order(order.order_id)
            return self.client.filled_qty(raw)
        except Exception:
            return 0

    def _send(self, side: str, action: str, qty: int, yes_price: float) -> Order:
        order = Order(
            ticker=self.home_ticker, side=side, action=action, qty=qty,
            order_type="limit",
            limit_price=round(min(0.99, max(0.01, yes_price)), 2),
        )
        placed = self.om.place(order)
        if self.blotter:
            self.blotter.log_order(placed)
        if placed.status == "rejected":
            self._order_failures += 1
            if self._order_failures >= ENG.max_order_failures:
                self._halt(f"{self._order_failures} consecutive order failures")
        else:
            self._order_failures = 0
        return placed

    def _execute_entry(self, intent: Intent, ctx: StrategyContext) -> bool:
        qty = intent.qty or self.qty   # sizing hook: Kelly/bankroll logic goes here
        side = "yes" if intent.direction > 0 else "no"

        # Marketable YES-frame limit: buying YES lifts the ask; buying NO hits
        # the bid (we acquire NO at 1 - yes_bid).
        if ctx.quote and ctx.quote.bid and ctx.quote.ask:
            yes_px = ctx.quote.ask if intent.direction > 0 else ctx.quote.bid
        else:
            yes_px = ctx.yes_price

        placed = self._send(side, "buy", qty, yes_px)
        filled = self._await_fill(placed)
        if filled <= 0:
            print(f"  [engine] entry NOT filled ({side} x{qty} @ {yes_px:.2f}) — staying flat",
                  flush=True)
            return False

        self._pos = PositionState(
            direction=intent.direction, qty=filled,
            entry_price=yes_px, entry_ts=time.time(),
            entry_period=ctx.game_state.period if ctx.game_state else 0,
            entry_secs=ctx.game_state.secs_remaining if ctx.game_state else 0.0,
            meta=dict(intent.meta),
        )
        side_str = "YES" if intent.direction > 0 else "NO"
        print(f"  >>> ENTER {side_str} {self.home} x{filled} @ {yes_px:.2f} "
              f"({intent.reason}) [FILLED]", flush=True)
        if self.blotter:
            self.blotter.log_entry(
                self.home_ticker, ctx.game_state, intent.direction, yes_px,
                intent.meta.get("model_prob", 0.0), intent.meta.get("edge", 0.0), filled,
            )
        self.strategy.on_entry(self._pos)
        return True

    def _execute_exit(self, intent: Intent, ctx: StrategyContext) -> bool:
        if self._pos is None:
            return False
        pos = self._pos
        side = "yes" if pos.direction > 0 else "no"

        # Marketable YES-frame limit: selling YES hits the bid; selling NO
        # lifts the ask (NO sells at 1 - yes_ask).
        if ctx.quote and ctx.quote.bid and ctx.quote.ask:
            yes_px = ctx.quote.bid if pos.direction > 0 else ctx.quote.ask
        else:
            yes_px = ctx.yes_price

        placed = self._send(side, "sell", pos.qty, yes_px)
        filled = self._await_fill(placed)

        if filled < pos.qty and not self._halted:
            # one more try, crossing further through the book
            cross = ENG.exit_cross_extra * (-1 if pos.direction > 0 else 1)
            retry_px = yes_px + cross
            remaining = pos.qty - filled
            print(f"  [engine] exit partial ({filled}/{pos.qty}); retrying "
                  f"{remaining} @ {retry_px:.2f}", flush=True)
            placed2 = self._send(side, "sell", remaining, retry_px)
            filled += self._await_fill(placed2)

        if filled <= 0:
            print(f"  [engine] exit NOT filled — still holding; will retry next poll",
                  flush=True)
            return False

        gain = pos.direction * (yes_px - pos.entry_price)
        held = time.time() - pos.entry_ts
        trade = {
            "entry_ts": dt.datetime.fromtimestamp(pos.entry_ts).strftime("%H:%M:%S"),
            "exit_ts": dt.datetime.now().strftime("%H:%M:%S"),
            "exit_epoch": time.time(),
            "direction": pos.direction, "qty": filled,
            "entry_price": pos.entry_price, "exit_price": yes_px,
            "gross": round(gain * filled, 4), "net": round(gain * filled, 4),
            "reason": intent.reason, "held_s": round(held),
            "entry_period": pos.entry_period, "entry_secs": pos.entry_secs,
        }
        self._trades.append(trade)
        print(f"  <<< EXIT x{filled} @ {yes_px:.2f} [{intent.reason}] "
              f"gross={gain * filled:+.3f} held={int(held)}s [FILLED]", flush=True)
        if self.blotter:
            self.blotter.log_exit(
                self.home_ticker, ctx.game_state,
                {"direction": pos.direction, "entry_price": pos.entry_price},
                yes_px, intent.reason, gain * filled, gain * filled, held, filled,
            )

        if filled >= pos.qty:
            self._pos = None
        else:
            pos.qty -= filled   # partial: keep the remainder, strategy retries
        self.strategy.on_exit(trade)
        return True

    def flatten(self, ctx: StrategyContext | None = None, reason: str = "flatten"):
        """Close any open position at market (used on game over / shutdown)."""
        if self._pos is None:
            return
        if ctx is None:
            ctx = self._build_ctx(self._latest_price() or self._pos.entry_price, None)
        self._execute_exit(Intent(kind="exit", reason=reason), ctx)

    # ── poll loop ─────────────────────────────────────────────────────────────

    def _build_ctx(self, price: float, gs: GameState | None,
                   blocked: bool = False, block_reason: str = "") -> StrategyContext:
        return StrategyContext(
            ts=time.time(), yes_price=price, quote=self._quote(),
            game_state=gs, position=self._pos,
            pregame_prob=self._pregame if self._pregame is not None else 0.5,
            entries_blocked=blocked, block_reason=block_reason,
        )

    def step(self) -> bool:
        """One poll cycle. Returns False when the session should stop."""
        self._poll_count += 1
        ts = dt.datetime.now().strftime("%H:%M:%S")

        price = self._latest_price()
        if price is None:
            print(f"  [{ts}] waiting for tick file: {self.tick_path.name}", flush=True)
            return True

        gs = self.feed.game_state(self.game_id)
        if gs is None:
            self._seen_pre_tip = True
            print(f"  [{ts}] pre-tip ({self.home} px={price:.2f})", flush=True)
            return True

        self._load_or_lock_pregame(price, in_game=True)

        blocked, reason = self._update_guards(gs)

        if self._halted:
            self.flatten(reason=f"halt: {self._halt_reason}")
            return False

        ctx = self._build_ctx(price, gs, blocked, reason)
        try:
            intents = self.strategy.on_poll(ctx)
        except Exception as e:
            print(f"  [{ts}] strategy error: {e}", flush=True)
            return True

        status = f"  [{ts}] Q{gs.period} {int(gs.secs_remaining // 60)}:" \
                 f"{int(gs.secs_remaining % 60):02d} sd={gs.score_diff:+d} | " \
                 f"{self.home} px={price:.2f}"
        if blocked and self._pos is None:
            status += f"  [entries blocked: {reason}]"
        print(status, flush=True)

        for intent in intents:
            if intent.kind == "enter":
                if blocked:
                    if self.blotter:
                        self.blotter.log_guard(self.home_ticker, gs,
                                               f"entry suppressed: {reason}")
                    continue
                if self._pos is None:
                    self._execute_entry(intent, ctx)
            elif intent.kind == "exit":
                self._execute_exit(intent, ctx)

        return not self._halted

    def run(self, until: dt.datetime):
        print(
            f"  [engine] strategy={self.strategy.name} {self.home} "
            f"(game {self.game_id})\n"
            f"  guards: stale_clock={ENG.stale_clock_polls} polls, "
            f"game_over={ENG.game_over_polls} polls, "
            f"order_failures={ENG.max_order_failures}, "
            f"fill_timeout={ENG.fill_timeout_secs:.0f}s\n"
            f"  Polling every {ENG.poll_secs:.0f}s until {until:%Y-%m-%d %H:%M}.\n",
            flush=True,
        )
        self.reconcile()
        try:
            while dt.datetime.now() < until:
                try:
                    if not self.step():
                        break
                except Exception as e:
                    print(f"  [engine] step error: {e}", flush=True)
                time.sleep(ENG.poll_secs)
        except KeyboardInterrupt:
            print("  [engine] Ctrl-C", flush=True)

        if self._pos is not None:
            print("  [engine] open position at shutdown — flattening", flush=True)
            self.flatten(reason="session end")
        self._print_summary()

    # ── summary ───────────────────────────────────────────────────────────────

    def _print_summary(self):
        n = len(self._trades)
        print(f"\n=== Engine summary [{self.strategy.name}]: {n} round-trip(s) ===",
              flush=True)
        if self._halt_reason:
            print(f"  session halted: {self._halt_reason}", flush=True)
        if not self._trades:
            print("  No trades.", flush=True)
            return
        df = pd.DataFrame(self._trades)
        wins = (df["net"] > 0).sum()
        print(f"  {wins}W / {n - wins}L  |  net (assumed-at-limit): "
              f"{df['net'].sum():+.3f}", flush=True)
        for t in self._trades:
            side = "YES" if t["direction"] > 0 else "NO"
            print(f"  {t['entry_ts']} {side} x{t['qty']} @ {t['entry_price']:.2f} -> "
                  f"{t['exit_ts']} @ {t['exit_price']:.2f} [{t['reason']}] "
                  f"net={t['net']:+.3f} held={t['held_s']}s", flush=True)
        print("\n  Run blotter/reconcile_fills.py for fill-true realized P&L.",
              flush=True)

    @property
    def trades(self) -> list[dict]:
        return list(self._trades)

    @property
    def open_position(self) -> PositionState | None:
        return self._pos
