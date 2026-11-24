"""
Plotly figure / Dash table builders for the 7 terminal panels.

Each builder takes plain interface dataclasses (Quote, OrderBook, Position,
Fill, Signal, AccountSummary, GameState) and returns either a Plotly Figure or
a Dash component, so the panels stay decoupled from any data source.

Bloomberg-ish: dark background, monospace, tight margins, green/red by sign.
"""
from __future__ import annotations

from _bootstrap import boot
boot()

from typing import Optional

import plotly.graph_objects as go
from dash import dash_table, html

from core.interfaces import (
    AccountSummary,
    Fill,
    GameState,
    OrderBook,
    Position,
    Quote,
    Signal,
)

# ── Palette ───────────────────────────────────────────────────────────────────
BG = "#0d1117"
PANEL_BG = "#11161d"
GRID = "#222b36"
FG = "#c9d1d9"
MUTED = "#7d8590"
GREEN = "#2ec27e"
RED = "#f0506e"
BLUE = "#58a6ff"
AMBER = "#f5a623"
PURPLE_EDGE = "#9b59b6"
FONT = "Consolas, 'Courier New', monospace"

_BASE_LAYOUT = dict(
    paper_bgcolor=PANEL_BG,
    plot_bgcolor=PANEL_BG,
    font=dict(family=FONT, color=FG, size=11),
    margin=dict(l=40, r=10, t=28, b=28),
    hovermode="closest",
)


def _sign_color(x: float) -> str:
    if x > 0:
        return GREEN
    if x < 0:
        return RED
    return MUTED


def _empty_fig(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**_BASE_LAYOUT)
    fig.add_annotation(text=msg, showarrow=False, font=dict(color=MUTED, size=13))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


# ── 1. Market Watch (table) ───────────────────────────────────────────────────
def market_watch_table(rows: list[dict], table_id="market-watch"):
    """rows: list of dicts with keys
    ticker, bid, ask, last, mid, volume, model_prob, edge.
    table_id may be a dict (pattern-matching) so widget instances stay unique."""
    style_data_conditional = [
        {
            "if": {"filter_query": "{edge} > 0", "column_id": "edge"},
            "color": GREEN, "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{edge} < 0", "column_id": "edge"},
            "color": RED, "fontWeight": "bold",
        },
    ]
    return dash_table.DataTable(
        id=table_id,
        columns=[
            {"name": "Ticker", "id": "ticker"},
            {"name": "Bid", "id": "bid"},
            {"name": "Ask", "id": "ask"},
            {"name": "Last", "id": "last"},
            {"name": "Mid", "id": "mid"},
            {"name": "Vol", "id": "volume"},
            {"name": "Model", "id": "model_prob"},
            {"name": "Edge", "id": "edge"},
        ],
        data=rows,
        row_selectable="single",
        selected_rows=[],
        style_as_list_view=True,
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": BG, "color": MUTED, "fontWeight": "bold",
            "border": f"1px solid {GRID}", "fontFamily": FONT,
            "textTransform": "uppercase", "fontSize": "10px",
        },
        style_cell={
            "backgroundColor": PANEL_BG, "color": FG, "fontFamily": FONT,
            "fontSize": "11px", "padding": "3px 8px", "border": f"1px solid {GRID}",
            "textAlign": "right",
        },
        style_cell_conditional=[
            {"if": {"column_id": "ticker"}, "textAlign": "left", "minWidth": "210px"},
        ],
        style_data_conditional=style_data_conditional,
    )


def market_watch_rows(provider, signals_engine, providers_bundle,
                      tickers: Optional[list[str]] = None) -> list[dict]:
    """Builds market-watch rows from a MarketData provider plus signal engine.
    tickers limits the rows to a watchlist; None = every open NBA market."""
    rows = []
    for ticker in (tickers if tickers is not None else provider.list_nba_markets()):
        try:
            q = provider.quote(ticker)
        except Exception:
            continue
        glue = providers_bundle.game_for_ticker(ticker)
        model_prob = None
        edge = 0.0
        if glue is not None and signals_engine is not None:
            gid, home, away, pregame = glue
            gs = providers_bundle.games.game_state(gid)
            if gs is not None:
                model_prob = signals_engine.model_prob_home(gs, pregame)
                edge = model_prob - q.mid
        rows.append({
            "ticker": ticker,
            "bid": f"{q.bid:.2f}",
            "ask": f"{q.ask:.2f}",
            "last": f"{q.last:.2f}",
            "mid": f"{q.mid:.2f}",
            "volume": f"{int(q.volume):,}",
            "model_prob": f"{model_prob:.2f}" if model_prob is not None else "-",
            "edge": round(edge, 3),
        })
    return rows


