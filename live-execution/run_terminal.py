"""
Entrypoint for the Kalshi NBA trading terminal.

Run from anywhere:
    .\\venv\\Scripts\\python.exe live-execution\\run_terminal.py

Constructs the data providers and launches the modular Plotly Dash workspace
(dashboard/app.py): add/remove widgets from the toolbar — live market widgets
plus the historical Signal Explorer / Alpha Scanner backtest widgets, which
work even with no live feed (USE_MOCKS, missing creds, or no games today).

By default the terminal uses dashboard/mock_providers.py (realistic fake live
data, no network/credentials). Set TERMINAL_LIVE=1 to wire real Kalshi/NBA
providers (needs Kalshi API credentials in .env).
"""
from __future__ import annotations

import os

# Put live-execution/ (and repo root) on sys.path. The package dir has a hyphen
# so it can't be imported normally; every module in this tree boots the same way.
from _bootstrap import boot
boot()

import config                              # noqa: E402
from dashboard.app import create_app       # noqa: E402

# ── PROVIDER SELECTION ────────────────────────────────────────────────────────
USE_MOCKS = os.environ.get("TERMINAL_LIVE") != "1"   # mock live feed unless opted in


def build_mock_providers():
    """The mock bundle — realistic fake NBA data, no network/credentials."""
    from dashboard.mock_providers import MockProviders
    return MockProviders(dry_run=config.RISK.dry_run)


def build_real_providers():
    """Wire in the real providers once the sibling agents land.

    Expected to return an object with attributes:
        .market     -> core.interfaces.MarketData      (kalshi_client)
        .games      -> core.interfaces.GameFeed        (nba_feed)
        .signals    -> core.interfaces.SignalEngine    (model_signal)
        .executor   -> core.interfaces.Executor        (order_manager)
        .portfolio  -> core.interfaces.Portfolio       (tracker)
    plus a `game_for_ticker(ticker)` helper returning
        (game_id, home, away, pregame_prob) | None
    used by the dashboard to glue markets to games.

    A small adapter class is the cleanest way to assemble these. Example:

        from core.kalshi_client import KalshiClient
        from core.nba_feed import NbaFeed
        from core.model_signal import ModelSignal
        from execution.order_manager import OrderManager
        from portfolio.tracker import PortfolioTracker

        class RealProviders:
            def __init__(self):
                self.market = KalshiClient()
                self.games = NbaFeed()
                self.signals = ModelSignal()
                self.executor = OrderManager(dry_run=config.RISK.dry_run)
                self.portfolio = PortfolioTracker()
            def game_for_ticker(self, ticker):
                ...  # map ticker -> (game_id, home, away, pregame_prob)
        return RealProviders()
    """
    import re
    from core.kalshi_client import KalshiClient
    from core.nba_feed import NBAFeed
    from core.model_signal import ModelSignalEngine
    from execution.order_manager import OrderManager
    from portfolio.tracker import Tracker

    MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

    class RealProviders:
        def __init__(self):
            self.market    = KalshiClient()
            self.games     = NBAFeed()
            self.signals   = ModelSignalEngine()
            self.executor  = OrderManager(client=self.market)
            self.portfolio = Tracker(client=self.market)
            self._games_cache = None

        def _todays(self):
            if self._games_cache is None:
                try: self._games_cache = self.games.todays_games()
                except Exception: self._games_cache = []
            return self._games_cache

        def game_for_ticker(self, ticker):
            # KXNBAGAME-26JUN05NYKSAS-SAS -> away=NYK, home=SAS, side=SAS
            m = re.search(r"(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})-([A-Z]{3})$", ticker)
            if not m: return None
            _,_,_,away,home,_ = m.groups()
            gid = None
            for g in self._todays():
                if g.get("home") == home or g.get("away") == away:
                    gid = g.get("game_id"); break
            # pregame prob proxy: current home-side market mid (binned to 0.05)
            pregame = 0.5
            try:
                home_ticker = re.sub(r"-[A-Z]{3}$", f"-{home}", ticker)
                q = self.market.quote(home_ticker)
                pregame = round(q.mid / 0.05) * 0.05
            except Exception:
                pass
            return (gid, home, away, pregame)

    return RealProviders()


def main():
    providers = build_mock_providers() if USE_MOCKS else build_real_providers()
    app = create_app(providers)
    mode = "MOCK" if USE_MOCKS else "REAL"
    dry = "DRY-RUN" if config.RISK.dry_run else "LIVE"
    print(f"[run_terminal] providers={mode}  risk={dry}  "
          f"serving on http://{config.DASH_HOST}:{config.DASH_PORT}")
    app.run(host=config.DASH_HOST, port=config.DASH_PORT, debug=False)


if __name__ == "__main__":
    main()
