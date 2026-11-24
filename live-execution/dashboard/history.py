"""
history.py
==========
Historical data layer for the terminal's backtest widgets.

Two responsibilities:

1. GAME CATALOG — every archived game that is fully replayable: a tick parquet
   in data/kalshi/ticks plus parsed play-by-play in data/cache/s3_pbp, with the
   Kalshi game_code resolved to an NBA game_id (mapping parquet first, then
   date+team lookup against the season schedule, then a scan of playoff PBP
   files for games newer than the schedule snapshot).

2. REPLAY SERVICE — runs backtest.session_replay.run_replay in background
   threads so Dash callbacks never block on a multi-second replay. Results are
   cached per (game, strategy, exec scenario, qty); a batch job re-uses any
   single-game results already computed.

All prices YES-frame dollars (home side). If only the away-side tick file
exists for a game, prices are inverted (1 - p) into the home frame.
"""
from __future__ import annotations

import re
import threading
import traceback
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from _bootstrap import boot
boot()

import config                                              # noqa: E402
import game_data                                           # noqa: E402
from backtest.exec_model import ExecConfig, default_scenarios   # noqa: E402
from strategy import registry                              # noqa: E402

MAPPING_PARQUET = config.MAPPING_PARQUET
SCHEDULE_PARQUET = config.SCHEDULE_PARQUET
PBP_ARCHIVE = config.PBP_ARCHIVE
PBP_JSON_CACHE = config.PBP_JSON_CACHE

_CODE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})$")
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def parse_game_code(code: str) -> Optional[dict]:
    """'26JUN05NYKSAS' -> {date: 2026-06-05, away: NYK, home: SAS}."""
    m = _CODE_RE.match(code)
    if not m:
        return None
    yy, mon, dd, away, home = m.groups()
    try:
        date = pd.Timestamp(year=2000 + int(yy), month=_MONTHS[mon], day=int(dd))
    except (KeyError, ValueError):
        return None
    return {"date": date, "away": away, "home": home}


# ── game catalog ──────────────────────────────────────────────────────────────
@dataclass
class HistGame:
    code: str
    game_id: str
    date: pd.Timestamp
    home: str
    away: str
    tick_path: object           # Path of the tick parquet to load
    invert_ticks: bool          # True if tick file is the away side
    @property
    def label(self) -> str:
        return f"{self.date:%b %d}  {self.away} @ {self.home}"
    @property
    def home_ticker(self) -> str:
        return f"KXNBAGAME-{self.code}-{self.home}"