# ── 2. Order Book (depth chart) ───────────────────────────────────────────────
def orderbook_figure(ob: Optional[OrderBook]) -> go.Figure:
    if ob is None or (not ob.yes_bids and not ob.yes_asks):
        return _empty_fig("no order book")
    fig = go.Figure()
    bid_prices = [lvl.price for lvl in ob.yes_bids]
    bid_qtys = [lvl.qty for lvl in ob.yes_bids]
    ask_prices = [lvl.price for lvl in ob.yes_asks]
    ask_qtys = [lvl.qty for lvl in ob.yes_asks]

    fig.add_trace(go.Bar(
        x=bid_prices, y=bid_qtys, name="Bids", marker_color=GREEN,
        opacity=0.85, hovertemplate="bid %{x:.2f}<br>%{y} contracts<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=ask_prices, y=ask_qtys, name="Asks", marker_color=RED,
        opacity=0.85, hovertemplate="ask %{x:.2f}<br>%{y} contracts<extra></extra>",
    ))
    fig.update_layout(
        **_BASE_LAYOUT,
        barmode="overlay",
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=10)),
        title=dict(text=f"DEPTH — {ob.ticker}", font=dict(size=11, color=MUTED)),
    )
    fig.update_xaxes(title="price ($)", gridcolor=GRID, zeroline=False,
                     tickformat=".2f", color=MUTED)
    fig.update_yaxes(title="qty", gridcolor=GRID, zeroline=False, color=MUTED)
    return fig


