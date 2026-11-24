"""
registry.py
===========
Named catalog of pluggable strategies (core.interfaces.Strategy implementations).

One registry serves three consumers:
  * execution/engine.py  — live trading picks a strategy by key
  * backtest/session_replay.py — replays pass `spec.factory` as strategy_factory
  * dashboard widgets    — dropdowns list `all_specs()`, run either path

Each spec is a *factory*: it builds a fresh strategy instance per game/session
around whatever signal engine the caller owns (live ModelSignalEngine or the
replay's ReplayModelEngine). cfg_fn lets a variant derive its parameters from
the base cfg (strategy/example_params.json) instead of hardcoding values
that drift when the base cfg is retuned.

Adding a strategy: implement the Strategy protocol, then register a StrategySpec
here. The terminal picks it up automatically.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import boot
boot()

from strategy.model_edge import ModelEdgeStrategy   # noqa: E402


@dataclass
class StrategySpec:
    key: str
    label: str
    description: str
    cfg_fn: Optional[Callable[[dict], dict]] = None   # base cfg -> variant cfg
    builder: Optional[Callable] = None                # custom (engine, cfg) -> Strategy
    extra: dict = field(default_factory=dict)

    def factory(self, signal_engine=None):
        """Build a fresh strategy instance (quiet — no per-poll prints)."""
        if self.builder is not None:
            strat = self.builder(signal_engine, self.cfg_fn)
        else:
            from core.model_signal import ModelSignalEngine
            engine = signal_engine or ModelSignalEngine()
            cfg = dict(engine.cfg)
            if self.cfg_fn is not None:
                cfg = self.cfg_fn(cfg)
            strat = ModelEdgeStrategy(signal_engine=engine, cfg=cfg)
        strat.verbose = False
        return strat


def _tight(cfg: dict) -> dict:
    out = dict(cfg)
    out["entry_edge"] = round(cfg["entry_edge"] + 0.02, 3)   # demand 2c more edge
    return out


def _loose(cfg: dict) -> dict:
    out = dict(cfg)
    out["entry_edge"] = round(max(0.02, cfg["entry_edge"] - 0.02), 3)
    return out


def _fast_exit(cfg: dict) -> dict:
    out = dict(cfg)
    out["max_hold_secs"] = max(60, int(cfg["max_hold_secs"] / 2))
    out["tp_cents"] = round(max(0.02, cfg["tp_cents"] / 2), 3)
    return out


_REGISTRY: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> None:
    _REGISTRY[spec.key] = spec


def get(key: str) -> StrategySpec:
    return _REGISTRY[key]


def all_specs() -> list[StrategySpec]:
    return list(_REGISTRY.values())


def cfg_for(key: str) -> dict:
    """Resolved cfg for a spec WITHOUT building a model engine — reads the base
    cfg straight from strategy/example_params.json and applies the variant's
    cfg_fn. Used by the dashboard to label thresholds."""
    import json
    import config
    base = json.loads(Path(config.STRATEGY_CFG).read_text())
    spec = get(key)
    return spec.cfg_fn(base) if spec.cfg_fn else base


register(StrategySpec(
    key="model_edge",
    label="Model Edge (example params)",
    description="Model-vs-market edge with a mean-reversion exit; "
                "parameters from strategy/example_params.json.",
))
register(StrategySpec(
    key="model_edge_tight",
    label="Model Edge — tight (+2c entry)",
    description="Same exits, but demands 2c more edge to enter. "
                "Fewer trades, higher conviction.",
    cfg_fn=_tight,
))
register(StrategySpec(
    key="model_edge_loose",
    label="Model Edge — loose (−2c entry)",
    description="Enters on 2c less edge. More trades; shows how fast "
                "the edge decays as the entry bar drops.",
    cfg_fn=_loose,
))
register(StrategySpec(
    key="model_edge_scalp",
    label="Model Edge — scalp (half hold/tp)",
    description="Half the max hold and take-profit: tests whether the edge "
                "is captured early or needs the full convergence window.",
    cfg_fn=_fast_exit,
))
