"""
session_replay.py
=================
Offline backtest harness: replay an archived game through a real Strategy under
a configurable execution model.

It REGENERATES the signal (rebuilds model inputs from the play-by-play
truncated to each poll, runs the actual strategy decision logic) and
simulates fills against the recorded Kalshi tick stream. The execution model
(backtest/exec_model.py) is a parameter, so the same recorded game can be
scored under ideal, maker-rest, hybrid, or taker assumptions with explicit
slippage — separating signal quality from execution quality.

Same Strategy protocol as live (core.interfaces.Strategy); this is the offline
twin of execution/engine.py, and mirrors its guards (stale-clock entry block,
game-over flatten/halt).

USAGE
  python live-execution/backtest/session_replay.py \
      --game_id 0022500001 --home PHX --away MIL \
      --home_ticker KXNBAGAME-26JAN10MILPHX-PHX --qty 100 --pregame auto
  (the defaults replay the first synthetic sample game)
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot  # noqa: E402
boot()

import config                                               # noqa: E402
from core.interfaces import GameState, PositionState, StrategyContext  # noqa: E402
from core.model_signal import (ModelSignalEngine,           # noqa: E402
                               build_features_from_actions,
                               _infer_home_tricode)
from core.fees import fee                                   # noqa: E402
from strategy.model_edge import ModelEdgeStrategy           # noqa: E402
from backtest.exec_model import ExecConfig, resolve_fill, default_scenarios  # noqa: E402
import game_data                                            # noqa: E402  (canonical I/O)

ENG = config.ENGINE
PBP_CACHE = config.PBP_JSON_CACHE
PBP_ARCHIVE = config.PBP_ARCHIVE   # parsed PBP archive


# ── model engine that scores from a pre-set PBP slice (no live S3 refetch) ────
class ReplayModelEngine(ModelSignalEngine):
    def __init__(self):
        super().__init__()
        self._acts: list[dict] = []
        self._home_tri = ""

    def set_state(self, acts: list[dict], home_tri: str):
        self._acts = acts
        self._home_tri = home_tri

    def model_prob_home(self, game_state: GameState, pregame_prob: float) -> float:
        pregame_binned = round(round(pregame_prob / 0.05) * 0.05, 2)
        feat = build_features_from_actions(self._acts, pregame_binned, self._home_tri)
        if feat is None:
            feat = self._features_from_state(game_state, pregame_binned)
        return self.prob_from_features(feat)


# ── data loaders (PBP cached so replays are reproducible offline) ─────────────
def _acts_from_archive(game_id: str) -> list[dict] | None:
    """Load parsed PBP from the local season archive (data/cache/s3_pbp), if present.

    The archive parquet uses snake_case columns; remap to the raw-action keys
    build_features_from_actions expects so live and replay share one code path.
    """
    path = PBP_ARCHIVE / f"{game_id}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if df.empty or "ts" not in df.columns:
        return None
    df = df[df["ts"].notna()]
    return [{
        "timeActual": r.ts, "period": r.period, "clock": r.clock,
        "scoreHome": r.score_home, "scoreAway": r.score_away,
        "teamTricode": r.team, "actionType": r.action_type, "shotResult": r.shot_result,
    } for r in df.itertuples()]


def _resolve_home_tri(acts: list[dict], home_fallback: str) -> str:
    seen = {a.get("teamTricode") for a in acts}
    if home_fallback in seen:
        return home_fallback
    return next((t for t in seen if t), home_fallback)


def _load_pbp(game_id: str, home_fallback: str):
    # 1. local season archive (covers the full 2025-26 regular season + playoffs)
    acts = _acts_from_archive(game_id)
    if acts is not None:
        print(f"  PBP from archive: s3_pbp/{game_id}.parquet")
        times = [pd.to_datetime(a["timeActual"], utc=True) for a in acts]
        return acts, times, _resolve_home_tri(acts, home_fallback)

    # 2. json cache  3. live S3 (recent games only)
    PBP_CACHE.mkdir(parents=True, exist_ok=True)
    cache = PBP_CACHE / f"{game_id}.json"
    if cache.exists():
        payload = json.loads(cache.read_text())
        print(f"  PBP from cache: {cache.name}")
    else:
        url = config.NBA_S3_PBP.format(game_id)
        r = requests.get(url, headers=config.HTTP_HEADERS, timeout=20)
        r.raise_for_status()
        payload = r.json()
        cache.write_text(json.dumps(payload))
        print(f"  PBP fetched + cached: {cache.name}")

    acts = [a for a in payload.get("game", {}).get("actions", []) if a.get("timeActual")]
    times = [pd.to_datetime(a["timeActual"], utc=True) for a in acts]
    home_tri = _infer_home_tricode(payload, "") or _resolve_home_tri(acts, home_fallback)
    return acts, times, home_tri


def _load_ticks(home_ticker: str) -> pd.DataFrame:
    # canonical reader: ts normalised to datetime64[ns, UTC], price float, sorted
    return game_data.read_ticks(config.TICK_DIR / f"{home_ticker}.parquet")


def _leg_fee(qty, fill_yes, direction, is_maker):
    side_price = fill_yes if direction > 0 else 1.0 - fill_yes
    return fee(qty, side_price, maker=is_maker, nba=True)


# ── one replay under one ExecConfig ───────────────────────────────────────────
def run_replay(game_id, home, away, home_ticker, pregame, qty,
               acts, act_times, home_tri, ticks, exec_cfg: ExecConfig,
               strategy_cfg: dict | None = None,
               strategy_factory=None):
    """strategy_factory: optional callable(ReplayModelEngine) -> Strategy, so any
    registered strategy (strategy/registry.py) can be replayed, not just the
    default ModelEdgeStrategy."""
    engine = ReplayModelEngine()
    if strategy_factory is not None:
        strat = strategy_factory(engine)
    else:
        strat = ModelEdgeStrategy(signal_engine=engine, cfg=strategy_cfg)

    tip, end = act_times[0], act_times[-1]
    poll_times = pd.date_range(tip, end + pd.Timedelta(seconds=30),
                               freq=f"{int(ENG.poll_secs)}s", tz="UTC")
    # canonical ns epoch ints (read_ticks already normalised ts to ns-UTC)
    tick_ns = game_data.epoch_ns(ticks["ts"]).values
    tick_px = ticks["price"].values

    pos: PositionState | None = None
    entry_fill = 0.0
    entry_maker = False
    trades, poll_rows = [], []
    last_clock = None
    stale = go = 0
    halted = False

    def price_at(T):
        idx = np.searchsorted(tick_ns, T.value) - 1
        return float(tick_px[idx]) if idx >= 0 else np.nan

    def forward(T, n_polls):
        lo = np.searchsorted(tick_ns, T.value, "right")
        hi = np.searchsorted(tick_ns, T.value + int(n_polls * ENG.poll_secs * 1e9), "right")
        return tick_px[lo:hi]

    def close_trade(reason, exit_fill, exit_maker, T):
        net = (pos.direction * (exit_fill - entry_fill) * pos.qty
               - _leg_fee(pos.qty, entry_fill, pos.direction, entry_maker)
               - _leg_fee(pos.qty, exit_fill, pos.direction, exit_maker))
        return {
            "entry_epoch": pos.entry_ts,
            "exit_epoch": T.timestamp(),
            "dir": pos.direction, "qty": pos.qty,
            "entry": entry_fill, "exit": exit_fill, "reason": reason,
            "period": pos.entry_period,
            "gross": pos.direction * (exit_fill - entry_fill) * pos.qty,
            "net": net,
            "entry_maker": entry_maker, "exit_maker": exit_maker,
        }

    for T in poll_times:
        if halted:
            break
        yes_px = price_at(T)
        if np.isnan(yes_px):
            continue
        k = bisect.bisect_right(act_times, T)
        if k == 0:
            continue
        slice_acts = acts[:k]
        last = slice_acts[-1]
        try:
            period = int(last.get("period"))
            sh = int(last.get("scoreHome") or 0)
            sa = int(last.get("scoreAway") or 0)
        except (TypeError, ValueError):
            continue
        engine.set_state(slice_acts, home_tri)
        model_prob = engine.model_prob_home(None, pregame)
        feat = build_features_from_actions(
            slice_acts, round(round(pregame / 0.05) * 0.05, 2), home_tri)
        secs = feat["secs_remaining"] if feat else 0.0
        gs = GameState(game_id=game_id, home=home, away=away, period=period,
                       secs_remaining=secs, score_home=sh, score_away=sa)

        # guards (mirror execution/engine.py)
        clock = (period, secs)
        stale = stale + 1 if clock == last_clock else 0
        last_clock = clock
        go = go + 1 if (period >= 4 and secs <= 0) else 0
        blocked = stale >= ENG.stale_clock_polls

        if go >= ENG.game_over_polls:
            if pos is not None:
                # forced flatten crosses (taker) to guarantee the exit
                yb = pos.direction < 0
                xf = yes_px + exec_cfg.half_spread if yb else yes_px - exec_cfg.half_spread
                trades.append(close_trade("halt_game_over", min(.99, max(.01, xf)), False, T))
                pos = None
            halted = True
            break

        ctx = StrategyContext(ts=T.timestamp(), yes_price=yes_px, quote=None,
                              game_state=gs, position=pos, pregame_prob=pregame,
                              entries_blocked=blocked,
                              block_reason="stale" if blocked else "")
        intents = strat.on_poll(ctx)

        for intent in intents:
            if intent.kind == "enter" and pos is None and not blocked:
                yb = intent.direction > 0     # opening long YES = buy YES
                fres = resolve_fill(exec_cfg, yb, yes_px, forward(T, exec_cfg.maker_wait_polls))
                if fres is None:
                    continue                  # passive order missed -> stay flat, retry
                pos = PositionState(direction=intent.direction, qty=qty,
                                    entry_price=fres.fill_yes, entry_ts=T.timestamp(),
                                    entry_period=period, entry_secs=secs,
                                    meta=dict(intent.meta))
                entry_fill, entry_maker = fres.fill_yes, fres.is_maker
                strat.on_entry(pos)
            elif intent.kind == "exit" and pos is not None:
                yb = pos.direction < 0        # closing long NO = buy YES
                fres = resolve_fill(exec_cfg, yb, yes_px, forward(T, exec_cfg.maker_wait_polls))
                if fres is None:
                    continue                  # couldn't exit passively -> hold, retry
                trades.append(close_trade(intent.reason, fres.fill_yes, fres.is_maker, T))
                strat.on_exit(trades[-1])
                pos = None

        poll_rows.append((T, yes_px, model_prob, period, secs, sh - sa,
                          blocked, pos is not None))

    if pos is not None:
        last_px = price_at(poll_times[-1])
        yb = pos.direction < 0
        xf = last_px + exec_cfg.half_spread if yb else last_px - exec_cfg.half_spread
        trades.append(close_trade("session_end", min(.99, max(.01, xf)), False, poll_times[-1]))

    tdf = pd.DataFrame(trades)
    pdf = pd.DataFrame(poll_rows, columns=["ts", "yes_px", "model", "period",
                                           "secs", "sd", "blocked", "in_pos"])
    return tdf, pdf


# ── reporting ──────────────────────────────────────────────────────────────────
def _summary_row(cfg, tdf):
    if tdf.empty:
        return dict(name=cfg.name, trades=0, net=0.0, wins=0, losses=0,
                    maker_fills=0, gross=0.0)
    return dict(
        name=cfg.name, trades=len(tdf), net=tdf["net"].sum(),
        wins=int((tdf["net"] > 0).sum()), losses=int((tdf["net"] < 0).sum()),
        maker_fills=int(tdf["entry_maker"].sum() + tdf["exit_maker"].sum()),
        gross=tdf["gross"].sum(),
    )


def _plot(game_label, pregame, results, pdf, qty, out):
    DARK, PANEL, TEXT, GRID = "#1a1a2e", "#16213e", "#eaeaea", "#2a2a4a"
    BLUE, PURPLE = "#00d4ff", "#9b59b6"
    palette = ["#2ecc71", "#3498db", "#f39c12", "#e74c3c", "#9b59b6", "#1abc9c"]
    fig = plt.figure(figsize=(16, 10), facecolor=DARK)
    g = fig.add_gridspec(2, 2, hspace=0.3, wspace=0.22)
    axp = fig.add_subplot(g[0, :]); axc = fig.add_subplot(g[1, 0]); axb = fig.add_subplot(g[1, 1])
    for ax in (axp, axc, axb):
        ax.set_facecolor(PANEL); ax.tick_params(colors=TEXT, labelsize=8)
        for sp in ax.spines.values():
            sp.set_edgecolor(GRID)
    fig.suptitle(f"{game_label} — signal regenerated (pregame {pregame:.2f}), "
                 f"{qty}-contract sizing, execution sweep",
                 color=TEXT, fontsize=13, fontweight="bold", y=0.97)

    axp.plot(pdf["ts"], pdf["yes_px"], color=BLUE, lw=1.0, label="Kalshi price")
    axp.plot(pdf["ts"], pdf["model"], color=PURPLE, lw=1.2, ls="--", label="model P(home)")
    axp.set_ylim(0, 1); axp.set_ylabel("P(home win)", color=TEXT, fontsize=8)
    axp.set_title("Price vs regenerated model", color=TEXT, fontsize=10)
    axp.legend(fontsize=8, loc="upper right", facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT)

    for i, (cfg, tdf, _) in enumerate(results):
        if tdf.empty:
            continue
        c = palette[i % len(palette)]
        axc.step(range(1, len(tdf) + 1), tdf["net"].cumsum(), where="post",
                 color=c, lw=1.8, label=f"{cfg.name} -> ${tdf['net'].sum():+.0f}")
    axc.axhline(0, color=GRID, lw=0.8, ls="--")
    axc.set_title(f"Cumulative net P&L ({qty} contracts)", color=TEXT, fontsize=10)
    axc.set_xlabel("Trade #", color=TEXT, fontsize=8); axc.set_ylabel("$", color=TEXT, fontsize=8)
    axc.legend(fontsize=7.5, loc="best", facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT)

    names = [c.name for c, _, _ in results]
    nets = [t["net"].sum() if not t.empty else 0 for _, t, _ in results]
    cols = [palette[i % len(palette)] for i in range(len(results))]
    axb.bar(range(len(names)), nets, color=cols)
    axb.axhline(0, color=GRID, lw=0.8)
    axb.set_xticks(range(len(names)))
    axb.set_xticklabels(names, fontsize=7.5, rotation=20, ha="right")
    axb.set_title("Total net by execution scenario", color=TEXT, fontsize=10)
    axb.set_ylabel("$", color=TEXT, fontsize=8)
    for i, v in enumerate(nets):
        axb.text(i, v, f"${v:+.0f}", ha="center",
                 va="bottom" if v >= 0 else "top", color=TEXT, fontsize=8)

    plt.savefig(out, dpi=140, bbox_inches="tight", facecolor=DARK)
    print(f"\nSaved: {out}")


def _recover_pregame(ticks, tip):
    pre = ticks[ticks["ts"] < tip]
    if pre.empty:
        return 0.5
    return round(round(float(pre["price"].iloc[-1]) / 0.05) * 0.05, 2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Replay a game through a strategy + exec sweep")
    ap.add_argument("--game_id", default="0022500001")
    ap.add_argument("--home", default="PHX")
    ap.add_argument("--away", default="MIL")
    ap.add_argument("--home_ticker", default="KXNBAGAME-26JAN10MILPHX-PHX")
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--pregame", default="auto",
                    help="'auto' (recover from pre-tip ticks) or a float like 0.55")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    print("Loading PBP + ticks...")
    acts, act_times, home_tri = _load_pbp(args.game_id, args.home)
    ticks = _load_ticks(args.home_ticker)
    pregame = (_recover_pregame(ticks, act_times[0]) if args.pregame == "auto"
               else round(round(float(args.pregame) / 0.05) * 0.05, 2))
    print(f"  {len(acts)} actions, {len(ticks)} ticks, home_tri={home_tri}, "
          f"pregame={pregame:.2f}")

    scenarios = default_scenarios()
    results, pdf_ref = [], None
    for cfg in scenarios:
        tdf, pdf = run_replay(args.game_id, args.home, args.away, args.home_ticker,
                              pregame, args.qty, acts, act_times, home_tri, ticks, cfg)
        results.append((cfg, tdf, pdf))
        pdf_ref = pdf

    print(f"\n{'scenario':<18} {'trades':>6} {'net $':>9} {'W/L':>8} "
          f"{'maker fills':>11} {'gross $':>9}")
    print("-" * 70)
    for cfg, tdf, _ in results:
        s = _summary_row(cfg, tdf)
        wl = f"{s['wins']}W/{s['losses']}L"
        print(f"{s['name']:<18} {s['trades']:>6} {s['net']:>+9.2f} {wl:>8} "
              f"{s['maker_fills']:>11} {s['gross']:>+9.2f}")

    label = f"{args.away} @ {args.home} ({args.game_id})"
    out = Path(args.out) if args.out else config.LIVE_DIR / "blotter" / f"replay_{args.game_id}.png"
    _plot(label, pregame, results, pdf_ref, args.qty, out)