# ── 3. Win-Prob Chart (Kalshi mid vs Model + score subplot) ────────────────
def winprob_figure(history: list[dict], ticker: str) -> go.Figure:
    """history: list of dicts with ts, kalshi_mid, model_prob, score_diff."""
    if not history:
        return _empty_fig("no win-prob history yet")
    ts = [h["ts"] for h in history]
    kalshi = [h["kalshi_mid"] for h in history]
    model = [h["model_prob"] for h in history]
    diff = [h["score_diff"] for h in history]

    from plotly.subplots import make_subplots
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.68, 0.32],
        vertical_spacing=0.06,
        subplot_titles=(f"WIN PROB (home) — {ticker}", "SCORE DIFF (home − away)"),
    )
    fig.add_trace(go.Scatter(
        x=ts, y=kalshi, name="Kalshi mid", mode="lines",
        line=dict(color=BLUE, width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=ts, y=model, name="Model", mode="lines",
        line=dict(color=AMBER, width=2, dash="dot")), row=1, col=1)
    fig.add_hline(y=0.5, line=dict(color=GRID, width=1, dash="dash"), row=1, col=1)

    bar_colors = [GREEN if d >= 0 else RED for d in diff]
    fig.add_trace(go.Bar(
        x=ts, y=diff, name="score diff", marker_color=bar_colors,
        showlegend=False), row=2, col=1)
    fig.add_hline(y=0, line=dict(color=GRID, width=1), row=2, col=1)

    layout = dict(_BASE_LAYOUT)
    layout["margin"] = dict(l=40, r=10, t=40, b=28)
    fig.update_layout(
        **layout,
        legend=dict(orientation="h", y=1.14, x=0, font=dict(size=10)),
    )
    fig.update_yaxes(range=[0, 1], tickformat=".0%", gridcolor=GRID,
                     color=MUTED, row=1, col=1)
    fig.update_yaxes(gridcolor=GRID, color=MUTED, row=2, col=1)
    fig.update_xaxes(gridcolor=GRID, color=MUTED, row=2, col=1)
    for ann in fig.layout.annotations:
        ann.font = dict(size=10, color=MUTED)
    return fig


# ── 4. Positions (table) ──────────────────────────────────────────────────────
def positions_table(positions: list[Position], table_id="positions-table"):
    rows = []
    for p in positions:
        rows.append({
            "ticker": p.ticker,
            "side": p.side.upper(),
            "qty": p.qty,
            "avg": f"{p.avg_price:.2f}",
            "mark": f"{p.mark:.2f}",
            "upnl": round(p.unrealized, 2),
        })
    return dash_table.DataTable(
        id=table_id,
        columns=[
            {"name": "Ticker", "id": "ticker"},
            {"name": "Side", "id": "side"},
            {"name": "Qty", "id": "qty"},
            {"name": "Avg", "id": "avg"},
            {"name": "Mark", "id": "mark"},
            {"name": "uPnL $", "id": "upnl"},
        ],
        data=rows,
        style_as_list_view=True,
        style_header=_table_header(),
        style_cell=_table_cell(),
        style_cell_conditional=[
            {"if": {"column_id": "ticker"}, "textAlign": "left", "minWidth": "210px"},
        ],
        style_data_conditional=[
            {"if": {"filter_query": "{upnl} > 0", "column_id": "upnl"},
             "color": GREEN, "fontWeight": "bold"},
            {"if": {"filter_query": "{upnl} < 0", "column_id": "upnl"},
             "color": RED, "fontWeight": "bold"},
        ],
    )


# ── 5. Blotter (fills + realized P&L) ─────────────────────────────────────────
def blotter_table(fills: list[Fill], table_id="blotter-table"):
    """Shows fills newest-first with per-trade realized P&L on closing sells.

    Realized P&L is computed with a simple per-ticker average-cost FIFO-ish
    model: buys build cost basis, sells realize against the running avg.
    """
    realized_by_fill = _realized_pnl(fills)
    rows = []
    for f in sorted(fills, key=lambda x: x.ts, reverse=True):
        rpnl = realized_by_fill.get(f.fill_id)
        rows.append({
            "time": f.ts.strftime("%H:%M:%S"),
            "ticker": f.ticker,
            "act": f.action.upper(),
            "side": f.side.upper(),
            "qty": f.qty,
            "price": f"{f.price:.2f}",
            "fee": f"{f.fee:.2f}",
            "rpnl": round(rpnl, 2) if rpnl is not None else "-",
        })
    return dash_table.DataTable(
        id=table_id,
        columns=[
            {"name": "Time", "id": "time"},
            {"name": "Ticker", "id": "ticker"},
            {"name": "Act", "id": "act"},
            {"name": "Side", "id": "side"},
            {"name": "Qty", "id": "qty"},
            {"name": "Px", "id": "price"},
            {"name": "Fee", "id": "fee"},
            {"name": "rPnL $", "id": "rpnl"},
        ],
        data=rows,
        page_size=12,
        style_as_list_view=True,
        style_header=_table_header(),
        style_cell=_table_cell(),
        style_cell_conditional=[
            {"if": {"column_id": "ticker"}, "textAlign": "left", "minWidth": "200px"},
        ],
        style_data_conditional=[
            {"if": {"filter_query": "{rpnl} > 0", "column_id": "rpnl"},
             "color": GREEN, "fontWeight": "bold"},
            {"if": {"filter_query": "{rpnl} < 0", "column_id": "rpnl"},
             "color": RED, "fontWeight": "bold"},
            {"if": {"filter_query": '{act} = "BUY"', "column_id": "act"},
             "color": GREEN},
            {"if": {"filter_query": '{act} = "SELL"', "column_id": "act"},
             "color": RED},
        ],
    )


def _realized_pnl(fills: list[Fill]) -> dict[str, float]:
    """Average-cost realized P&L attributed to each closing (sell) fill_id."""
    out: dict[str, float] = {}
    book: dict[str, dict] = {}  # ticker -> {qty, avg}
    for f in sorted(fills, key=lambda x: x.ts):
        b = book.setdefault(f.ticker, {"qty": 0, "avg": 0.0})
        if f.action == "buy":
            new_qty = b["qty"] + f.qty
            if new_qty > 0:
                b["avg"] = (b["avg"] * b["qty"] + f.price * f.qty) / new_qty
            b["qty"] = new_qty
        else:  # sell -> realize vs running avg
            pnl = (f.price - b["avg"]) * f.qty - f.fee
            out[f.fill_id] = pnl
            b["qty"] = max(0, b["qty"] - f.qty)
    return out


# ── 6. Signal Feed (list) ─────────────────────────────────────────────────────
def signal_feed(signals: list[Signal]):
    if not signals:
        return html.Div("no signals", style={"color": MUTED, "padding": "8px"})
    items = []
    for s in sorted(signals, key=lambda x: x.ts, reverse=True):
        color = {
            "enter_yes": GREEN, "enter_no": RED,
            "exit": AMBER, "hold": MUTED,
        }.get(s.action, FG)
        edge_color = _sign_color(s.edge)
        items.append(html.Div([
            html.Span(s.ts.strftime("%H:%M:%S "), style={"color": MUTED}),
            html.Span(f"{s.action.upper():<10}", style={"color": color, "fontWeight": "bold"}),
            html.Span(f"{s.ticker.split('-')[-1]:<5} ", style={"color": FG}),
            html.Span(f"edge {s.edge:+.0%} ", style={"color": edge_color}),
            html.Span(s.reason, style={"color": MUTED}),
        ], style={"fontFamily": FONT, "fontSize": "11px", "padding": "2px 6px",
                  "borderBottom": f"1px solid {GRID}", "whiteSpace": "nowrap"}))
    return html.Div(items, style={"maxHeight": "240px", "overflowY": "auto"})


# ── 7. Account (summary tiles) ────────────────────────────────────────────────
def account_tiles(acct: AccountSummary):
    def tile(label, value, color=FG):
        return html.Div([
            html.Div(label, style={"color": MUTED, "fontSize": "10px",
                                   "textTransform": "uppercase"}),
            html.Div(value, style={"color": color, "fontSize": "18px",
                                   "fontWeight": "bold"}),
        ], style={"flex": "1", "padding": "8px 12px", "backgroundColor": BG,
                  "border": f"1px solid {GRID}", "borderRadius": "4px",
                  "margin": "0 4px"})

    ks_color = RED if acct.kill_switch else GREEN
    ks_text = "TRIPPED" if acct.kill_switch else "ARMED OK"
    return html.Div([
        tile("Balance", f"${acct.balance_usd:,.2f}", FG),
        tile("Exposure", f"${acct.total_exposure_usd:,.2f}", AMBER),
        tile("Realized", f"${acct.realized_pnl_usd:+,.2f}",
             _sign_color(acct.realized_pnl_usd)),
        tile("Unrealized", f"${acct.unrealized_pnl_usd:+,.2f}",
             _sign_color(acct.unrealized_pnl_usd)),
        tile("Day P&L", f"${acct.day_pnl_usd:+,.2f}",
             _sign_color(acct.day_pnl_usd)),
        tile("Kill Switch", ks_text, ks_color),
    ], style={"display": "flex", "flexWrap": "wrap"})


# ── 8. Replay / backtest panels (Signal Explorer + Alpha Scanner widgets) ─────
def replay_figure(pdf, tdf, title: str, entry_edge: float = 0.08) -> go.Figure:
    """Replayed game: Kalshi YES price vs regenerated model prob with trade
    markers (top), model-minus-market edge vs the entry threshold (bottom).

    pdf: poll frame from run_replay (ts, yes_px, model, ...).
    tdf: trade frame from run_replay (entry_epoch, exit_epoch, entry, exit,
         dir, net, reason, ...). May be empty.
    """
    import pandas as pd
    from plotly.subplots import make_subplots
    if pdf is None or len(pdf) == 0:
        return _empty_fig("no replay data")

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.66, 0.34], vertical_spacing=0.07,
                        subplot_titles=(title, "SIGNAL — model edge vs entry threshold"))
    fig.add_trace(go.Scatter(x=pdf["ts"], y=pdf["yes_px"], name="Kalshi YES",
                             mode="lines", line=dict(color=BLUE, width=1.6)), row=1, col=1)
    fig.add_trace(go.Scatter(x=pdf["ts"], y=pdf["model"], name="Model",
                             mode="lines", line=dict(color=AMBER, width=1.6, dash="dot")),
                  row=1, col=1)
    fig.add_hline(y=0.5, line=dict(color=GRID, width=1, dash="dash"), row=1, col=1)

    if tdf is not None and len(tdf):
        ent_ts = pd.to_datetime(tdf["entry_epoch"], unit="s", utc=True)
        exi_ts = pd.to_datetime(tdf["exit_epoch"], unit="s", utc=True)
        ecolor = [GREEN if d > 0 else RED for d in tdf["dir"]]
        fig.add_trace(go.Scatter(
            x=ent_ts, y=tdf["entry"], mode="markers", name="entry",
            marker=dict(symbol="triangle-up", size=11, color=ecolor,
                        line=dict(color=FG, width=1)),
            text=[f"{'LONG YES' if d > 0 else 'LONG NO'} @ {p:.2f}"
                  for d, p in zip(tdf["dir"], tdf["entry"])],
            hovertemplate="%{text}<extra>entry</extra>"), row=1, col=1)
        xcolor = [GREEN if n > 0 else RED for n in tdf["net"]]
        fig.add_trace(go.Scatter(
            x=exi_ts, y=tdf["exit"], mode="markers", name="exit",
            marker=dict(symbol="x", size=10, color=xcolor),
            text=[f"{r} → ${n:+.2f}" for r, n in zip(tdf["reason"], tdf["net"])],
            hovertemplate="exit @ %{y:.2f}<br>%{text}<extra></extra>"), row=1, col=1)

    edge = pdf["model"] - pdf["yes_px"]
    fig.add_trace(go.Scatter(x=pdf["ts"], y=edge, name="edge",
                             mode="lines", line=dict(color=PURPLE_EDGE, width=1.2),
                             fill="tozeroy", fillcolor="rgba(155,89,182,0.18)"),
                  row=2, col=1)
    for y in (entry_edge, -entry_edge):
        fig.add_hline(y=y, line=dict(color=AMBER, width=1, dash="dash"), row=2, col=1)
    fig.add_hline(y=0, line=dict(color=GRID, width=1), row=2, col=1)

    layout = dict(_BASE_LAYOUT)
    layout["margin"] = dict(l=42, r=10, t=40, b=26)
    fig.update_layout(**layout, legend=dict(orientation="h", y=1.13, x=0,
                                            font=dict(size=10)))
    fig.update_yaxes(range=[0, 1], tickformat=".0%", gridcolor=GRID, color=MUTED,
                     row=1, col=1)
    fig.update_yaxes(tickformat="+.2f", gridcolor=GRID, color=MUTED, row=2, col=1)
    fig.update_xaxes(gridcolor=GRID, color=MUTED)
    for ann in fig.layout.annotations:
        ann.font = dict(size=10, color=MUTED)
    return fig


