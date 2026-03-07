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

import time
from pathlib import Path

import tpqoa

from strategies import config
from strategies.base       import SafeguardsBase
from strategies.stat_arb   import StatArbStrategy
from strategies.momentum   import MomentumStrategy
from strategies.vol_premium import VolPremiumStrategy

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


# ─── Runner ───────────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, api, state_path: Path) -> None:
        self._api        = api
        self._state_path = state_path
        self._strategies = {
            "stat_arb":    StatArbStrategy(api),
            "momentum":    MomentumStrategy(api),
            "vol_premium": VolPremiumStrategy(api),
        }
        self._enabled: dict[str, bool] = {k: False for k in self._strategies}
        self._running = True

    def run(self) -> None:
        log.info("Strategy runner started (pid=%d)", __import__("os").getpid())

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
                nav = _get_nav(self._api)
                SafeguardsBase.update_nav(nav)
                log.debug("NAV updated: %.2f", nav)
                last_nav_update = now

            # Fetch current mid prices for strategies that need them
            prices = _get_mid_prices(self._api)

            # Tick each enabled strategy and submit any signals
            for name, strategy in self._strategies.items():
                if not self._enabled[name]:
                    continue
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
                            _submit(self._api, sig)

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

def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy runner")
    parser.add_argument("--config", required=True, help="Path to OANDA .cfg file")
    parser.add_argument("--state",  default=config.STATE_FILE_PATH,
                        help="Path to strategy state JSON")
    args = parser.parse_args()

    api        = tpqoa.tpqoa(args.config)
    state_path = Path(args.state)

    runner = Runner(api, state_path)

    def _shutdown(signum, frame):
        log.info("Shutdown signal received")
        runner.stop()

    _signal.signal(_signal.SIGINT,  _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)

    runner.run()


if __name__ == "__main__":
    main()
