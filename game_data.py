"""
game_data.py — canonical game-data I/O + validation.

The ONE place game data is read and written, so ingested and retrieved data is
always in the same format. Enforces the project data standards:

  * timestamps : column `ts`, dtype datetime64[ns, UTC] (ns resolution, tz-aware)
  * prices     : float64 dollars in [0,1], YES-frame (P(home) for a -HOME ticker)
  * counts     : int contracts
  * tricodes   : NBA/Kalshi modern codes (BKN, CHA, PHX)

Why this exists: a microsecond-vs-nanosecond timestamp mismatch between the live
collector (us) and the REST backfill (ns) silently froze the backtester's price
lookup (June 2026). Normalising at the I/O boundary makes that class of bug
impossible regardless of how a source happens to store things.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# ── canonical schemas ─────────────────────────────────────────────────────────
TICK_COLS = ["ts", "price"]                       # <DATA_DIR>/kalshi/ticks/{ticker}.parquet
PBP_COLS = ["ts", "period", "clock", "score_home", "score_away",
            "team", "action_type", "shot_result"]  # <DATA_DIR>/cache/s3_pbp/{game_id}.parquet

# liveData (camelCase JSON) -> canonical PBP column. Apply when ingesting the
# NBA liveData feed so it matches the archive parquet exactly.
LIVEDATA_TO_PBP = {
    "timeActual": "ts", "period": "period", "clock": "clock",
    "scoreHome": "score_home", "scoreAway": "score_away",
    "teamTricode": "team", "actionType": "action_type", "shotResult": "shot_result",
}
# canonical PBP column -> the raw-action key model_signal.build_features_from_actions
# expects (it reads the camelCase keys). Inverse of LIVEDATA_TO_PBP.
PBP_TO_ACTION = {v: k for k, v in LIVEDATA_TO_PBP.items()}

# Legacy tricodes (bbref) -> canonical (NBA/Kalshi). Apply at ingestion.
BBREF_TO_KALSHI = {"BRK": "BKN", "CHO": "CHA", "PHO": "PHX"}


# ── timestamps ────────────────────────────────────────────────────────────────
def to_utc_ns(s) -> pd.Series:
    """Any datetime-like -> tz-aware UTC at NANOSECOND resolution.

    Handles ISO strings, epoch seconds, and datetime64 of any resolution. This
    is the single source of truth for time; never call pd.to_datetime directly
    on game data elsewhere.
    """
    out = pd.to_datetime(s, utc=True)
    # pandas >=2 preserves source resolution (us/ms); force ns so int64 epoch
    # comparisons line up across all files.
    try:
        out = out.dt.as_unit("ns")
    except (AttributeError, ValueError):
        out = out.astype("datetime64[ns, UTC]")
    return out


def epoch_ns(s) -> "pd.Series":
    """Canonical ts -> int64 nanoseconds since epoch (for searchsorted)."""
    return to_utc_ns(s).astype("int64")


# ── ticks ─────────────────────────────────────────────────────────────────────
def read_ticks(path) -> pd.DataFrame:
    """Read a tick parquet into the canonical (ts ns-UTC, price float) frame,
    sorted ascending by ts. Tolerates legacy files (extra columns, us ts)."""
    df = pd.read_parquet(path)
    df["ts"] = to_utc_ns(df["ts"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["price"])
    return df[TICK_COLS].sort_values("ts").reset_index(drop=True)


def write_ticks(df: pd.DataFrame, path) -> None:
    """Write a tick frame in canonical form: ts (ns UTC) + price, sorted,
    deduped on (ts, price)."""
    out = df.copy()
    out["ts"] = to_utc_ns(out["ts"])
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = (out.dropna(subset=["price"])[TICK_COLS]
           .drop_duplicates(subset=["ts", "price"])
           .sort_values("ts").reset_index(drop=True))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False)


# ── price / count normalisation at the Kalshi API boundary ────────────────────
def kalshi_price_dollars(payload: dict, side: str) -> float:
    """YES/NO price in float dollars from a Kalshi object, tolerant of both the
    `*_price_dollars` (string) and legacy integer-cents shapes."""
    dk = f"{side}_price_dollars"
    if payload.get(dk) not in (None, ""):
        return float(payload[dk])
    ck = f"{side}_price"
    v = payload.get(ck)
    return float(v) / 100.0 if v not in (None, "") else 0.0


def kalshi_count(payload: dict) -> int:
    """Contract count from a Kalshi object: `count` (int) or `count_fp` (string)."""
    if payload.get("count") not in (None, ""):
        return int(payload["count"])
    try:
        return int(float(payload.get("count_fp", 0) or 0))
    except (TypeError, ValueError):
        return 0


# ── validation ────────────────────────────────────────────────────────────────
def validate_ticks(path) -> list[str]:
    """Return a list of standard violations for a tick file (empty == clean)."""
    problems = []
    df = pd.read_parquet(path)
    if list(df.columns)[:2] != TICK_COLS or set(df.columns) - set(TICK_COLS):
        problems.append(f"columns {list(df.columns)} != canonical {TICK_COLS}")
    dt = str(df["ts"].dtype) if "ts" in df else "<missing>"
    if dt != "datetime64[ns, UTC]":
        problems.append(f"ts dtype {dt} != datetime64[ns, UTC]")
    if "price" in df:
        p = pd.to_numeric(df["price"], errors="coerce")
        if p.min() < 0 or p.max() > 1:
            problems.append(f"price out of [0,1]: [{p.min()}, {p.max()}]")
    if "ts" in df and not df["ts"].is_monotonic_increasing:
        problems.append("ts not sorted ascending")
    return problems


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for f in sys.argv[1:]:
            probs = validate_ticks(f)
            tag = "OK" if not probs else "VIOLATIONS"
            print(f"[{tag}] {f}")
            for p in probs:
                print(f"    - {p}")
    else:
        print("usage: python game_data.py <tick.parquet> [...]  (validates files)")
