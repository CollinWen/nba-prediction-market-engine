"""
Plotly Dash terminal app — modular "Bloomberg terminal" workspace.

The terminal is a WORKSPACE of widget instances (dashboard/widgets.py): pick a
widget type from the toolbar and ADD it; every widget carries its own ✕ and its
own selections, so you can run two order books on different tickers or two
signal explorers on different historical games side by side.

Live widgets (market watch / order book / win-prob / positions / blotter /
signal feed / order entry / account) refresh off one dcc.Interval against the
providers bundle. Historical widgets (Signal Explorer, Alpha Scanner) replay
archived games through registered strategies via backtest/session_replay — the
same Strategy protocol the live ExecutionEngine runs — in background threads,
so the UI never blocks.

The app is built against the core.interfaces Protocols and takes a `providers`
bundle, so the same code runs on mocks or the real providers. See
run_terminal.py for the single place the swap happens. With no live providers
(offline), the historical widgets still work — the terminal doubles as a
backtest workbench.
"""
from __future__ import annotations

import datetime as dt
import uuid

import dash
from dash import ALL, Input, Output, Patch, State, dcc, html
from dash.exceptions import PreventUpdate

from _bootstrap import boot
boot()

import config                                   # noqa: E402
from dashboard import widgets                   # noqa: E402
from dashboard.panels import (AMBER, BG, BLUE, FG, FONT, GREEN, GRID, MUTED,
                              RED)              # noqa: E402

# the workspace you get on boot; everything else is addable from the toolbar
DEFAULT_LIVE = ["account", "market_watch", "winprob", "signal_feed",
                "blotter", "signal_explorer"]
DEFAULT_OFFLINE = ["signal_explorer", "alpha_scanner"]


def _new_wid() -> str:
    return uuid.uuid4().hex[:8]


def create_app(providers) -> dash.Dash:
    app = dash.Dash(__name__, title="Kalshi NBA Terminal",
                    suppress_callback_exceptions=True)

    markets = widgets.list_markets(providers)
    live_feed = bool(markets)
    default_types = DEFAULT_LIVE if live_feed else DEFAULT_OFFLINE

    workspace = [{"wid": _new_wid(), "wt": wt} for wt in default_types]
    children = [widgets.widget_shell(w["wid"], widgets.WIDGET_TYPES[w["wt"]],
                                     providers) for w in workspace]

    dry = config.RISK.dry_run
    add_options = [{"label": w.title, "value": w.key, "title": w.key}
                   for w in widgets.WIDGET_TYPES.values()]

    app.layout = html.Div([
        dcc.Store(id="workspace", data=workspace),
        dcc.Store(id="watchlist", data=markets),
        dcc.Interval(id="tick", interval=config.REFRESH_MS, n_intervals=0),
        html.Div(id="winprob-recorder", style={"display": "none"}),

        # header
        html.Div([
            html.Span("KALSHI · NBA TERMINAL", style={
                "color": FG, "fontWeight": "bold", "fontSize": "15px",
                "letterSpacing": "1px"}),
            html.Span(id="clock", style={"color": MUTED, "marginLeft": "16px"}),
            html.Span(("● DRY-RUN" if dry else "● LIVE"), style={
                "color": (AMBER if dry else RED), "marginLeft": "16px",
                "fontWeight": "bold"}),
            html.Span(("● FEED" if live_feed else "● OFFLINE — historical only"),
                      style={"color": (GREEN if live_feed else MUTED),
                             "marginLeft": "16px", "fontWeight": "bold"}),
        ], style={"padding": "8px 14px", "backgroundColor": BG,
                  "borderBottom": f"2px solid {GRID}", "fontFamily": FONT}),

        # toolbar — add widgets to the workspace
        html.Div([
            html.Span("ADD WIDGET", style={
                "color": MUTED, "fontSize": "10px", "fontWeight": "bold",
                "textTransform": "uppercase", "letterSpacing": "0.5px"}),
            dcc.Dropdown(id="add-widget-type", options=add_options,
                         value="signal_explorer", clearable=False,
                         style={"width": "380px", "color": "#000",
                                "fontSize": "11px"}),
            html.Button("＋ ADD", id="add-widget-btn", n_clicks=0, style={
                "backgroundColor": BLUE, "color": "#000", "border": "none",
                "padding": "6px 16px", "fontFamily": FONT, "fontWeight": "bold",
                "cursor": "pointer", "borderRadius": "3px"}),
        ], style={"display": "flex", "gap": "10px", "alignItems": "center",
                  "padding": "8px 14px", "backgroundColor": BG,
                  "borderBottom": f"1px solid {GRID}", "fontFamily": FONT}),

        # the workspace
        html.Div(children, id="workspace-area",
                 style={"display": "flex", "flexWrap": "wrap",
                        "alignItems": "flex-start", "padding": "4px"}),

    ], style={"backgroundColor": BG, "minHeight": "100vh",
              "fontFamily": FONT, "paddingBottom": "20px"})

    _register_workspace_callbacks(app, providers)
    widgets.register_callbacks(app, providers)
    return app


def _register_workspace_callbacks(app, providers):

    @app.callback(Output("clock", "children"), Input("tick", "n_intervals"))
    def _clock(_n):
        return dt.datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

    # sample win-prob history for every watchlist ticker each tick (the live
    # win-prob widgets read widgets.WINPROB_HISTORY)
    @app.callback(
        Output("winprob-recorder", "children"),
        Input("tick", "n_intervals"),
        State("watchlist", "data"),
    )
    def _record(_n, watchlist):
        for tk in (watchlist or []):
            widgets.record_winprob(providers, tk)
        return ""

    # add a widget instance — Patch appends, so existing widgets keep state
    @app.callback(
        Output("workspace", "data"),
        Output("workspace-area", "children"),
        Input("add-widget-btn", "n_clicks"),
        State("add-widget-type", "value"),
        State("workspace", "data"),
        prevent_initial_call=True,
    )
    def _add(n, wtype, workspace):
        if not n or wtype not in widgets.WIDGET_TYPES:
            raise PreventUpdate
        wid = _new_wid()
        workspace = (workspace or []) + [{"wid": wid, "wt": wtype}]
        patch = Patch()
        patch.append(widgets.widget_shell(wid, widgets.WIDGET_TYPES[wtype],
                                          providers))
        return workspace, patch

    # close a widget — Patch deletes just that child
    @app.callback(
        Output("workspace", "data", allow_duplicate=True),
        Output("workspace-area", "children", allow_duplicate=True),
        Input({"t": "w-close", "wid": ALL}, "n_clicks"),
        State("workspace", "data"),
        prevent_initial_call=True,
    )
    def _close(_clicks, workspace):
        trg = dash.ctx.triggered
        if not trg or not trg[0]["value"]:        # ignore creation-time fires
            raise PreventUpdate
        wid = dash.ctx.triggered_id["wid"]
        workspace = workspace or []
        idx = next((i for i, w in enumerate(workspace) if w["wid"] == wid), None)
        if idx is None:
            raise PreventUpdate
        patch = Patch()
        del patch[idx]
        return workspace[:idx] + workspace[idx + 1:], patch