def equity_figure(per_game: list[dict], qty: int) -> go.Figure:
    """Aggregate alpha view across replayed games: cumulative net P&L over all
    trades in chronological order (top), net per game (bottom bars).
    per_game: ReplayService batch results (each with label + tdf)."""
    import pandas as pd
    from plotly.subplots import make_subplots
    rows = []
    for g in per_game:
        tdf = g.get("tdf")
        if tdf is None or not len(tdf):
            continue
        for r in tdf.itertuples():
            rows.append({"ts": r.exit_epoch, "net": r.net, "game": g["label"]})
    fig = make_subplots(rows=2, cols=1, row_heights=[0.6, 0.4],
                        vertical_spacing=0.14,
                        subplot_titles=(f"CUMULATIVE NET P&L — {qty} contracts/trade",
                                        "NET BY GAME"))
    if rows:
        df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
        df["cum"] = df["net"].cumsum()
        fig.add_trace(go.Scatter(
            x=list(range(1, len(df) + 1)), y=df["cum"], mode="lines",
            line=dict(color=BLUE, width=2, shape="hv"), name="cum net",
            text=df["game"], hovertemplate="trade %{x} · %{text}"
                                           "<br>cum $%{y:+.2f}<extra></extra>"))
        fig.add_hline(y=0, line=dict(color=GRID, width=1, dash="dash"), row=1, col=1)

    labels, nets = [], []
    for g in per_game:
        tdf = g.get("tdf")
        labels.append(g["label"])
        nets.append(float(tdf["net"].sum()) if tdf is not None and len(tdf) else 0.0)
    fig.add_trace(go.Bar(x=labels, y=nets, showlegend=False,
                         marker_color=[GREEN if n >= 0 else RED for n in nets],
                         hovertemplate="%{x}<br>$%{y:+.2f}<extra></extra>"),
                  row=2, col=1)
    fig.add_hline(y=0, line=dict(color=GRID, width=1), row=2, col=1)

    layout = dict(_BASE_LAYOUT)
    layout["margin"] = dict(l=42, r=10, t=40, b=60)
    fig.update_layout(**layout, showlegend=False)
    fig.update_xaxes(title="trade #", gridcolor=GRID, color=MUTED, row=1, col=1)
    fig.update_yaxes(title="$", gridcolor=GRID, color=MUTED)
    fig.update_xaxes(gridcolor=GRID, color=MUTED, tickfont=dict(size=8),
                     tickangle=30, row=2, col=1)
    for ann in fig.layout.annotations:
        ann.font = dict(size=10, color=MUTED)
    return fig