class GameCatalog:
    def __init__(self):
        self._games: Optional[dict[str, HistGame]] = None
        self._lock = threading.Lock()

    def games(self) -> list[HistGame]:
        """Replayable games, newest first. Built once, cached for the session."""
        with self._lock:
            if self._games is None:
                self._games = self._build()
        return sorted(self._games.values(), key=lambda g: g.date, reverse=True)

    def get(self, code: str) -> Optional[HistGame]:
        self.games()
        return self._games.get(code)

    # -- internals -------------------------------------------------------------
    def _build(self) -> dict[str, HistGame]:
        # PBP is available from the parquet archive OR the liveData JSON cache
        # (recent games land in the cache first); _load_pbp reads both.
        pbp_ids = ({p.stem for p in PBP_ARCHIVE.glob("*.parquet")}
                   | {p.stem for p in PBP_JSON_CACHE.glob("*.json")})
        mapping = self._mapping()
        schedule = self._schedule_index()

        # collect tick files per game_code, preferring the home-side ticker
        sides: dict[str, dict[str, object]] = {}
        for path in config.TICK_DIR.glob("KXNBAGAME-*.parquet"):
            m = re.match(r"KXNBAGAME-(\w+)-([A-Z]{3})$", path.stem)
            if m:
                sides.setdefault(m.group(1), {})[m.group(2)] = path

        out: dict[str, HistGame] = {}
        unmapped: list[tuple[str, dict, dict]] = []
        for code, side_paths in sides.items():
            parsed = parse_game_code(code)
            if parsed is None:
                continue
            gid = mapping.get(code) or schedule.get(
                (parsed["date"].strftime("%Y-%m-%d"), parsed["away"], parsed["home"]))
            if gid and gid in pbp_ids:
                out[code] = self._mk(code, gid, parsed, side_paths)
            elif not gid:
                unmapped.append((code, parsed, side_paths))

        if unmapped:
            for code, parsed, side_paths in unmapped:
                gid = self._scan_playoff_pbp(parsed, pbp_ids)
                if gid:
                    out[code] = self._mk(code, gid, parsed, side_paths)
        return out

    @staticmethod
    def _mk(code, gid, parsed, side_paths) -> HistGame:
        home = parsed["home"]
        if home in side_paths:
            tick_path, invert = side_paths[home], False
        else:  # only the away side was collected -> invert into home frame
            away_side = next(iter(side_paths))
            tick_path, invert = side_paths[away_side], True
        return HistGame(code=code, game_id=str(gid), date=parsed["date"],
                        home=home, away=parsed["away"],
                        tick_path=tick_path, invert_ticks=invert)

    @staticmethod
    def _mapping() -> dict[str, str]:
        if not MAPPING_PARQUET.exists():
            return {}
        df = pd.read_parquet(MAPPING_PARQUET)
        return {r.game_code: str(r.nba_game_id)
                for r in df.itertuples()
                if isinstance(r.nba_game_id, str) and r.nba_game_id}

    @staticmethod
    def _schedule_index() -> dict[tuple, str]:
        if not SCHEDULE_PARQUET.exists():
            return {}
        df = pd.read_parquet(SCHEDULE_PARQUET)
        return {(str(r.game_date)[:10], r.away_team, r.home_team): str(r.game_id)
                for r in df.itertuples()}

    @staticmethod
    def _pbp_meta(gid: str) -> Optional[tuple[str, set[str]]]:
        """(first action's UTC date, team tricodes) from either PBP store."""
        pq = PBP_ARCHIVE / f"{gid}.parquet"
        try:
            if pq.exists():
                df = pd.read_parquet(pq, columns=["ts", "team"])
                if df.empty:
                    return None
                teams = {t for t in df["team"].dropna().unique() if t}
                first = game_data.to_utc_ns(df["ts"].dropna().iloc[:1])
                return (first.iloc[0].strftime("%Y-%m-%d"), teams) if len(first) else None
            js = PBP_JSON_CACHE / f"{gid}.json"
            if js.exists():
                import json
                acts = json.loads(js.read_text()).get("game", {}).get("actions", [])
                acts = [a for a in acts if a.get("timeActual")]
                if not acts:
                    return None
                teams = {a.get("teamTricode") for a in acts if a.get("teamTricode")}
                date = pd.to_datetime(acts[0]["timeActual"], utc=True).strftime("%Y-%m-%d")
                return date, teams
        except Exception:
            return None
        return None

    @classmethod
    def _scan_playoff_pbp(cls, parsed: dict, pbp_ids: set[str]) -> Optional[str]:
        """Find the PBP file for a game the schedule snapshot doesn't know yet
        (e.g. finals): match by tip-off date and the two tricodes. The code's
        local US date maps to a UTC timestamp on the same or next day."""
        want = {parsed["home"], parsed["away"]}
        ok_dates = {parsed["date"].strftime("%Y-%m-%d"),
                    (parsed["date"] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")}
        for gid in sorted(pbp_ids, reverse=True):
            if not gid.startswith("004"):     # playoffs only — keep the scan small
                continue
            meta = cls._pbp_meta(gid)
            if meta and meta[0] in ok_dates and want.issubset(meta[1]):
                return gid
        return None


# ── replay service (background jobs + cache) ──────────────────────────────────
@dataclass
class Job:
    key: tuple
    status: str = "running"           # running | done | error
    progress: str = ""
    result: dict = field(default_factory=dict)
    error: str = ""


def scenario_map() -> dict[str, ExecConfig]:
    return {c.name: c for c in default_scenarios()}


class ReplayService:
    def __init__(self, catalog: GameCatalog):
        self.catalog = catalog
        self._jobs: dict[tuple, Job] = {}
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------------
    def single(self, code: str, strategy_key: str, exec_name: str, qty: int) -> Job:
        """Return the cached/running job for one game replay, submitting if new."""
        key = ("single", code, strategy_key, exec_name, qty)
        return self._get_or_spawn(key, lambda job: self._run_single(job, code,
                                  strategy_key, exec_name, qty))

    def batch(self, codes: tuple[str, ...], strategy_key: str,
              exec_name: str, qty: int) -> Job:
        """Replay many games under one strategy; per-game results are cached
        through the same single-game path the explorer uses."""
        key = ("batch", codes, strategy_key, exec_name, qty)
        return self._get_or_spawn(key, lambda job: self._run_batch(job, codes,
                                  strategy_key, exec_name, qty))

    # -- internals -----------------------------------------------------------------
    def _get_or_spawn(self, key: tuple, target) -> Job:
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                return job
            job = Job(key=key)
            self._jobs[key] = job
        threading.Thread(target=self._guard(target, job), daemon=True).start()
        return job

    @staticmethod
    def _guard(target, job: Job):
        def run():
            try:
                target(job)
                job.status = "done"
            except Exception:
                job.error = traceback.format_exc(limit=8)
                job.status = "error"
        return run

    def _replay_one(self, code: str, strategy_key: str, exec_name: str,
                    qty: int) -> dict:
        """Synchronous single-game replay with cache short-circuit."""
        key = ("single", code, strategy_key, exec_name, qty)
        with self._lock:
            done = self._jobs.get(key)
            if done is not None and done.status == "done":
                return done.result

        # imported lazily: session_replay pulls in matplotlib
        from backtest.session_replay import (_load_pbp, _recover_pregame,
                                             run_replay)
        game = self.catalog.get(code)
        if game is None:
            raise ValueError(f"unknown game code {code}")
        acts, act_times, home_tri = _load_pbp(game.game_id, game.home)
        ticks = game_data.read_ticks(game.tick_path)
        if game.invert_ticks:
            ticks = ticks.assign(price=1.0 - ticks["price"])
        pregame = _recover_pregame(ticks, act_times[0])
        exec_cfg = scenario_map()[exec_name]
        spec = registry.get(strategy_key)
        tdf, pdf = run_replay(game.game_id, game.home, game.away,
                              game.home_ticker, pregame, qty,
                              acts, act_times, home_tri, ticks, exec_cfg,
                              strategy_factory=spec.factory)
        result = {"code": code, "label": game.label, "pregame": pregame,
                  "qty": qty, "strategy": spec.label, "exec": exec_name,
                  "tdf": tdf, "pdf": pdf}
        with self._lock:
            j = self._jobs.setdefault(key, Job(key=key))
            j.result, j.status = result, "done"
        return result

    def _run_single(self, job: Job, code, strategy_key, exec_name, qty):
        job.progress = "replaying…"
        job.result = self._replay_one(code, strategy_key, exec_name, qty)

    def _run_batch(self, job: Job, codes, strategy_key, exec_name, qty):
        per_game = []
        for i, code in enumerate(codes, 1):
            job.progress = f"game {i}/{len(codes)}: {code}"
            try:
                per_game.append(self._replay_one(code, strategy_key, exec_name, qty))
            except Exception as e:          # one bad game shouldn't sink the sweep
                per_game.append({"code": code, "label": code, "error": str(e)})
        job.result = {"per_game": per_game, "strategy": strategy_key,
                      "exec": exec_name, "qty": qty}


# module-level singletons shared by all dashboard callbacks
CATALOG = GameCatalog()
REPLAYS = ReplayService(CATALOG)
