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

from strategies import config
from strategies.base            import SafeguardsBase
from strategies.stat_arb        import StatArbStrategy
from strategies.momentum        import MomentumStrategy
from strategies.vol_premium     import VolPremiumStrategy
from strategies.crypto_momentum import CryptoMomentumStrategy
from strategies.daily_target    import DailyTargetStrategy
from strategies.brokers.kraken         import KrakenBroker
from strategies.brokers.kraken_futures import KrakenFuturesBroker
from database.database import (
    DB_PATH as _DB_PATH,
    upsert_open_trade, delete_open_trade, get_strategy_states,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-14s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("runner")

_PRICE_INSTRUMENTS = [
    "SPX500_USD", "XAU_USD", "XAG_USD", "BCO_USD", "NAS100_USD",
    "EUR_USD", "GBP_USD", "BTC_USD", "ETH_USD",
]

# Kraken Futures symbol mapping (mirrors kraken_futures._INST)
_KF_INST = {
    "BTC_USD": "PF_XBTUSD",
    "ETH_USD": "PF_ETHUSD",
    "SOL_USD": "PF_SOLUSD",
    "LTC_USD": "PF_LTCUSD",
    "XRP_USD": "PF_XRPUSD",
    "BCH_USD": "PF_BCHUSD",
}


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


def _submit(api, sig) -> bool:
    """
    Submit a Signal to OANDA with exponential backoff on HTTP 429.
    Returns True if the order was accepted, False otherwise.
    This is the ONLY place broker calls happen — strategies never call
    the API directly.
    """
    action = sig.meta.get("action", "open")
    delay  = config.OANDA_BACKOFF_BASE

    # Use float units for fractional instruments (crypto); int for whole-unit instruments.
    raw_units = sig.units * sig.direction
    if abs(raw_units) >= 1:
        signed_units: float = int(raw_units)
    else:
        signed_units = round(raw_units, 8)   # crypto — preserve fractional units

    if action == "close":
        close_units = abs(signed_units) if abs(signed_units) >= 1 else abs(raw_units)
    else:
        if signed_units == 0:
            log.warning("[runner] skipping %s order — computed 0 units (raw=%.8f); "
                        "check position sizing", sig.instrument, sig.units)
            return False

    # ── Kraken broker path (spot or futures) ─────────────────────────────────
    if isinstance(api, (KrakenBroker, KrakenFuturesBroker)):
        try:
            if action == "close":
                ok = api.close_trade(sig.instrument, close_units)
                return ok
            else:
                result = api.submit_market_order(
                    sig.instrument, signed_units,
                    tp_price=sig.tp_price if sig.tp_price else None,
                    stop_price=sig.stop_price if sig.stop_price else None,
                )
                if result.get("filled"):
                    log.info("[runner] Kraken order filled for %s", sig.instrument)
                    return True
                log.error("[runner] Kraken order failed for %s: %s",
                          sig.instrument, result.get("error"))
                return False
        except Exception as exc:
            log.error("[runner] Kraken order exception for %s: %s", sig.instrument, exc)
            return False

    # ── OANDA / tpqoa path ────────────────────────────────────────────────────
    oanda_units = int(signed_units)   # OANDA requires integer units

    if action == "close":
        # Use the position close endpoint — closes all units regardless of side.
        # close signal direction is OPPOSITE of the original position:
        #   sig.direction == -1 → we're selling → original was LONG  → longUnits="ALL"
        #   sig.direction == +1 → we're buying  → original was SHORT → shortUnits="ALL"
        try:
            if sig.direction < 0:
                resp = api.ctx.position.close(api.account_id, sig.instrument, longUnits="ALL")
            else:
                resp = api.ctx.position.close(api.account_id, sig.instrument, shortUnits="ALL")
            log.info("[runner] position.close status=%s body=%s", resp.status, resp.body)
            if resp.status in (200, 201):
                return True
            # If NO_UNITS_TO_CLOSEOUT in the first direction, try the other
            body_str = str(resp.body)
            if "NO_UNITS_TO_CLOSEOUT" in body_str or "CLOSEOUT_POSITION_DOESNT_EXIST" in body_str:
                if sig.direction < 0:
                    resp2 = api.ctx.position.close(api.account_id, sig.instrument, shortUnits="ALL")
                else:
                    resp2 = api.ctx.position.close(api.account_id, sig.instrument, longUnits="ALL")
                log.info("[runner] position.close fallback status=%s body=%s", resp2.status, resp2.body)
                if resp2.status in (200, 201):
                    return True
                # Position genuinely gone (already closed by SL/TP on OANDA)
                if "NO_UNITS_TO_CLOSEOUT" in str(resp2.body) or "CLOSEOUT_POSITION_DOESNT_EXIST" in str(resp2.body):
                    log.info("[runner] position for %s already closed on OANDA", sig.instrument)
                    return True
            log.error("[runner] OANDA position close failed for %s: status=%s body=%s",
                      sig.instrument, resp.status, resp.body)
            return False
        except Exception as exc:
            log.error("[runner] OANDA position.close raised for %s: %s", sig.instrument, exc)
            return False

    for attempt in range(config.OANDA_MAX_RETRIES):
        try:
            # Bypass tpqoa's create_order (silently returns None on error).
            # Call the underlying v20 context directly to get the full response.
            # positionFill=OPEN_ONLY prevents OANDA cancelling orders for FX/CFD
            # pairs where DEFAULT fill would attempt to close an opposing position.
            request = api.ctx.order.market(
                api.account_id,
                instrument=sig.instrument,
                units=oanda_units,
                positionFill="OPEN_ONLY",
            )
            body = request.body
            status = request.status
            log.info("[runner] OANDA status=%s body=%s", status, body)

            if status == 429 or (isinstance(body, dict) and "TooManyRequests" in str(body)):
                log.warning("[runner] rate limit on %s; retry in %.1fs (attempt %d)",
                            sig.instrument, delay, attempt + 1)
                time.sleep(delay)
                delay = min(delay * 2, config.OANDA_BACKOFF_MAX)
                continue

            if isinstance(body, dict):
                if "orderRejectTransaction" in body:
                    txn = body["orderRejectTransaction"]
                    reason = getattr(txn, "rejectReason", txn)
                    log.error("[runner] order REJECTED for %s: %s", sig.instrument, reason)
                    return False
                if "orderCancelTransaction" in body:
                    txn = body["orderCancelTransaction"]
                    reason = getattr(txn, "reason", txn)
                    log.error("[runner] order CANCELLED for %s: reason=%s", sig.instrument, reason)
                    return False
                if "orderFillTransaction" in body:
                    log.info("[runner] order filled successfully for %s", sig.instrument)
                    return True
                if "orderCreateTransaction" in body:
                    # GTC/GTD order accepted but not yet filled — treat as success
                    log.info("[runner] order created (pending fill) for %s", sig.instrument)
                    return True
                # Unexpected body — log everything for diagnosis
                log.error("[runner] unexpected OANDA response for %s (status=%s): %s",
                          sig.instrument, status, body)
                return False

            log.error("[runner] non-dict response body for %s: %s", sig.instrument, body)
            return False

        except Exception as exc:
            exc_str = str(exc)
            if "429" in exc_str or "TooManyRequests" in exc_str:
                log.warning("[runner] rate limit on %s; retry in %.1fs (attempt %d)",
                            sig.instrument, delay, attempt + 1)
                time.sleep(delay)
                delay = min(delay * 2, config.OANDA_BACKOFF_MAX)
            else:
                log.error("[runner] order error: %s", exc_str)
                return False
    return False


# ─── State ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return get_strategy_states()
    except Exception:
        return {"stat_arb": {"enabled": False}, "momentum": {"enabled": False}, "vol_premium": {"enabled": False}}


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
    strategy_name: str = "",
    entry_metadata: str | None = None,
) -> None:
    """Write a completed trade to the dashboard's SQLite database."""
    if exit_price <= 0:
        return
    if entry_price <= 0:
        entry_price = exit_price  # opened during price-fetch failure; P&L unknown
    pl_points = (exit_price - entry_price) * direction
    raw_pl    = pl_points * units
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        conn.execute(
            """INSERT INTO trades
                   (entry_time, exit_time, instrument, direction, entry_units,
                    entry_price, exit_price, exit_reason,
                    pl_points, pl_R, raw_pl,
                    bar_length, momentum, threshold_k, per_trade_sl, per_trade_tp, trailing_mode,
                    strategy_name, entry_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry_time, exit_time, instrument, direction, int(units),
             entry_price, exit_price, exit_reason,
             pl_points, 0.0, raw_pl,
             None, None, None, None, None, None,
             strategy_name or None, entry_metadata),
        )
        conn.commit()
        conn.close()
        log.info("Trade recorded: %s %s pl=%.4f", exit_reason, instrument, raw_pl)
    except Exception:
        log.exception("Failed to record trade to DB")


# ─── Runner ───────────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, apis: dict) -> None:
        """
        :param apis: {bot_key: tpqoa_instance} — one API connection per strategy account.
        """
        self._apis = apis
        # Instantiate each strategy only if credentials were provided for it
        self._strategies = {}
        if "stat_arb" in apis:
            self._strategies["stat_arb"]    = StatArbStrategy(apis["stat_arb"])
        if "momentum" in apis:
            self._strategies["momentum"]    = MomentumStrategy(apis["momentum"])
        if "vol_premium" in apis:
            self._strategies["vol_premium"] = VolPremiumStrategy(apis["vol_premium"])
        if "crypto" in apis:
            self._strategies["crypto"]       = CryptoMomentumStrategy(apis["crypto"])
        if "daily_target" in apis:
            self._strategies["daily_target"] = DailyTargetStrategy(apis["daily_target"])

        # Prefer a tpqoa (OANDA) API for price polling — Kraken can't price OANDA instruments
        self._default_api = (
            next((a for a in apis.values() if isinstance(a, tpqoa.tpqoa)), None)
            or next(iter(apis.values()), None)
        )
        self._enabled: dict[str, bool] = {k: False for k in self._strategies}
        # Track open positions so we can record PnL when they close
        # key: f"{strategy_name}:{instrument}"
        self._open_trades: dict[str, dict] = {}
        self._running = True

    def run(self) -> None:
        log.info("Strategy runner started (pid=%d)", os.getpid())

        last_state_reload   = 0.0
        last_nav_update     = 0.0
        last_kraken_sync    = 0.0
        STATE_INTERVAL      = 30      # seconds
        NAV_INTERVAL        = 300     # seconds
        KRAKEN_SYNC_INTERVAL = 60     # seconds — detect exchange SL/TP fires

        while self._running:
            now = time.monotonic()

            # Reload enabled flags from DB
            if now - last_state_reload >= STATE_INTERVAL:
                state = _load_state()
                for name in self._strategies:
                    self._enabled[name] = state.get(name, {}).get("enabled", False)
                last_state_reload = now

            # Refresh account NAV from default (OANDA) API for position sizing
            if now - last_nav_update >= NAV_INTERVAL:
                if self._default_api:
                    nav = _get_nav(self._default_api)
                    SafeguardsBase.update_nav(nav)
                    log.debug("NAV updated: %.2f", nav)
                last_nav_update = now

            prices = _get_mid_prices(self._default_api) if self._default_api else {}

            # Supplement with live Kraken prices for crypto instruments (SOL not on OANDA)
            crypto_api = self._apis.get("crypto")
            if isinstance(crypto_api, KrakenFuturesBroker):
                for inst in config.CRYPTO_INSTRUMENTS:
                    try:
                        bid, ask, _ = crypto_api.get_prices(inst)
                        prices[inst] = (bid + ask) / 2
                    except Exception:
                        pass

            # Kraken position sync — detect positions closed by exchange SL/TP
            if isinstance(crypto_api, KrakenFuturesBroker) and now - last_kraken_sync >= KRAKEN_SYNC_INTERVAL:
                last_kraken_sync = now
                try:
                    krak_open = {p["symbol"] for p in crypto_api._get_open_positions()}
                    for inst in list(config.CRYPTO_INSTRUMENTS):
                        trade_key = f"crypto:{inst}"
                        if trade_key not in self._open_trades:
                            continue
                        krak_sym = _KF_INST.get(inst)
                        if krak_sym and krak_sym not in krak_open:
                            # Position closed on exchange (SL/TP fired) — record it
                            log.info("[runner] Kraken SL/TP detected for %s — recording close", inst)
                            entry = self._open_trades.pop(trade_key, None)
                            try:
                                delete_open_trade(trade_key)
                            except Exception:
                                pass
                            if entry:
                                exit_price = prices.get(inst, 0.0)
                                if exit_price == 0.0:
                                    try:
                                        _, _, exit_price = crypto_api.get_prices(inst)
                                    except Exception:
                                        pass
                                raw_pl = ((exit_price - entry["entry_price"])
                                          * entry["direction"] * entry["units"])
                                _record_trade(
                                    instrument=entry["instrument"],
                                    direction=entry["direction"],
                                    units=entry["units"],
                                    entry_price=entry["entry_price"],
                                    exit_price=exit_price,
                                    entry_time=entry["entry_time"],
                                    exit_time=datetime.now(timezone.utc).isoformat(),
                                    exit_reason="exchange_sl_tp",
                                    strategy_name=entry.get("strategy_name", ""),
                                    entry_metadata=entry.get("entry_metadata"),
                                )
                                strat_obj = self._strategies.get("crypto")
                                if strat_obj:
                                    strat_obj.record_fill(raw_pl)
                                    strat_obj.on_position_close()
                                # Remove from strategy's in-memory trades dict
                                crypto_strat = self._strategies.get("crypto")
                                if crypto_strat and hasattr(crypto_strat, "_trades"):
                                    crypto_strat._trades.pop(inst, None)
                except Exception:
                    log.exception("[runner] Kraken position sync failed")

            # Tick each enabled strategy, submit signals via its own API connection
            for name, strategy in self._strategies.items():
                if not self._enabled[name]:
                    continue
                api = self._apis.get(name, self._default_api)
                try:
                    if name == "vol_premium":
                        signals = strategy.tick(current_price=prices.get(config.VOL_INSTRUMENT))
                    elif name in ("momentum", "crypto", "daily_target"):
                        signals = strategy.tick(current_prices=prices)
                    else:
                        signals = strategy.tick()

                    for sig in signals:
                        if self.approve_signal(strategy, sig):
                            log.info("[runner] → %s %s %+d %.2f units",
                                     sig.strategy, sig.instrument, sig.direction, sig.units)
                            submitted = _submit(api, sig)
                            if not submitted:
                                continue

                            trade_key = f"{name}:{sig.instrument}"
                            action = sig.meta.get("action", "open")
                            now_str = datetime.now(timezone.utc).isoformat()

                            if action == "open":
                                if isinstance(api, KrakenFuturesBroker):
                                    # Always fetch from Kraken — OANDA prices are a different exchange
                                    try:
                                        _, _, entry_price = api.get_prices(sig.instrument)
                                    except Exception:
                                        entry_price = 0.0
                                else:
                                    entry_price = prices.get(sig.instrument, 0.0)
                                    if entry_price == 0.0:
                                        try:
                                            _, _, entry_price = api.get_prices(sig.instrument)
                                        except Exception:
                                            pass
                                self._open_trades[trade_key] = {
                                    "instrument":     sig.instrument,
                                    "direction":      sig.direction,
                                    "units":          sig.units,
                                    "entry_price":    entry_price,
                                    "entry_time":     now_str,
                                    "strategy_name":  name,
                                    "entry_metadata": json.dumps(sig.meta),
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
                                        stop_price=sig.stop_price if sig.stop_price else None,
                                        tp_price=sig.tp_price if sig.tp_price else None,
                                    )
                                except Exception:
                                    log.exception("[runner] failed to persist open trade %s", trade_key)
                                # Update safeguard counters
                                nav_for_lev = getattr(strategy, "_kraken_nav_cache", 0) or 0
                                if nav_for_lev <= 0:
                                    from strategies.base import _nav as _global_nav
                                    nav_for_lev = _global_nav or 1.0
                                lev_contrib = (sig.units * entry_price / nav_for_lev) if nav_for_lev > 0 else 0.0
                                strategy.on_position_open(lev_contrib)

                            elif action == "close":
                                entry = self._open_trades.pop(trade_key, None)
                                try:
                                    delete_open_trade(trade_key)
                                except Exception:
                                    log.exception("[runner] failed to delete open trade %s", trade_key)
                                if entry:
                                    exit_price = prices.get(sig.instrument, 0.0)
                                    # Kraken prices won't be in the OANDA prices dict — fetch directly
                                    if exit_price == 0.0 and isinstance(api, KrakenFuturesBroker):
                                        try:
                                            _, _, exit_price = api.get_prices(sig.instrument)
                                        except Exception:
                                            pass
                                    _record_trade(
                                        instrument=entry["instrument"],
                                        direction=entry["direction"],
                                        units=entry["units"],
                                        entry_price=entry["entry_price"],
                                        exit_price=exit_price,
                                        entry_time=entry["entry_time"],
                                        exit_time=now_str,
                                        exit_reason=sig.meta.get("reason", "close"),
                                        strategy_name=entry.get("strategy_name", ""),
                                        entry_metadata=entry.get("entry_metadata"),
                                    )
                                    # Update safeguard counters — record_fill updates _daily_pnl + consec_loss
                                    if entry["entry_price"] > 0 and exit_price > 0:
                                        raw_pl = ((exit_price - entry["entry_price"])
                                                  * entry["direction"] * entry["units"])
                                        strategy.record_fill(raw_pl)
                                    else:
                                        strategy.record_fill(0.0)

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
    args = parser.parse_args()

    creds_map = json.loads(Path(args.creds).read_text())

    # Build one tpqoa API instance per strategy that has credentials
    apis: dict = {}
    for bot_key, creds in creds_map.items():
        try:
            account_type = creds.get("account_type", "practice")

            if account_type == "kraken":
                # Kraken Spot: account_id = API key, access_token = API secret
                broker = KrakenBroker(
                    api_key=creds["account_id"],
                    api_secret=creds["access_token"],
                )
                apis[bot_key] = broker
                log.info("API initialised for %s (broker=kraken)", bot_key)
                try:
                    summary = broker.get_account_summary()
                    bal = summary.get("balance", "?")
                    log.info("Kraken account verified for %s: balance=%s USD", bot_key, bal)
                except Exception as exc:
                    log.error("Kraken account check error for %s: %s", bot_key, exc)
            elif account_type in ("kraken_futures", "kraken_futures_demo"):
                # Kraken Futures: account_id = API key, access_token = API secret
                use_demo = account_type == "kraken_futures_demo"
                broker = KrakenFuturesBroker(
                    api_key=creds["account_id"],
                    api_secret=creds["access_token"],
                    use_demo=use_demo,
                )
                apis[bot_key] = broker
                env_label = "demo" if use_demo else "live"
                log.info("API initialised for %s (broker=kraken_futures %s)", bot_key, env_label)
                try:
                    summary = broker.get_account_summary()
                    bal = summary.get("balance", "?")
                    log.info("Kraken Futures account verified for %s: balance=%s USD", bot_key, bal)
                except Exception as exc:
                    log.error("Kraken Futures account check error for %s: %s", bot_key, exc)
            else:
                cfg = _make_cfg_file(creds["account_id"], creds["access_token"], account_type)
                apis[bot_key] = tpqoa.tpqoa(cfg)
                log.info("API initialised for %s (account: %s type=%s hostname=%s)",
                         bot_key, creds["account_id"], account_type,
                         apis[bot_key].hostname)
                try:
                    resp = apis[bot_key].ctx.account.summary(creds["account_id"])
                    if resp.status == 200:
                        acct = resp.body["account"]
                        bal  = getattr(acct, "balance", "?")
                        cur  = getattr(acct, "currency", "?")
                        log.info("Account verified for %s: balance=%s currency=%s",
                                 bot_key, bal, cur)
                    else:
                        log.error("Account check FAILED for %s: status=%s body=%s",
                                  bot_key, resp.status, resp.body)
                except Exception as exc:
                    log.error("Account check error for %s: %s", bot_key, exc)
        except Exception:
            log.exception("Failed to initialise API for %s — strategy will be skipped", bot_key)

    if not apis:
        log.error("No valid OANDA credentials found. Exiting.")
        sys.exit(1)

    runner = Runner(apis)

    def _shutdown(signum, frame):
        log.info("Shutdown signal received")
        runner.stop()

    _signal.signal(_signal.SIGINT,  _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)

    runner.run()


if __name__ == "__main__":
    main()
