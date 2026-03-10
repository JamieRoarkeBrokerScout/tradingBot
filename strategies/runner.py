#!/usr/bin/env python3
# strategies/runner.py
"""
Strategy runner — single subprocess that orchestrates all three strategies.

Run as:  python strategies/runner.py --config <oanda.cfg> --state <state.json>
or via:  python -m strategies.runner ...
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal as _signal
import sys

# Ensure the project root is on sys.path so package imports work whether
# this is invoked as a plain script or with -m.
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import tpqoa

_DB_PATH = Path(_root) / "database" / "trades.db"

from strategies import config
from strategies.base       import SafeguardsBase
from strategies.stat_arb   import StatArbStrategy
from strategies.momentum   import MomentumStrategy
from strategies.vol_premium import VolPremiumStrategy
from database.database import upsert_open_trade, delete_open_trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-14s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("runner")

_PRICE_INSTRUMENTS = [
    "SPX500_USD", "XAU_USD", "XAG_USD", "BCO_USD", "NAS100_USD",
]


# ─── OANDA helpers ────────────────────────────────────────────────────────────

def _get_mid_prices(api) -> dict[str, float]:
    prices: dict[str, float] = {}
    for inst in _PRICE_INSTRUMENTS:
        try:
            bid, ask, _ = api.get_prices(inst)
            prices[inst] = (float(bid) + float(ask)) / 2
        except Exception:
            pass
    return prices


def _get_nav(api) -> float:
    try:
        summary = api.get_account_summary()
        return float(summary.get("NAV", summary.get("balance", 100_000)))
    except Exception:
        return 100_000.0


def _submit(api, sig) -> None:
    """
    Submit a Signal to OANDA with exponential backoff on HTTP 429.
    This is the ONLY place broker calls happen — strategies never call
    the API directly.
    """
    action = sig.meta.get("action", "open")
    delay  = config.OANDA_BACKOFF_BASE

    for attempt in range(config.OANDA_MAX_RETRIES):
        try:
            if action == "close":
                api.close_trade(sig.instrument, int(sig.units))
            else:
                signed_units = int(sig.units * sig.direction)
                api.create_order(sig.instrument, units=signed_units)
            return
        except Exception as exc:
            exc_str = str(exc)
            if "429" in exc_str or "TooManyRequests" in exc_str:
                log.warning("[runner] rate limit on %s; retry in %.1fs (attempt %d)",
                            sig.instrument, delay, attempt + 1)
                time.sleep(delay)
                delay = min(delay * 2, config.OANDA_BACKOFF_MAX)
            else:
                log.error("[runner] order error: %s", exc_str)
                return


# ─── State file ───────────────────────────────────────────────────────────────

_DEFAULT_STATE = {
    "stat_arb":    {"enabled": False},
    "momentum":    {"enabled": False},
    "vol_premium": {"enabled": False},
}


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return dict(_DEFAULT_STATE)


# ─── Trade recording ──────────────────────────────────────────────────────────

def _record_trade(
    instrument: str,
    direction: int,
    units: float,
    entry_price: float,
    exit_price: float,
    entry_time: str,
    exit_time: str,
    exit_reason: str,
) -> None:
    """Write a completed trade to the dashboard's SQLite database."""
    if entry_price <= 0 or exit_price <= 0:
        return
    pl_points = (exit_price - entry_price) * direction
    raw_pl    = pl_points * units
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        conn.execute(
            """INSERT INTO trades
                   (entry_time, exit_time, instrument, direction, entry_units,
                    entry_price, exit_price, exit_reason,
                    pl_points, pl_R, raw_pl,
                    bar_length, momentum, threshold_k, per_trade_sl, per_trade_tp, trailing_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry_time, exit_time, instrument, direction, int(units),
             entry_price, exit_price, exit_reason,
             pl_points, 0.0, raw_pl,
             None, None, None, None, None, None),
        )
        conn.commit()
        conn.close()
        log.info("Trade recorded: %s %s pl=%.4f", exit_reason, instrument, raw_pl)
    except Exception:
        log.exception("Failed to record trade to DB")


# ─── Runner ───────────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, apis: dict, state_path: Path) -> None:
        """
        :param apis: {bot_key: tpqoa_instance} — one API connection per strategy account.
        """
        self._apis       = apis
        self._state_path = state_path
        # Instantiate each strategy only if credentials were provided for it
        self._strategies = {}
        if "stat_arb" in apis:
            self._strategies["stat_arb"]    = StatArbStrategy(apis["stat_arb"])
        if "momentum" in apis:
            self._strategies["momentum"]    = MomentumStrategy(apis["momentum"])
        if "vol_premium" in apis:
            self._strategies["vol_premium"] = VolPremiumStrategy(apis["vol_premium"])

        # Use any available API for shared calls (price polling, NAV)
        self._default_api = next(iter(apis.values()), None)
        self._enabled: dict[str, bool] = {k: False for k in self._strategies}
        # Track open positions so we can record PnL when they close
        # key: f"{strategy_name}:{instrument}"
        self._open_trades: dict[str, dict] = {}
        self._running = True

    def run(self) -> None:
        log.info("Strategy runner started (pid=%d)", os.getpid())

        last_state_reload = 0.0
        last_nav_update   = 0.0
        STATE_INTERVAL    = 30      # seconds
        NAV_INTERVAL      = 300     # seconds

        while self._running:
            now = time.monotonic()

            # Reload enabled flags from state file
            if now - last_state_reload >= STATE_INTERVAL:
                state = _load_state(self._state_path)
                for name in self._strategies:
                    self._enabled[name] = state.get(name, {}).get("enabled", False)
                last_state_reload = now

            # Refresh account NAV
            if now - last_nav_update >= NAV_INTERVAL:
                if self._default_api:
                    nav = _get_nav(self._default_api)
                    SafeguardsBase.update_nav(nav)
                    log.debug("NAV updated: %.2f", nav)
                last_nav_update = now

            prices = _get_mid_prices(self._default_api) if self._default_api else {}

            # Tick each enabled strategy, submit signals via its own API connection
            for name, strategy in self._strategies.items():
                if not self._enabled[name]:
                    continue
                api = self._apis.get(name, self._default_api)
                try:
                    if name == "vol_premium":
                        signals = strategy.tick(current_price=prices.get(config.VOL_INSTRUMENT))
                    elif name == "momentum":
                        signals = strategy.tick(current_prices=prices)
                    else:
                        signals = strategy.tick()

                    for sig in signals:
                        if self.approve_signal(strategy, sig):
                            log.info("[runner] → %s %s %+d %.2f units",
                                     sig.strategy, sig.instrument, sig.direction, sig.units)
                            _submit(api, sig)

                            trade_key = f"{name}:{sig.instrument}"
                            action = sig.meta.get("action", "open")
                            now_str = datetime.now(timezone.utc).isoformat()

                            if action == "open":
                                entry_price = prices.get(sig.instrument, 0.0)
                                self._open_trades[trade_key] = {
                                    "instrument":  sig.instrument,
                                    "direction":   sig.direction,
                                    "units":       sig.units,
                                    "entry_price": entry_price,
                                    "entry_time":  now_str,
                                }
                                try:
                                    upsert_open_trade(
                                        trade_key=trade_key,
                                        strategy=name,
                                        instrument=sig.instrument,
                                        direction=sig.direction,
                                        units=sig.units,
                                        entry_price=entry_price,
                                        entry_time=now_str,
                                    )
                                except Exception:
                                    log.exception("[runner] failed to persist open trade %s", trade_key)
                            elif action == "close":
                                entry = self._open_trades.pop(trade_key, None)
                                try:
                                    delete_open_trade(trade_key)
                                except Exception:
                                    log.exception("[runner] failed to delete open trade %s", trade_key)
                                if entry:
                                    exit_price = prices.get(sig.instrument, 0.0)
                                    _record_trade(
                                        instrument=entry["instrument"],
                                        direction=entry["direction"],
                                        units=entry["units"],
                                        entry_price=entry["entry_price"],
                                        exit_price=exit_price,
                                        entry_time=entry["entry_time"],
                                        exit_time=now_str,
                                        exit_reason=sig.meta.get("reason", "close"),
                                    )

                except Exception:
                    log.exception("[runner] error ticking strategy %s", name)

            time.sleep(5)

        log.info("Strategy runner stopped")

    def approve_signal(self, strategy: SafeguardsBase, sig) -> bool:
        """Gate: only approve if action is 'close' (always pass) or approve_trade passes."""
        if sig.meta.get("action") == "close":
            return True
        return strategy.approve_trade(sig)

    def stop(self) -> None:
        self._running = False


# ─── Entry point ──────────────────────────────────────────────────────────────

def _make_cfg_file(account_id: str, access_token: str, account_type: str) -> str:
    """Write a tpqoa-compatible .cfg file and return its path."""
    import tempfile
    content = (
        f"[oanda]\n"
        f"account_id = {account_id}\n"
        f"access_token = {access_token}\n"
        f"account_type = {account_type}\n"
    )
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", prefix="oanda_", delete=False)
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return tmp.name


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy runner")
    parser.add_argument("--creds", required=True,
                        help="Path to JSON file with per-strategy OANDA credentials")
    parser.add_argument("--state", default=config.STATE_FILE_PATH,
                        help="Path to strategy state JSON")
    args = parser.parse_args()

    creds_map = json.loads(Path(args.creds).read_text())
    state_path = Path(args.state)

    # Build one tpqoa API instance per strategy that has credentials
    apis: dict = {}
    for bot_key, creds in creds_map.items():
        try:
            cfg = _make_cfg_file(creds["account_id"], creds["access_token"], creds["account_type"])
            apis[bot_key] = tpqoa.tpqoa(cfg)
            log.info("API initialised for %s (account: %s)", bot_key, creds["account_id"])
        except Exception:
            log.exception("Failed to initialise API for %s — strategy will be skipped", bot_key)

    if not apis:
        log.error("No valid OANDA credentials found. Exiting.")
        sys.exit(1)

    runner = Runner(apis, state_path)

    def _shutdown(signum, frame):
        log.info("Shutdown signal received")
        runner.stop()

    _signal.signal(_signal.SIGINT,  _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)

    runner.run()


if __name__ == "__main__":
    main()
