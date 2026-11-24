"""
Offline tests for the dashboard's historical layer (catalog + replay service)
and a smoke test that the modular terminal app builds.

Runs against the synthetic games in sample_data/ (generated on first run).

    python live-execution/test_dashboard.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import boot
boot()

import config
from dashboard import history


def _ensure_sample_data():
    if not any(config.TICK_DIR.glob("KXNBAGAME-*.parquet")):
        script = config.ROOT / "scripts" / "make_sample_data.py"
        subprocess.run([sys.executable, str(script)], check=True)


def test_catalog():
    _ensure_sample_data()
    games = history.CATALOG.games()
    print(f"[catalog] {len(games)} replayable games")
    print("  newest:", ", ".join(g.label for g in games[:4]))
    assert len(games) >= 1, "no replayable games found in DATA_DIR"
    return games


def test_replay():
    g = history.CATALOG.games()[0]
    print(f"[replay] {g.code} game_id={g.game_id} invert={g.invert_ticks}")
    t0 = time.time()
    res = history.REPLAYS._replay_one(g.code, "model_edge", "taker", 100)
    took = time.time() - t0
    tdf, pdf = res["tdf"], res["pdf"]
    net = float(tdf["net"].sum()) if len(tdf) else 0.0
    print(f"  {took:.1f}s — {len(pdf)} polls, {len(tdf)} trades, "
          f"net ${net:+.2f}, pregame {res['pregame']:.2f}")
    assert len(pdf) > 50, "poll frame suspiciously small"
    if len(tdf):
        assert {"entry_epoch", "exit_epoch", "net"} <= set(tdf.columns)
    # cached second call must be instant
    t0 = time.time()
    history.REPLAYS._replay_one(g.code, "model_edge", "taker", 100)
    assert time.time() - t0 < 0.1, "replay cache miss on identical key"
    print("  cache hit OK")
    return res


def test_figures(res):
    from dashboard import panels
    fig = panels.replay_figure(res["pdf"], res["tdf"], "test", 0.08)
    assert fig.data, "replay figure empty"
    eq = panels.equity_figure([res], 100)
    assert eq.data is not None
    panels.replay_trades_table(res["tdf"])
    panels.perf_summary_tiles([res])
    print("[figures] replay/equity/table/tiles build OK")


def test_app_builds():
    from dashboard.mock_providers import MockProviders
    from dashboard.app import create_app
    app = create_app(MockProviders(dry_run=True))
    n_cb = len(app.callback_map)
    print(f"[app] built with mock providers — {n_cb} callbacks registered")
    assert n_cb > 15


if __name__ == "__main__":
    test_catalog()
    res = test_replay()
    test_figures(res)
    test_app_builds()
    print("\nALL DASHBOARD TESTS PASSED")
