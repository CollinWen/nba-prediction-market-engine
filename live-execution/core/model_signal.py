"""
model_signal.py
===============
Win-probability + trading-edge signal engine (public baseline).

Implements the `SignalEngine` protocol from `core.interfaces`:
  * model_prob_home(game_state, pregame_prob) -> float   P(home win)
  * signal(ticker, game_state, quote, pregame_prob) -> Signal

NOTE: the private research version of this module runs a gradient-boosted
model trained on several seasons of play-by-play. This public build ships a
closed-form baseline instead, so the engine, replay and terminal run end to
end without any proprietary model or data:

    The final margin is treated as Normal. The pregame probability implies a
    point spread s = SIGMA * Phi^-1(p_pregame); with fraction f of regulation
    left, the remaining margin has mean s*f and std SIGMA*sqrt(f), so

        P(home win) = Phi( (score_diff + s*f) / (SIGMA * sqrt(f)) )

This is the classic random-walk model of an NBA game (Stern, 1994). Any model
that maps game state -> P(home win) can be dropped in behind the same API.

Edge convention
---------------
edge = model_prob_home - kalshi_price  (both home-side, in dollars 0-1)
Thresholds come from strategy/example_params.json (illustrative values).
"""
from __future__ import annotations

import json
import math
import re
import sys
import datetime as dt
from pathlib import Path
from statistics import NormalDist
from typing import Optional

import requests

# ── Import bootstrap (see _bootstrap.py) ────────────────────────────────────────
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))   # .../live-execution
from _bootstrap import boot  # noqa: E402
boot()

from core.interfaces import GameState, Quote, Signal  # noqa: E402
from config import STRATEGY_CFG, NBA_S3_PBP, HTTP_HEADERS  # noqa: E402

SIGMA = 13.0          # std of a full-game final margin, in points (ballpark)
REG_SECS = 2880.0     # 4 x 12-minute quarters
_N = NormalDist()
_CLOCK_RE = re.compile(r"PT(\d+)M(\d+(?:\.\d+)?)S")


def _clock_to_secs_remaining(period, clock: str) -> Optional[float]:
    m = _CLOCK_RE.match(str(clock))
    if not m:
        return None
    try:
        period = int(period)
    except (TypeError, ValueError):
        return None
    return int(m.group(1)) * 60 + int(float(m.group(2))) + max(0, 4 - period) * 720


def win_prob(score_diff: float, secs_remaining: float, pregame_prob: float) -> float:
    """Closed-form P(home win) from margin, time left and the pregame prior."""
    p0 = min(0.98, max(0.02, pregame_prob))
    spread = SIGMA * _N.inv_cdf(p0)
    frac = max(secs_remaining, 1.0) / REG_SECS
    z = (score_diff + spread * frac) / (SIGMA * math.sqrt(frac))
    return min(0.995, max(0.005, _N.cdf(z)))


class ModelSignalEngine:
    """Turns game state into P(home win) and a directional edge vs the market."""

    def __init__(self, cfg_path: Path | None = None):
        with open(cfg_path or STRATEGY_CFG) as f:
            self.cfg: dict = json.load(f)

    # ── Feature construction ────────────────────────────────────────────────────
    def _features_from_pbp(self, game_state: GameState, pregame_prob: float) -> Optional[dict]:
        """Rebuild state from live NBA play-by-play; None if unavailable (pre-tip)."""
        url = NBA_S3_PBP.format(game_state.game_id)
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        payload = r.json()
        acts = [x for x in payload.get("game", {}).get("actions", []) if x.get("timeActual")]
        if not acts:
            return None
        home_tri = _infer_home_tricode(payload, game_state.home)
        return build_features_from_actions(acts, pregame_prob, home_tri)

    def _features_from_state(self, game_state: GameState, pregame_prob: float) -> dict:
        return {
            "pregame_home_winprob": pregame_prob,
            "score_diff": float(game_state.score_diff),
            "secs_remaining": float(game_state.secs_remaining),
            "period": int(game_state.period),
        }

    def prob_from_features(self, feat: dict) -> float:
        return win_prob(feat["score_diff"], feat["secs_remaining"], feat["pregame_home_winprob"])

    # ── SignalEngine protocol ───────────────────────────────────────────────────
    def model_prob_home(self, game_state: GameState, pregame_prob: float) -> float:
        feat = self._features_from_pbp(game_state, pregame_prob)
        if feat is None:
            feat = self._features_from_state(game_state, pregame_prob)
        return self.prob_from_features(feat)

    def signal(self, ticker: str, game_state: GameState, quote: Quote,
               pregame_prob: float) -> Signal:
        model_home = self.model_prob_home(game_state, pregame_prob)
        kalshi_price = quote.mid if quote.mid else quote.last
        edge = model_home - kalshi_price
        entry_edge, exit_edge = self.cfg["entry_edge"], self.cfg["exit_edge"]

        if abs(edge) >= entry_edge:
            action = "enter_yes" if edge > 0 else "enter_no"
            reason = f"|edge|={abs(edge):.3f} >= entry_edge={entry_edge}"
        elif abs(edge) <= exit_edge:
            action = "exit"
            reason = f"|edge|={abs(edge):.3f} <= exit_edge={exit_edge}"
        else:
            action = "hold"
            reason = f"|edge|={abs(edge):.3f} in ({exit_edge}, {entry_edge})"

        return Signal(ticker=ticker, ts=dt.datetime.now(dt.timezone.utc),
                      model_prob_home=model_home, kalshi_price=kalshi_price,
                      edge=edge, action=action, reason=reason)


def _infer_home_tricode(pbp_json: dict, fallback: str) -> str:
    """Best-effort home tricode from the PBP payload; else the GameState value."""
    ht = pbp_json.get("game", {}).get("homeTeam")
    if isinstance(ht, dict) and ht.get("teamTricode"):
        return ht["teamTricode"]
    return fallback


def build_features_from_actions(acts: list[dict], pregame_prob: float,
                                home_tri: str) -> Optional[dict]:
    """Game state as of the LAST action in ``acts``.

    Live use passes the full play-by-play; backtest replay passes the actions
    truncated to each poll time, so both paths compute identical inputs.
    Returns None if no usable rows (caller falls back to state-only).
    """
    for a in reversed(acts):
        secs = _clock_to_secs_remaining(a.get("period"), a.get("clock", ""))
        if secs is None:
            continue
        try:
            sh, sa = float(a.get("scoreHome") or 0), float(a.get("scoreAway") or 0)
            period = int(a.get("period"))
        except (TypeError, ValueError):
            continue
        return {"pregame_home_winprob": pregame_prob, "score_diff": sh - sa,
                "secs_remaining": float(secs), "period": period}
    return None


if __name__ == "__main__":
    eng = ModelSignalEngine()
    for diff, secs in [(0, 2880), (5, 1440), (5, 120), (-3, 30)]:
        print(f"diff={diff:+d} secs_left={secs:>4}  P(home)={win_prob(diff, secs, 0.60):.3f}")