def replay_trades_table(tdf):
    """Trade-by-trade blotter for one replayed game."""
    import datetime as _dt
    rows = []
    if tdf is not None and len(tdf):
        for r in tdf.itertuples():
            rows.append({
                "entry_t": _dt.datetime.fromtimestamp(r.entry_epoch).strftime("%H:%M:%S"),
                "exit_t": _dt.datetime.fromtimestamp(r.exit_epoch).strftime("%H:%M:%S"),
                "dir": "YES" if r.dir > 0 else "NO",
                "qty": r.qty,
                "entry": f"{r.entry:.2f}",
                "exit": f"{r.exit:.2f}",
                "reason": r.reason,
                "net": round(float(r.net), 2),
            })
    return dash_table.DataTable(
        columns=[{"name": n, "id": i} for n, i in [
            ("In", "entry_t"), ("Out", "exit_t"), ("Dir", "dir"), ("Qty", "qty"),
            ("Entry", "entry"), ("Exit", "exit"), ("Reason", "reason"),
            ("Net $", "net")]],
        data=rows, page_size=8, style_as_list_view=True,
        style_header=_table_header(), style_cell=_table_cell(),
        style_data_conditional=[
            {"if": {"filter_query": "{net} > 0", "column_id": "net"},
             "color": GREEN, "fontWeight": "bold"},
            {"if": {"filter_query": "{net} < 0", "column_id": "net"},
             "color": RED, "fontWeight": "bold"},
        ],
    )


