"""
model_edge.py
=============
Example model-vs-market edge strategy, a plug-in for the ExecutionEngine.

Pure decision layer implementing the core.interfaces.Strategy protocol:
  on_poll(ctx)  -> [Intent]      decisions only; no orders, no exchange access
  on_entry(pos)                  engine confirms a fill opened a position
  on_exit(trade)                 engine confirms a fill closed it

Decision logic (parameters from strategy/example_params.json, illustrative):

  FLAT     |edge| >= entry_edge          -> Intent(enter, sign of edge)
  HOLDING  directed_edge <= exit_edge    -> Intent(exit 'converge')
           gain >= tp_cents              -> Intent(exit 'tp')
           gain <= -sl_cents             -> Intent(exit 'stop')
           held >= max_hold_secs         -> Intent(exit 'time')

Plus a PERSISTENT-EDGE GOVERNOR (lesson from a live session): if the edge sits
beyond the entry threshold for edge_persist_limit_secs without ever converging,
the right inference is "a model input is wrong", not "free money keeps
appearing". Entries are suspended until the edge drops back inside the
threshold. In the session that motivated it, a contaminated pregame input kept
the model 10-15c away from the market for two hours; this governor would have
stood the strategy down about 15 minutes in.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot
boot()

from core.interfaces import Intent, PositionState, StrategyContext   # noqa: E402
from core.model_signal import ModelSignalEngine                      # noqa: E402

EDGE_PERSIST_LIMIT_SECS = 900   # 15 min of unconverged edge -> distrust the model


class ModelEdgeStrategy:
    """Model-vs-market edge strategy. One instance per game."""

    name = "model_edge"
    verbose = True   # set False for batch backtests to silence per-poll prints

    def __init__(self, signal_engine: ModelSignalEngine | None = None,
                 edge_persist_limit_secs: float = EDGE_PERSIST_LIMIT_SECS,
                 cfg: dict | None = None):
        self.engine = signal_engine or ModelSignalEngine()
        # cfg override lets the backtester sweep parameters; defaults to the
        # engine's loaded cfg.
        self.cfg = cfg if cfg is not None else self.engine.cfg
        self.edge_persist_limit = edge_persist_limit_secs

        self._cooldown_until = 0.0
        self._edge_streak_start: float | None = None   # when |edge| first exceeded entry
        self._governor_tripped = False

    # ── Strategy protocol ─────────────────────────────────────────────────────

    def on_poll(self, ctx: StrategyContext) -> list[Intent]:
        if ctx.game_state is None:
            return []

        model_home = self.engine.model_prob_home(ctx.game_state, ctx.pregame_prob)
        edge = model_home - ctx.yes_price
        now = ctx.ts

        if self.verbose:
            print(f"      model={model_home:.2f} edge={edge:+.3f}"
                  f"{'  [governor: model distrusted]' if self._governor_tripped else ''}",
                  flush=True)

        # ── persistent-edge governor bookkeeping ──────────────────────────────
        if abs(edge) >= self.cfg["entry_edge"]:
            if self._edge_streak_start is None:
                self._edge_streak_start = now
            elif (now - self._edge_streak_start) >= self.edge_persist_limit \
                    and not self._governor_tripped:
                self._governor_tripped = True
                if self.verbose:
                    print(f"      [governor] edge has persisted "
                          f"{(now - self._edge_streak_start) / 60:.0f}m without converging "
                          f"— suspending new entries (model input suspect)", flush=True)
        else:
            self._edge_streak_start = None
            if self._governor_tripped:
                self._governor_tripped = False
                if self.verbose:
                    print("      [governor] edge converged — entries re-enabled", flush=True)

        # ── HOLDING: evaluate exits ────────────────────────────────────────────
        if ctx.position is not None:
            pos = ctx.position
            gain = pos.direction * (ctx.yes_price - pos.entry_price)
            held = now - pos.entry_ts
            directed_edge = pos.direction * edge

            reason = None
            if directed_edge <= self.cfg["exit_edge"]:
                reason = "converge"
            elif gain >= self.cfg["tp_cents"]:
                reason = "tp"
            elif gain <= -self.cfg["sl_cents"]:
                reason = "stop"
            elif held >= self.cfg["max_hold_secs"]:
                reason = "time"

            if reason:
                return [Intent(kind="exit", reason=reason,
                               meta={"model_prob": model_home, "edge": edge})]
            return []

        # ── FLAT: evaluate entry ───────────────────────────────────────────────
        if ctx.entries_blocked or self._governor_tripped:
            return []
        if now < self._cooldown_until:
            return []
        if abs(edge) >= self.cfg["entry_edge"]:
            direction = 1 if edge > 0 else -1
            return [Intent(kind="enter", direction=direction,
                           reason=f"edge {edge:+.3f}",
                           meta={"model_prob": model_home, "edge": edge})]
        return []

    def on_entry(self, position: PositionState) -> None:
        pass   # engine owns position state; nothing to track here

    def on_exit(self, trade: dict) -> None:
        # Use the exit's own timestamp so the cooldown runs on the SAME clock as
        # on_poll's ctx.ts — under replay ctx.ts is simulated game time, and
        # time.time() would pin the cooldown permanently in the future.
        exit_epoch = trade.get("exit_epoch", time.time())
        self._cooldown_until = exit_epoch + self.cfg["cooldown_secs"]
