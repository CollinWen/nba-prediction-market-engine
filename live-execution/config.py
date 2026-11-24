"""
Central config for the live-execution terminal.
All tunables live here so subsystems stay decoupled.
"""
from dataclasses import dataclass, field
from pathlib import Path

# ── Paths (repo root is parent of this folder) ────────────────────────────────
# DATA_DIR defaults to ./sample_data (synthetic games built by
# scripts/make_sample_data.py). Point NBA_DATA_DIR at a real archive with the
# same layout (kalshi/ticks, cache/s3_pbp, kalshi/markets) to replay real games.
import os
ROOT        = Path(__file__).resolve().parent.parent
DATA_DIR    = Path(os.environ.get("NBA_DATA_DIR", ROOT / "sample_data"))
STRATEGY_CFG = Path(__file__).resolve().parent / "strategy" / "example_params.json"
TICK_DIR    = DATA_DIR / "kalshi/ticks"
PBP_ARCHIVE = DATA_DIR / "cache/s3_pbp"
PBP_JSON_CACHE = DATA_DIR / "nba/pbp_cache"
MAPPING_PARQUET = DATA_DIR / "kalshi/markets/kalshi_nba_mapping.parquet"
SCHEDULE_PARQUET = DATA_DIR / "raw/nba_api/nba_games.parquet"
LIVE_DIR    = DATA_DIR / "live"
STATE_DIR   = LIVE_DIR / "state"         # persisted session state (pregame prob etc.)

# ── Kalshi endpoints ──────────────────────────────────────────────────────────
WS_URL      = "wss://api.elections.kalshi.com/trade-api/ws/v2"
# REST uses kalshi_auth.BASE_URL / get()

# ── NBA data ──────────────────────────────────────────────────────────────────
NBA_S3_PBP   = "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/NBA/liveData/playbyplay/playbyplay_{}.json"
NBA_S3_SCORE = "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/NBA/liveData/scoreboard/todaysScoreboard_00.json"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── Series ────────────────────────────────────────────────────────────────────
NBA_SERIES   = "KXNBAGAME"
BBREF_TO_KALSHI = {"BRK": "BKN", "CHO": "CHA", "PHO": "PHX"}

# ── Risk controls (see DESIGN.md) ─────────────────────────────────────────────
@dataclass
class RiskLimits:
    dry_run: bool = True                 # MUST be explicitly set False to trade real money
    max_contracts_per_order: int = 5
    max_position_per_market: int = 20
    max_total_exposure_usd: float = 20.0
    max_daily_loss_usd: float = 5.0
    default_order_type: str = "limit"    # 'limit' (maker) or 'market' (taker)

RISK = RiskLimits()

# ── Execution engine (see execution/engine.py) ────────────────────────────────
@dataclass
class EngineSettings:
    poll_secs: float = 20.0           # strategy poll interval
    fill_poll_secs: float = 3.0       # order-status poll while awaiting a fill
    fill_timeout_secs: float = 30.0   # cancel unfilled limit after this long
    exit_cross_extra: float = 0.02    # 2nd-attempt exits cross this far through
    stale_clock_polls: int = 6        # PBP clock frozen this many polls -> block entries
    game_over_polls: int = 3          # Q4+ 0:00 sustained this many polls -> flatten+halt
    max_order_failures: int = 3       # consecutive send failures -> halt session
    market_check_polls: int = 30      # verify market still active every N polls

ENGINE = EngineSettings()

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASH_HOST   = "127.0.0.1"
DASH_PORT   = 8050
REFRESH_MS  = 3000                       # live refresh interval
THEME       = "dark"