def perf_summary_tiles(per_game: list[dict]):
    """Headline stats for a multi-game strategy run."""
    import pandas as pd
    nets, trades, wins, losses, errors = [], 0, 0, 0, 0
    for g in per_game:
        if g.get("error"):
            errors += 1
            continue
        tdf = g.get("tdf")
        if tdf is None or not len(tdf):
            nets.append(0.0)
            continue
        nets.append(float(tdf["net"].sum()))
        trades += len(tdf)
        wins += int((tdf["net"] > 0).sum())
        losses += int((tdf["net"] < 0).sum())
    total = sum(nets)
    s = pd.Series(nets) if nets else pd.Series(dtype=float)
    win_rate = f"{wins / max(1, wins + losses):.0%}"

    def chip(label, value, color=FG):
        return html.Div([
            html.Span(label + " ", style={"color": MUTED, "fontSize": "10px",
                                          "textTransform": "uppercase"}),
            html.Span(value, style={"color": color, "fontWeight": "bold",
                                    "fontSize": "13px"}),
        ], style={"padding": "4px 10px", "backgroundColor": BG,
                  "border": f"1px solid {GRID}", "borderRadius": "3px"})

    chips = [
        chip("Total", f"${total:+,.2f}", _sign_color(total)),
        chip("Games", f"{len(nets)}" + (f" ({errors} err)" if errors else "")),
        chip("Trades", f"{trades}"),
        chip("Win rate", win_rate, _sign_color(wins - losses)),
        chip("Best game", f"${s.max():+,.2f}" if len(s) else "—", GREEN),
        chip("Worst game", f"${s.min():+,.2f}" if len(s) else "—", RED),
    ]
    return html.Div(chips, style={"display": "flex", "gap": "6px",
                                  "flexWrap": "wrap", "fontFamily": FONT,
                                  "marginBottom": "6px"})


# ── shared table styling ──────────────────────────────────────────────────────
def _table_header():
    return {
        "backgroundColor": BG, "color": MUTED, "fontWeight": "bold",
        "border": f"1px solid {GRID}", "fontFamily": FONT,
        "textTransform": "uppercase", "fontSize": "10px",
    }


def _table_cell():
    return {
        "backgroundColor": PANEL_BG, "color": FG, "fontFamily": FONT,
        "fontSize": "11px", "padding": "3px 8px", "border": f"1px solid {GRID}",
        "textAlign": "right",
    }
