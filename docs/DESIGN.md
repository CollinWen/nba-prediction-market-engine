# Live Execution Terminal — Design Spec

A "Bloomberg terminal" for trading Kalshi NBA in-game win markets, wired to a
win-probability model. Three subsystems: **order execution**,
**portfolio/fill tracking**, and a **live dashboard**.

> The original build spec. Subsystems were built against the interfaces defined
> here. Keep modules decoupled — talk through `core/interfaces.py`.

---

## Goals
1. **See the market** — live bid/ask/last/mid, orderbook depth, volume for NBA game contracts, with our model's win prob and the edge overlaid.
2. **See the game** — live score / clock / win-prob trajectory (Kalshi vs model).
3. **See our book** — open positions, fills, realized + unrealized P&L, exposure.
4. **Trade** — place/cancel limit & market orders with hard risk controls; manual
   and (later) automated from strategy signals.

Non-goals (for now): multi-account, options/spreads, non-NBA series.

---

## Architecture

```
live-execution/
├── DESIGN.md            ← this spec (the master prompt)
├── config.py            ← all settings: tickers, risk limits, strategy cfg, paths
├── core/
│   ├── interfaces.py    ← shared dataclasses + protocols (the contracts). DONE first.
│   ├── kalshi_client.py ← REST + WebSocket: markets, orderbook, trades, account, orders
│   ├── nba_feed.py      ← live game state from NBA S3 PBP (timeActual)
│   └── model_signal.py  ← win-prob model + edge (public build: closed-form baseline)
├── execution/
│   ├── order_manager.py ← place/cancel limit & market orders, idempotency, retries
│   └── risk.py          ← position/loss limits, kill switch, pre-trade checks
├── portfolio/
│   ├── tracker.py       ← positions, fills, realized/unrealized P&L
│   └── reconcile.py     ← sync local view with Kalshi truth (source of record)
├── dashboard/
│   ├── app.py           ← Plotly Dash app (dark theme), live callbacks
│   └── panels.py        ← panel builders (market watch, book, charts, blotters)
└── run_terminal.py      ← entrypoint: wires everything, starts dashboard
```

### Data flow
```
Kalshi REST/WS ─┐
NBA S3 PBP ─────┼─> core (client/feed/signal) ─> portfolio + execution ─> dashboard
Win-prob model ─┘                                         ^                    │
                                                          └──── user actions ──┘
```

---

## Key API facts
- **Auth only needed for** `/portfolio/*` and order placement; market/orderbook reads are public.
- **Order placement**: `POST /portfolio/orders` (authed). Limit (maker) and market (taker).
- **Cancel**: `DELETE /portfolio/orders/{order_id}`.
- **Fills**: `GET /portfolio/fills`; **positions**: `GET /portfolio/positions`; balance: `/portfolio/balance` (cents).
- **Fees**: maker `0.0175·C·P·(1−P)`, taker `0.07·C·P·(1−P)`, NBA ×1.15, round up to cent.
  The strategy is only viable on MAKER (limit) fills — default to limit orders.
- **Price units**: live trades = cents (int 0-100); historical = dollars (str); orderbook = cents.
  Normalize everything to float dollars 0-1 at the boundary.
- **WS** for live ticks/orderbook: `wss://api.elections.kalshi.com/trade-api/ws/v2`,
  channels `ticker`, `orderbook_delta`, `trade`, `fill` (auth).

## Account safety
- **Default to tiny size (1-5 contracts) until validated.**
- There may be pre-existing resting orders/positions unrelated to this system — never cancel
  orders this system didn't place (track our own order_ids).

---

## Risk controls (mandatory, in execution/risk.py)
- `max_contracts_per_order` (default 5)
- `max_position_per_market` (default 20 contracts)
- `max_total_exposure_usd` (default 20)
- `max_daily_loss_usd` (default 5) → kill switch halts new entries
- `dry_run` flag (default TRUE) — log intended orders without sending. Must be explicitly
  disabled to trade real money.
- Pre-trade check: reject if order would breach any limit.

---

## Dashboard (Plotly Dash, dark theme) — modular workspace
The terminal is a WORKSPACE of widget instances (June 12 rework): a toolbar
dropdown adds widgets, each widget has its own ✕ and its own selections, and
any type can run in multiple instances (pattern-matching ids,
`dashboard/widgets.py`). Adding/removing uses `dash.Patch`, so existing
widgets keep their state.

Live widgets (refresh off one `dcc.Interval`, ~3s, need providers):
1. **Market Watch** — watchlist-driven table: ticker, bid/ask/last/mid, volume,
   model_prob, edge. The watchlist (add/remove tickers, free-text add) is global
   state feeding every live widget.
2. **Order Book** — depth chart, per-widget ticker picker.
3. **Win-Prob Chart** — Kalshi mid vs model prob + score-diff subplot.
4. **Positions** / 5. **Blotter** / 6. **Signal Feed** / 7. **Account** — as before.
8. **Order Entry** — manual limit orders, gated behind `dry_run`.

Historical widgets (work offline — the terminal doubles as a backtest workbench):
9. **Signal Explorer** — pick an archived game (dashboard/history.py catalogs
   every game with ticks + PBP, ~72), a strategy (strategy/registry.py), and an
   execution scenario (backtest/exec_model.py) → replays it through
   backtest/session_replay.run_replay and shows Kalshi odds vs regenerated
   model odds, the edge signal vs its entry threshold, entry/exit markers,
   and the trade-by-trade net.
10. **Alpha Scanner** — same, but across MANY games: cumulative equity curve,
   per-game P&L bars, headline stats (total, win rate, best/worst game).

Replays run in background threads (dashboard/history.ReplayService) and cache
per (game, strategy, exec, qty) — the UI never blocks, re-runs are instant.
Strategies registered in `strategy/registry.py` appear in both widgets AND can
be run live by the ExecutionEngine — one Strategy protocol, live and backtest.

---

## Build order (for agents)
1. `core/interfaces.py` — DONE in scaffold (dataclasses + Protocols). Everyone imports these.
2. **core data layer** (kalshi_client, nba_feed, model_signal) — foundation.
3. **execution + risk** and **portfolio** — depend on core.
4. **dashboard** — depends on all; can develop against interface stubs with mock data first.

Each module must run/standalone-test with a `if __name__ == "__main__":` smoke test.
