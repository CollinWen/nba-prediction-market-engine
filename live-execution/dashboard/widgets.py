"""
widgets.py
==========
Modular widget framework for the Kalshi NBA terminal.

The workspace is a list of widget INSTANCES the user adds/removes at runtime
(toolbar dropdown + per-widget ✕). Every component inside a widget body uses a
pattern-matching id {"t": <token>, "wid": <instance id>}, so any widget type
can exist in multiple instances with independent selections — two order books
on different tickers, two signal explorers on different games.

Widget catalog
  live (need providers):  account, market_watch (owns the global watchlist),
                          orderbook, winprob, positions, blotter, signal_feed,
                          order_entry
  historical (offline):   signal_explorer — replay ONE archived game through a
                          registered strategy: Kalshi odds vs Model odds,
                          the live edge signal vs its entry threshold, and the
                          trades the strategy would have done.
                          alpha_scanner — replay MANY games under one strategy
                          and execution model: cumulative equity, per-game P&L.

Backtests run through dashboard.history.ReplayService (background threads over
backtest/session_replay.run_replay — the same Strategy protocol the live
ExecutionEngine drives), so anything added to strategy/registry.py shows up
here automatically.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

import dash
from dash import ALL, MATCH, Input, Output, State, dcc, html, no_update
from dash.exceptions import PreventUpdate

from _bootstrap import boot
boot()

import config                                    # noqa: E402
from core.interfaces import Order                # noqa: E402
from dashboard import history, panels           # noqa: E402
from dashboard.panels import (AMBER, BG, BLUE, FG, FONT, GREEN, GRID, MUTED,
                              PANEL_BG, RED)     # noqa: E402
from strategy import registry                    # noqa: E402

# ── shared in-memory win-prob history (live widgets) ──────────────────────────
WINPROB_HISTORY: dict[str, list[dict]] = {}
_MAX_HISTORY = 240   # ~12 min at 3s refresh


def record_winprob(providers, ticker: str) -> None:
    """Append one (kalshi mid, model prob, score diff) sample for a live ticker."""
    try:
        glue = providers.game_for_ticker(ticker)
        if glue is None:
            return
        gid, home, away, pregame = glue
        gs = providers.games.game_state(gid)
        if gs is None:
            return
        q = providers.market.quote(ticker)
        model_prob = providers.signals.model_prob_home(gs, pregame)
    except Exception:
        return
    hist = WINPROB_HISTORY.setdefault(ticker, [])
    hist.append({"ts": dt.datetime.now(), "kalshi_mid": q.mid,
                 "model_prob": model_prob, "score_diff": gs.score_diff})
    if len(hist) > _MAX_HISTORY:
        del hist[: len(hist) - _MAX_HISTORY]


# ── small helpers ─────────────────────────────────────────────────────────────
def list_markets(providers) -> list[str]:
    try:
        return providers.market.list_nba_markets()
    except Exception:
        return []


def _msg(text, color=MUTED):
    return html.Div(text, style={"color": color, "fontFamily": FONT,
                                 "fontSize": "11px", "padding": "8px"})


def _dd_style(width="100%"):
    return {"width": width, "color": "#000", "fontSize": "11px"}


def _input_style(width="70px"):
    return {"width": width, "backgroundColor": BG, "color": FG,
            "border": f"1px solid {GRID}", "padding": "5px",
            "fontFamily": FONT, "fontSize": "11px"}


def _btn_style(color=BLUE):
    return {"backgroundColor": color, "color": "#000", "border": "none",
            "padding": "5px 14px", "fontFamily": FONT, "fontWeight": "bold",
            "cursor": "pointer", "borderRadius": "3px", "fontSize": "11px"}


def _game_options():
    return [{"label": f"{g.label}   [{g.code}]", "value": g.code}
            for g in history.CATALOG.games()]


def _strategy_options():
    return [{"label": s.label, "value": s.key, "title": s.description}
            for s in registry.all_specs()]


def _exec_options():
    return [{"label": name, "value": name} for name in history.scenario_map()]


def _ticker_options(providers, watchlist):
    opts = sorted(set(list_markets(providers)) | set(watchlist or []))
    return [{"label": t, "value": t} for t in opts]


# ── widget shell ──────────────────────────────────────────────────────────────
@dataclass
class WidgetType:
    key: str
    title: str
    build: Callable          # (wid, providers) -> body component
    basis: str = "480px"     # flex-basis: widget's natural width
    needs_live: bool = False


def widget_shell(wid: str, wtype: WidgetType, providers) -> html.Div:
    body = wtype.build(wid, providers)
    return html.Div([
        html.Div([
            html.Span(wtype.title, style={
                "color": BLUE, "fontSize": "11px", "fontWeight": "bold",
                "textTransform": "uppercase", "letterSpacing": "0.5px"}),
            html.Button("✕", id={"t": "w-close", "wid": wid}, n_clicks=0, style={
                "backgroundColor": "transparent", "color": MUTED, "border": "none",
                "cursor": "pointer", "float": "right", "fontSize": "12px",
                "padding": "0 2px", "lineHeight": "1"}),
        ], style={"padding": "4px 8px", "borderBottom": f"1px solid {GRID}",
                  "backgroundColor": BG}),
        html.Div(body, style={"padding": "6px"}),
    ], id={"t": "widget", "wid": wid}, style={
        "backgroundColor": PANEL_BG, "border": f"1px solid {GRID}",
        "borderRadius": "4px", "margin": "4px", "overflow": "hidden",
        "fontFamily": FONT, "flex": f"1 1 {wtype.basis}", "minWidth": "320px",
    })


# ── widget bodies ─────────────────────────────────────────────────────────────
def _build_account(wid, providers):
    return html.Div(_msg("loading…"), id={"t": "acct-body", "wid": wid})


def _build_market_watch(wid, providers):
    markets = list_markets(providers)
    return html.Div([
        html.Div([
            dcc.Dropdown(id={"t": "mw-sel", "wid": wid}, multi=True,
                         options=[{"label": t, "value": t} for t in markets],
                         value=markets, placeholder="watchlist tickers…",
                         style=_dd_style()),
            html.Div([
                dcc.Input(id={"t": "mw-add-input", "wid": wid}, type="text",
                          placeholder="add ticker (KXNBAGAME-…)",
                          style=_input_style("260px")),
                html.Button("ADD", id={"t": "mw-add-btn", "wid": wid},
                            n_clicks=0, style=_btn_style()),
            ], style={"display": "flex", "gap": "6px", "marginTop": "4px"}),
        ]),
        html.Div(_msg("loading…"), id={"t": "mw-body", "wid": wid},
                 style={"marginTop": "6px"}),
    ])


def _ticker_picker(wid, token, providers):
    markets = list_markets(providers)
    return dcc.Dropdown(id={"t": token, "wid": wid},
                        options=[{"label": t, "value": t} for t in markets],
                        value=markets[0] if markets else None,
                        placeholder="ticker…", style=_dd_style("320px"))


def _build_orderbook(wid, providers):
    return html.Div([
        _ticker_picker(wid, "ob-ticker", providers),
        dcc.Graph(id={"t": "ob-graph", "wid": wid},
                  config={"displayModeBar": False}, style={"height": "280px"},
                  figure=panels.orderbook_figure(None)),
    ])


def _build_winprob(wid, providers):
    return html.Div([
        _ticker_picker(wid, "wp-ticker", providers),
        dcc.Graph(id={"t": "wp-graph", "wid": wid},
                  config={"displayModeBar": False}, style={"height": "340px"},
                  figure=panels.winprob_figure([], "—")),
    ])


def _build_positions(wid, providers):
    return html.Div(_msg("loading…"), id={"t": "pos-body", "wid": wid})


def _build_blotter(wid, providers):
    return html.Div(_msg("loading…"), id={"t": "blot-body", "wid": wid})


def _build_signal_feed(wid, providers):
    return html.Div(_msg("loading…"), id={"t": "sig-body", "wid": wid})


def _build_order_entry(wid, providers):
    dry = config.RISK.dry_run
    markets = list_markets(providers)
    return html.Div([
        html.Div([
            dcc.Dropdown(id={"t": "oe-ticker", "wid": wid},
                         options=[{"label": t, "value": t} for t in markets],
                         value=markets[0] if markets else None, clearable=False,
                         style=_dd_style("260px")),
            dcc.Dropdown(id={"t": "oe-side", "wid": wid},
                         options=[{"label": "YES", "value": "yes"},
                                  {"label": "NO", "value": "no"}],
                         value="yes", clearable=False, style=_dd_style("80px")),
            dcc.Dropdown(id={"t": "oe-action", "wid": wid},
                         options=[{"label": "BUY", "value": "buy"},
                                  {"label": "SELL", "value": "sell"}],
                         value="buy", clearable=False, style=_dd_style("90px")),
            dcc.Input(id={"t": "oe-qty", "wid": wid}, type="number", value=1,
                      min=1, max=99, step=1, style=_input_style()),
            dcc.Input(id={"t": "oe-price", "wid": wid}, type="number",
                      value=0.50, min=0.01, max=0.99, step=0.01,
                      style=_input_style("90px")),
            html.Button("PLACE LIMIT", id={"t": "oe-submit", "wid": wid},
                        n_clicks=0, style=_btn_style(AMBER if dry else GREEN)),
        ], style={"display": "flex", "gap": "8px", "alignItems": "center",
                  "flexWrap": "wrap"}),
        html.Div("DRY-RUN MODE — orders are logged, not sent." if dry
                 else "LIVE MODE — orders will be SENT to Kalshi.",
                 style={"color": (AMBER if dry else RED), "fontSize": "10px",
                        "marginTop": "6px", "fontWeight": "bold"}),
        html.Div(id={"t": "oe-result", "wid": wid},
                 style={"fontSize": "11px", "color": MUTED, "marginTop": "4px"}),
    ])


def _build_signal_explorer(wid, providers):
    games = _game_options()
    return html.Div([
        html.Div([
            dcc.Dropdown(id={"t": "se-game", "wid": wid}, options=games,
                         value=games[0]["value"] if games else None,
                         placeholder="game…", style=_dd_style("330px")),
            dcc.Dropdown(id={"t": "se-strat", "wid": wid},
                         options=_strategy_options(), value="model_edge",
                         clearable=False, style=_dd_style("250px")),
            dcc.Dropdown(id={"t": "se-exec", "wid": wid},
                         options=_exec_options(), value="taker",
                         clearable=False, style=_dd_style("170px")),
            dcc.Input(id={"t": "se-qty", "wid": wid}, type="number", value=100,
                      min=1, step=1, placeholder="qty", style=_input_style()),
            html.Button("RUN", id={"t": "se-run", "wid": wid}, n_clicks=0,
                        style=_btn_style(GREEN)),
        ], style={"display": "flex", "gap": "6px", "flexWrap": "wrap",
                  "alignItems": "center"}),
        html.Div("pick a game + strategy, hit RUN",
                 id={"t": "se-status", "wid": wid},
                 style={"color": MUTED, "fontSize": "10px", "margin": "4px 2px"}),
        dcc.Graph(id={"t": "se-graph", "wid": wid},
                  config={"displayModeBar": False}, style={"height": "430px"},
                  figure=panels.replay_figure(None, None, "")),
        html.Div(id={"t": "se-trades", "wid": wid}, style={"marginTop": "6px"}),
        dcc.Store(id={"t": "se-req", "wid": wid}),
        dcc.Store(id={"t": "se-done", "wid": wid}),
    ])


def _build_alpha_scanner(wid, providers):
    games = _game_options()
    default = [g["value"] for g in games[:10]]
    return html.Div([
        html.Div([
            dcc.Dropdown(id={"t": "ap-games", "wid": wid}, options=games,
                         value=default, multi=True, placeholder="games…",
                         style=_dd_style("100%")),
        ]),
        html.Div([
            html.Button("ALL GAMES", id={"t": "ap-all", "wid": wid}, n_clicks=0,
                        style=_btn_style()),
            dcc.Dropdown(id={"t": "ap-strat", "wid": wid},
                         options=_strategy_options(), value="model_edge",
                         clearable=False, style=_dd_style("250px")),
            dcc.Dropdown(id={"t": "ap-exec", "wid": wid},
                         options=_exec_options(), value="taker",
                         clearable=False, style=_dd_style("170px")),
            dcc.Input(id={"t": "ap-qty", "wid": wid}, type="number", value=100,
                      min=1, step=1, style=_input_style()),
            html.Button("RUN SWEEP", id={"t": "ap-run", "wid": wid}, n_clicks=0,
                        style=_btn_style(GREEN)),
        ], style={"display": "flex", "gap": "6px", "flexWrap": "wrap",
                  "alignItems": "center", "marginTop": "4px"}),
        html.Div("pick games + a strategy, hit RUN SWEEP — replays run in the "
                 "background and cache, so re-runs are instant",
                 id={"t": "ap-status", "wid": wid},
                 style={"color": MUTED, "fontSize": "10px", "margin": "4px 2px"}),
        html.Div(id={"t": "ap-tiles", "wid": wid}),
        dcc.Graph(id={"t": "ap-graph", "wid": wid},
                  config={"displayModeBar": False}, style={"height": "440px"},
                  figure=panels.equity_figure([], 0)),
        dcc.Store(id={"t": "ap-req", "wid": wid}),
        dcc.Store(id={"t": "ap-done", "wid": wid}),
    ])


WIDGET_TYPES: dict[str, WidgetType] = {w.key: w for w in [
    WidgetType("account", "Account", _build_account, basis="100%", needs_live=True),
    WidgetType("market_watch", "Market Watch — watchlist", _build_market_watch,
               basis="560px", needs_live=True),
    WidgetType("orderbook", "Order Book", _build_orderbook, needs_live=True),
    WidgetType("winprob", "Win-Prob — Kalshi vs Model (live)", _build_winprob,
               basis="560px", needs_live=True),
    WidgetType("positions", "Positions", _build_positions, needs_live=True),
    WidgetType("blotter", "Blotter — Fills & Realized P&L", _build_blotter,
               basis="560px", needs_live=True),
    WidgetType("signal_feed", "Signal Feed", _build_signal_feed, needs_live=True),
    WidgetType("order_entry", "Order Entry", _build_order_entry, needs_live=True),
    WidgetType("signal_explorer", "Signal Explorer — historical replay",
               _build_signal_explorer, basis="100%"),
    WidgetType("alpha_scanner", "Alpha Scanner — multi-game strategy backtest",
               _build_alpha_scanner, basis="100%"),
]}


# ── callbacks (registered once; all pattern-matching) ─────────────────────────
def register_callbacks(app, providers) -> None:

    def tick_widget(token: str, render):
        """Bind a per-instance body refresh to the global interval. render
        receives the matched component id (so tables can get unique ids)."""
        @app.callback(
            Output({"t": token, "wid": MATCH}, "children"),
            Input("tick", "n_intervals"),
            State({"t": token, "wid": MATCH}, "id"),
        )
        def _cb(_n, _id, _render=render):
            try:
                return _render(_id)
            except Exception as e:
                return _msg(f"unavailable: {e}", RED)

    # account / positions / blotter — straight provider reads
    tick_widget("acct-body",
                lambda _id: panels.account_tiles(providers.portfolio.account()))
    tick_widget("pos-body",
                lambda _id: panels.positions_table(
                    providers.portfolio.positions(),
                    table_id={"t": "pos-table", "wid": _id["wid"]}))
    tick_widget("blot-body",
                lambda _id: panels.blotter_table(
                    providers.portfolio.fills(),
                    table_id={"t": "blot-table", "wid": _id["wid"]}))

    # market watch body — watchlist-filtered
    @app.callback(
        Output({"t": "mw-body", "wid": MATCH}, "children"),
        Input("tick", "n_intervals"),
        State("watchlist", "data"),
        State({"t": "mw-body", "wid": MATCH}, "id"),
    )
    def _mw_body(_n, watchlist, _id):
        try:
            rows = panels.market_watch_rows(providers.market, providers.signals,
                                            providers, tickers=watchlist or [])
            if not rows:
                return _msg("watchlist empty — add tickers above")
            return panels.market_watch_table(rows, table_id={"t": "mw-table",
                                                             "wid": _id["wid"]})
        except Exception as e:
            return _msg(f"unavailable: {e}", RED)

    # watchlist editing: dropdown is the source of truth
    @app.callback(
        Output("watchlist", "data"),
        Input({"t": "mw-sel", "wid": ALL}, "value"),
        prevent_initial_call=True,
    )
    def _watchlist_sync(values):
        trg = dash.ctx.triggered
        if not trg:
            raise PreventUpdate
        val = trg[0]["value"]
        return sorted(set(val or []))

    # free-text add: push the typed ticker into this instance's dropdown
    @app.callback(
        Output({"t": "mw-sel", "wid": MATCH}, "options"),
        Output({"t": "mw-sel", "wid": MATCH}, "value"),
        Input({"t": "mw-add-btn", "wid": MATCH}, "n_clicks"),
        State({"t": "mw-add-input", "wid": MATCH}, "value"),
        State({"t": "mw-sel", "wid": MATCH}, "options"),
        State({"t": "mw-sel", "wid": MATCH}, "value"),
        prevent_initial_call=True,
    )
    def _mw_add(n, text, options, value):
        if not n or not text:
            raise PreventUpdate
        t = text.strip().upper()
        opts = options or []
        if t not in {o["value"] for o in opts}:
            opts = opts + [{"label": t, "value": t}]
        val = sorted(set(value or []) | {t})
        return opts, val

    # orderbook graph
    @app.callback(
        Output({"t": "ob-graph", "wid": MATCH}, "figure"),
        Input("tick", "n_intervals"),
        State({"t": "ob-ticker", "wid": MATCH}, "value"),
    )
    def _ob(_n, ticker):
        if not ticker:
            return panels.orderbook_figure(None)
        try:
            return panels.orderbook_figure(providers.market.orderbook(ticker, depth=10))
        except Exception:
            return panels.orderbook_figure(None)

    # live win-prob graph (history recorded by the global recorder in app.py)
    @app.callback(
        Output({"t": "wp-graph", "wid": MATCH}, "figure"),
        Input("tick", "n_intervals"),
        State({"t": "wp-ticker", "wid": MATCH}, "value"),
    )
    def _wp(_n, ticker):
        if not ticker:
            return panels.winprob_figure([], "—")
        return panels.winprob_figure(WINPROB_HISTORY.get(ticker, []), ticker)

    # keep per-widget ticker pickers in sync with the watchlist
    for token in ("ob-ticker", "wp-ticker", "oe-ticker"):
        @app.callback(
            Output({"t": token, "wid": MATCH}, "options"),
            Input("watchlist", "data"),
            State({"t": token, "wid": MATCH}, "id"),
        )
        def _opts(watchlist, _id):
            return _ticker_options(providers, watchlist)

    # signal feed — current signal per watchlist ticker
    @app.callback(
        Output({"t": "sig-body", "wid": MATCH}, "children"),
        Input("tick", "n_intervals"),
        State("watchlist", "data"),
        State({"t": "sig-body", "wid": MATCH}, "id"),
    )
    def _sigs(_n, watchlist, _id):
        sigs = []
        for tk in (watchlist or []):
            try:
                glue = providers.game_for_ticker(tk)
                if glue is None:
                    continue
                gid, home, away, pregame = glue
                gs = providers.games.game_state(gid)
                if gs is None:
                    continue
                sigs.append(providers.signals.signal(
                    tk, gs, providers.market.quote(tk), pregame))
            except Exception:
                continue
        return panels.signal_feed(sigs)

    # order entry
    @app.callback(
        Output({"t": "oe-result", "wid": MATCH}, "children"),
        Input({"t": "oe-submit", "wid": MATCH}, "n_clicks"),
        State({"t": "oe-ticker", "wid": MATCH}, "value"),
        State({"t": "oe-side", "wid": MATCH}, "value"),
        State({"t": "oe-action", "wid": MATCH}, "value"),
        State({"t": "oe-qty", "wid": MATCH}, "value"),
        State({"t": "oe-price", "wid": MATCH}, "value"),
        prevent_initial_call=True,
    )
    def _place(n_clicks, ticker, side, action, qty, price):
        if not n_clicks or not ticker:
            raise PreventUpdate
        try:
            qty = int(qty or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            return "⚠ qty must be a positive integer"
        order = Order(ticker=ticker, side=side, action=action, qty=qty,
                      order_type=config.RISK.default_order_type,
                      limit_price=round(float(price), 2) if price is not None else None)
        try:
            placed = providers.executor.place(order)
        except Exception as e:
            return f"⚠ order failed: {e}"
        prefix = "DRY-RUN logged" if config.RISK.dry_run else "SENT"
        return (f"{prefix}: {placed.order_id} · {placed.action} {placed.qty} "
                f"{placed.side} {placed.ticker} @ {placed.limit_price} "
                f"→ status={placed.status}")

    # ── signal explorer ───────────────────────────────────────────────────────
    @app.callback(
        Output({"t": "se-req", "wid": MATCH}, "data"),
        Input({"t": "se-run", "wid": MATCH}, "n_clicks"),
        State({"t": "se-game", "wid": MATCH}, "value"),
        State({"t": "se-strat", "wid": MATCH}, "value"),
        State({"t": "se-exec", "wid": MATCH}, "value"),
        State({"t": "se-qty", "wid": MATCH}, "value"),
        prevent_initial_call=True,
    )
    def _se_submit(n, code, strat, exc, qty):
        if not n or not code or not strat or not exc:
            raise PreventUpdate
        qty = max(1, int(qty or 100))
        history.REPLAYS.single(code, strat, exc, qty)   # spawn (or hit cache)
        return {"code": code, "strat": strat, "exec": exc, "qty": qty, "n": n}

    @app.callback(
        Output({"t": "se-graph", "wid": MATCH}, "figure"),
        Output({"t": "se-trades", "wid": MATCH}, "children"),
        Output({"t": "se-status", "wid": MATCH}, "children"),
        Output({"t": "se-done", "wid": MATCH}, "data"),
        Input("tick", "n_intervals"),
        Input({"t": "se-req", "wid": MATCH}, "data"),
        State({"t": "se-done", "wid": MATCH}, "data"),
    )
    def _se_render(_n, req, done):
        if not req:
            raise PreventUpdate
        key = [req["code"], req["strat"], req["exec"], req["qty"], req["n"]]
        job = history.REPLAYS.single(req["code"], req["strat"], req["exec"],
                                     req["qty"])
        if job.status == "running":
            return no_update, no_update, f"⏳ {job.progress or 'replaying…'}", no_update
        if done == key:
            raise PreventUpdate          # already rendered this exact run
        if job.status == "error":
            return (panels.replay_figure(None, None, ""), "",
                    html.Span(f"✗ replay failed — {job.error.splitlines()[-1]}",
                              style={"color": RED}), key)
        r = job.result
        try:
            entry_edge = registry.cfg_for(req["strat"])["entry_edge"]
        except Exception:
            entry_edge = 0.08
        title = (f"{r['label']} — {r['strategy']} · exec={r['exec']} · "
                 f"pregame {r['pregame']:.2f}")
        fig = panels.replay_figure(r["pdf"], r["tdf"], title, entry_edge)
        tdf = r["tdf"]
        net = float(tdf["net"].sum()) if len(tdf) else 0.0
        status = html.Span(
            f"✓ {len(tdf)} trades · net ${net:+.2f} ({req['qty']} contracts/trade)",
            style={"color": GREEN if net >= 0 else RED, "fontWeight": "bold"})
        return fig, panels.replay_trades_table(tdf), status, key

    # ── alpha scanner ─────────────────────────────────────────────────────────
    @app.callback(
        Output({"t": "ap-games", "wid": MATCH}, "value"),
        Input({"t": "ap-all", "wid": MATCH}, "n_clicks"),
        prevent_initial_call=True,
    )
    def _ap_all(n):
        if not n:
            raise PreventUpdate
        return [g.code for g in history.CATALOG.games()]

    @app.callback(
        Output({"t": "ap-req", "wid": MATCH}, "data"),
        Input({"t": "ap-run", "wid": MATCH}, "n_clicks"),
        State({"t": "ap-games", "wid": MATCH}, "value"),
        State({"t": "ap-strat", "wid": MATCH}, "value"),
        State({"t": "ap-exec", "wid": MATCH}, "value"),
        State({"t": "ap-qty", "wid": MATCH}, "value"),
        prevent_initial_call=True,
    )
    def _ap_submit(n, codes, strat, exc, qty):
        if not n or not codes or not strat or not exc:
            raise PreventUpdate
        qty = max(1, int(qty or 100))
        codes = tuple(sorted(codes))
        history.REPLAYS.batch(codes, strat, exc, qty)
        return {"codes": list(codes), "strat": strat, "exec": exc,
                "qty": qty, "n": n}

    @app.callback(
        Output({"t": "ap-graph", "wid": MATCH}, "figure"),
        Output({"t": "ap-tiles", "wid": MATCH}, "children"),
        Output({"t": "ap-status", "wid": MATCH}, "children"),
        Output({"t": "ap-done", "wid": MATCH}, "data"),
        Input("tick", "n_intervals"),
        Input({"t": "ap-req", "wid": MATCH}, "data"),
        State({"t": "ap-done", "wid": MATCH}, "data"),
    )
    def _ap_render(_n, req, done):
        if not req:
            raise PreventUpdate
        key = [req["codes"], req["strat"], req["exec"], req["qty"], req["n"]]
        job = history.REPLAYS.batch(tuple(req["codes"]), req["strat"],
                                    req["exec"], req["qty"])
        if job.status == "running":
            return (no_update, no_update,
                    f"⏳ sweeping… {job.progress}", no_update)
        if done == key:
            raise PreventUpdate
        if job.status == "error":
            return (panels.equity_figure([], 0), "",
                    html.Span(f"✗ sweep failed — {job.error.splitlines()[-1]}",
                              style={"color": RED}), key)
        per_game = job.result["per_game"]
        fig = panels.equity_figure(per_game, req["qty"])
        tiles = panels.perf_summary_tiles(per_game)
        errs = [g for g in per_game if g.get("error")]
        status = html.Span(
            f"✓ {len(per_game)} games replayed — {req['strat']} / {req['exec']}"
            + (f" · {len(errs)} failed" if errs else ""),
            style={"color": GREEN, "fontWeight": "bold"})
        return fig, tiles, status, key
