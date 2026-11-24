"""
make_sample_data.py
===================
Generate SYNTHETIC games so the replay harness, terminal and tests run without
any real exchange or play-by-play data.

For each game it writes, in the same layout and canonical format the real
archive uses (see game_data.py):

  sample_data/kalshi/ticks/KXNBAGAME-{code}-{HOME}.parquet   ts (ns UTC), price
  sample_data/cache/s3_pbp/{game_id}.parquet                 play-by-play
  sample_data/kalshi/markets/kalshi_nba_mapping.parquet      game_code -> game_id

The game is a simple possession simulation. The market price is the baseline
model's fair value plus a short-lived over-reaction to each scoring play and
some noise. None of this is real market data.

    python scripts/make_sample_data.py            # 6 games, seed 7
    python scripts/make_sample_data.py --games 12 --seed 1
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "live-execution"))
import game_data                                   # noqa: E402
from core.model_signal import win_prob             # noqa: E402

OUT = ROOT / "sample_data"
TEAMS = ["BOS", "NYK", "MIL", "DEN", "LAL", "PHX", "MIA", "OKC", "SAS", "GSW", "DAL", "MIN"]


def _clock(secs_in_period: float) -> str:
    m, s = divmod(max(0.0, secs_in_period), 60)
    return f"PT{int(m):02d}M{s:05.2f}S"


def simulate_game(rng: random.Random, home: str, away: str, tip: pd.Timestamp,
                  pregame: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (pbp, ticks) for one synthetic game."""
    home_edge = 0.03 * (pregame - 0.5) / 0.15           # nudges scoring odds
    sh = sa = 0
    wall = tip
    acts, events = [], []                                # events: (wall_ts, jump)
    for period in (1, 2, 3, 4):
        if period == 3:
            wall += pd.Timedelta(seconds=900)            # halftime
        elif period > 1:
            wall += pd.Timedelta(seconds=130)            # quarter break
        t = 720.0
        home_ball = period % 2 == 1
        while t > 0:
            t -= rng.uniform(8, 22)
            wall += pd.Timedelta(seconds=rng.uniform(15, 45))
            if t <= 0:
                break
            team = home if home_ball else away
            p_score = 0.50 + (home_edge if home_ball else -home_edge)
            r = rng.random()
            before = win_prob(sh - sa, (4 - period) * 720 + t, pregame)
            if r < p_score:
                kind = rng.choices(["2pt", "3pt", "freethrow"], [0.62, 0.30, 0.08])[0]
                pts = {"2pt": 2, "3pt": 3, "freethrow": 1}[kind]
                if home_ball:
                    sh += pts
                else:
                    sa += pts
                result = "Made"
            elif r < p_score + 0.13:
                kind, result = "turnover", ""
            else:
                kind = rng.choice(["2pt", "3pt"])
                result = "Missed"
            acts.append({"ts": wall, "period": period, "clock": _clock(t),
                         "score_home": sh, "score_away": sa, "team": team,
                         "action_type": kind, "shot_result": result})
            if result == "Made":
                after = win_prob(sh - sa, (4 - period) * 720 + t, pregame)
                events.append((wall, after - before))
            home_ball = not home_ball
    if sh == sa:                                          # no OT in the toy sim
        sh += 1
        acts.append({"ts": wall + pd.Timedelta(seconds=5), "period": 4,
                     "clock": _clock(0.5), "score_home": sh, "score_away": sa,
                     "team": home, "action_type": "freethrow", "shot_result": "Made"})
    acts.append({"ts": wall + pd.Timedelta(seconds=20), "period": 4,
                 "clock": _clock(0), "score_home": sh, "score_away": sa,
                 "team": "", "action_type": "game", "shot_result": ""})
    pbp = pd.DataFrame(acts)

    # ── market ticks: fair value + decaying over-reaction + noise ─────────────
    pbp_ns = game_data.epoch_ns(pbp["ts"]).values
    start = tip - pd.Timedelta(minutes=30)
    end = pbp["ts"].iloc[-1] + pd.Timedelta(minutes=3)
    ts, px = [], []
    now = start
    ev_i = 0
    while now < end:
        now += pd.Timedelta(seconds=rng.expovariate(1 / 3.0))
        k = int((pbp_ns <= now.value).sum()) - 1
        if k < 0:
            fair = pregame
        else:
            row = pbp.iloc[k]
            secs = max(0.0, (4 - int(row.period)) * 720 + float(row.clock[2:4]) * 60
                       + float(row.clock[5:-1]))
            fair = win_prob(row.score_home - row.score_away, secs, pregame)
            if k == len(pbp) - 1:
                fair = 0.99 if sh > sa else 0.01
        shock = 0.0
        while ev_i < len(events) and events[ev_i][0] < now - pd.Timedelta(seconds=180):
            ev_i += 1
        for when, jump in events[ev_i:]:
            if when > now:
                break
            age = (now - when).total_seconds()
            shock += 0.9 * jump * math.exp(-age / 25.0)
        p = fair + shock + rng.gauss(0, 0.012)
        ts.append(now)
        px.append(round(min(0.99, max(0.01, p)), 2))
    ticks = pd.DataFrame({"ts": ts, "price": px})
    return pbp, ticks


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--games", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    mapping = []
    for i in range(args.games):
        home, away = rng.sample(TEAMS, 2)
        day = 10 + i
        code = f"26JAN{day:02d}{away}{home}"
        game_id = f"00225{i + 1:05d}"
        tip = pd.Timestamp(f"2026-01-{day:02d}T00:40:00Z")
        pregame = round(rng.uniform(0.35, 0.70), 2)
        pbp, ticks = simulate_game(rng, home, away, tip, pregame)

        pbp_path = OUT / "cache" / "s3_pbp" / f"{game_id}.parquet"
        pbp_path.parent.mkdir(parents=True, exist_ok=True)
        pbp.assign(ts=game_data.to_utc_ns(pbp["ts"]))[game_data.PBP_COLS].to_parquet(pbp_path, index=False)
        game_data.write_ticks(ticks, OUT / "kalshi" / "ticks" / f"KXNBAGAME-{code}-{home}.parquet")
        mapping.append({"game_code": code, "nba_game_id": game_id})
        final = pbp.iloc[-1]
        print(f"  {code}  {away} @ {home}  pregame {pregame:.2f}  "
              f"final {final.score_away}-{final.score_home}  {len(pbp)} plays, {len(ticks)} ticks")

    mp = OUT / "kalshi" / "markets" / "kalshi_nba_mapping.parquet"
    mp.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(mapping).to_parquet(mp, index=False)
    print(f"Wrote {args.games} synthetic games to {OUT}")


if __name__ == "__main__":
    main()
