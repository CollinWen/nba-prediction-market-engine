"""
nba_feed.py
===========
Live NBA game state from the NBA liveData S3 feeds.

Implements the `GameFeed` protocol from `core.interfaces`:
  * todays_games()         -> list[dict]  [{game_id, home, away, status}]
  * game_state(game_id)    -> Optional[GameState]

Data sources (see config.py)
----------------------------
* Scoreboard: NBA_S3_SCORE  (today's slate, game ids + matchups + status)
* Play-by-play: NBA_S3_PBP  (per-game; the LAST play carries current
  period/clock/score)

Clock parsing
-------------
NBA clocks are ISO-8601 durations: "PT11M23.00S" -> 11m23s left in the period.
We convert to *regulation seconds remaining* (0-2880):

    secs_remaining = period_secs + max(0, 4 - period) * 720

i.e. add the full remaining regulation quarters. OT (period >= 5) yields a
small/zero remaining value, which is fine for the model (it clamps).

403 handling
------------
Before a game's PBP is published (pre-tip), S3 returns 403/404. game_state()
returns None in that case so callers can poll quietly.
"""
from __future__ import annotations

import re
import sys
import datetime as dt
from pathlib import Path
from typing import Optional

import requests

# ── Import bootstrap (see _bootstrap.py) ────────────────────────────────────────
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))   # .../live-execution
from _bootstrap import boot  # noqa: E402
boot()

from core.interfaces import GameState  # noqa: E402
from config import NBA_S3_PBP, NBA_S3_SCORE, HTTP_HEADERS, BBREF_TO_KALSHI  # noqa: E402


_CLOCK_RE = re.compile(r"PT(\d+)M(\d+(?:\.\d+)?)S")


def _clock_to_secs_remaining(period, clock: str) -> Optional[float]:
    """ISO-8601 period clock + period number -> regulation seconds remaining."""
    m = _CLOCK_RE.match(str(clock))
    if not m:
        return None
    try:
        period = int(period)
    except (TypeError, ValueError):
        return None
    period_secs = int(m.group(1)) * 60 + int(float(m.group(2)))
    return period_secs + max(0, 4 - period) * 720


def _kalshi_abbr(tri: str) -> str:
    """Map an NBA tricode to the abbreviation Kalshi uses (BRK->BKN, etc.)."""
    return BBREF_TO_KALSHI.get(tri, tri)


class NBAFeed:
    """Live game feed backed by NBA S3 liveData JSON."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    # ── GameFeed protocol ───────────────────────────────────────────────────────
    def todays_games(self) -> list[dict]:
        """Return today's slate as [{game_id, home, away, status}].

        `status`: 1 = pre-game, 2 = live, 3 = final (NBA's gameStatus int) — we
        pass through both the int and a human label.
        """
        r = requests.get(NBA_S3_SCORE, headers=HTTP_HEADERS, timeout=self.timeout)
        r.raise_for_status()
        sb = r.json().get("scoreboard", {})
        games = []
        for g in sb.get("games", []):
            home = g.get("homeTeam", {}) or {}
            away = g.get("awayTeam", {}) or {}
            status_int = g.get("gameStatus")  # 1/2/3
            games.append({
                "game_id": g.get("gameId"),
                "home": _kalshi_abbr(home.get("teamTricode", "")),
                "away": _kalshi_abbr(away.get("teamTricode", "")),
                "status": status_int,
                "status_text": g.get("gameStatusText", ""),
                "home_score": home.get("score"),
                "away_score": away.get("score"),
            })
        return games

    def game_state(self, game_id: str) -> Optional[GameState]:
        """Current GameState from the latest published play. None if pre-tip (403)."""
        url = NBA_S3_PBP.format(game_id)
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=self.timeout)
        except requests.RequestException:
            return None
        # Pre-tip the PBP object doesn't exist yet -> 403/404 from S3.
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()

        game = r.json().get("game", {})
        actions = [a for a in game.get("actions", []) if a.get("timeActual")]
        if not actions:
            return None

        # Walk backwards to the most recent play that yields a parseable clock.
        cur = None
        secs = None
        for a in reversed(actions):
            secs = _clock_to_secs_remaining(a.get("period"), a.get("clock", ""))
            if secs is not None:
                cur = a
                break
        if cur is None:
            return None

        # Scores can be blank on non-scoring events; carry forward the last
        # non-empty values by scanning all actions.
        score_home = _last_int([a.get("scoreHome") for a in actions])
        score_away = _last_int([a.get("scoreAway") for a in actions])

        home = _kalshi_abbr(game.get("homeTeam", {}).get("teamTricode", "")
                            if isinstance(game.get("homeTeam"), dict) else "")
        away = _kalshi_abbr(game.get("awayTeam", {}).get("teamTricode", "")
                            if isinstance(game.get("awayTeam"), dict) else "")

        return GameState(
            game_id=str(game_id),
            home=home,
            away=away,
            period=int(cur.get("period") or 1),
            secs_remaining=float(secs),
            score_home=score_home,
            score_away=score_away,
        )


def _last_int(values) -> int:
    """Return the last non-empty value coerced to int, else 0."""
    last = 0
    for v in values:
        if v is None or v == "":
            continue
        try:
            last = int(v)
        except (TypeError, ValueError):
            continue
    return last


# ── Smoke test (LIVE, read-only) ────────────────────────────────────────────────
if __name__ == "__main__":
    feed = NBAFeed()
    print("=" * 64)
    print("NBAFeed smoke test (LIVE, read-only)")
    print("=" * 64)

    print("\n[1] todays_games()")
    try:
        games = feed.todays_games()
        print(f"    {len(games)} games today")
        for g in games:
            print(f"      {g['game_id']}  {g['away']} @ {g['home']}  "
                  f"status={g['status']} ({g['status_text']})  "
                  f"{g['away_score']}-{g['home_score']}")
    except Exception as e:
        print(f"    todays_games failed: {e}")
        games = []

    print("\n[2] game_state(game_id) for each game")
    if not games:
        print("    no games on the slate today — nothing to fetch.")
    for g in games:
        gs = feed.game_state(g["game_id"])
        if gs is None:
            print(f"    {g['game_id']}: pre-tip / unavailable (handled, returned None)")
        else:
            mins = int(gs.secs_remaining // 60)
            ss = int(gs.secs_remaining % 60)
            print(f"    {g['game_id']}: Q{gs.period} {mins}:{ss:02d} left  "
                  f"{gs.away} {gs.score_away} - {gs.score_home} {gs.home}  "
                  f"(diff {gs.score_diff:+d}, {gs.secs_remaining:.0f}s reg rem)")

    print("\nDone.")
