# strategies/brokers/kraken_futures.py
"""
KrakenFuturesBroker — adapter for the Kraken Futures REST API.

Supports both live (https://futures.kraken.com) and demo
(https://demo-futures.kraken.com) environments.

The demo environment is a full paper-trading sandbox with separate API keys
obtained from https://demo-futures.kraken.com.

Interface matches KrakenBroker (and tpqoa) so the runner needs no changes:
  - get_history(instrument, start, end, granularity) → DataFrame
  - get_prices(instrument) → (bid, ask, mid)
  - submit_market_order(instrument, signed_units) → dict
  - close_trade(instrument, units) → bool
  - get_account_summary() → dict

Instrument names are translated OANDA-style → Kraken Futures perpetuals.
signed_units are in coin terms (e.g. 0.003 BTC); internally converted to
USD-denominated contract size (each contract = $1 notional).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from datetime import datetime, timezone

import pandas as pd
import requests

log = logging.getLogger("kraken_futures")

_LIVE_BASE = "https://futures.kraken.com"
_DEMO_BASE = "https://demo-futures.kraken.com"

# OANDA-style → Kraken Futures perpetual symbol
_INST: dict[str, str] = {
    "BTC_USD": "PF_XBTUSD",
    "ETH_USD": "PF_ETHUSD",
    "SOL_USD": "PF_SOLUSD",
    "LTC_USD": "PF_LTCUSD",
    "XRP_USD": "PF_XRPUSD",
    "BCH_USD": "PF_BCHUSD",
}

# OANDA granularity → Kraken Futures chart resolution
_GRAN: dict[str, str] = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D": "1d", "W": "1w",
}

# Decimal precision for order size (contractValueTradePrecision from /instruments API)
# Size must be rounded to this many decimal places; min = 1 / 10^precision
_PRECISION: dict[str, int] = {
    "PF_XBTUSD": 4,
    "PF_ETHUSD": 3,
    "PF_SOLUSD": 2,
    "PF_LTCUSD": 2,
    "PF_XRPUSD": 0,
    "PF_BCHUSD": 2,
}

# Minimum order size in coin (base currency) = 1 / 10^precision
_MIN_COINS: dict[str, float] = {sym: 10 ** -p for sym, p in _PRECISION.items()}


class KrakenFuturesBroker:
    """Kraken Futures REST API adapter (live or demo)."""

    def __init__(self, api_key: str, api_secret: str, use_demo: bool = True) -> None:
        self._key    = api_key.strip()
        self._secret = api_secret.strip()
        self._base   = _DEMO_BASE if use_demo else _LIVE_BASE
        self.account_id = "kraken_futures_demo" if use_demo else "kraken_futures"
        self.hostname   = _DEMO_BASE.split("//")[1] if use_demo else _LIVE_BASE.split("//")[1]

    # ── Public endpoints ──────────────────────────────────────────────────────

    def get_history(
        self,
        instrument: str,
        start: str,
        end: str,
        granularity: str,
        price: str = "M",
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame indexed by UTC datetime.
        Columns match tpqoa format: o, h, l, c, volume.
        """
        symbol     = _INST.get(instrument, instrument)
        resolution = _GRAN.get(granularity, "1h")

        since = int(
            datetime.strptime(start, "%Y-%m-%dT%H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        to_ts = int(
            datetime.strptime(end, "%Y-%m-%dT%H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )

        resp = requests.get(
            f"{self._base}/api/charts/v1/trade/{symbol}/{resolution}",
            params={"from": since, "to": to_ts},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        # OHLC endpoint returns {"candles": [...], "more_candles": bool} — no "result" field
        candles = body.get("candles", [])
        if not candles:
            raise RuntimeError(f"No OHLC data returned for {symbol}")

        rows = []
        for c in candles:
            rows.append({
                "time":   c["time"],
                "o":      float(c["open"]),
                "h":      float(c["high"]),
                "l":      float(c["low"]),
                "c":      float(c["close"]),
                "volume": float(c.get("volume", 0)),
            })

        df = pd.DataFrame(rows)
        # time is in milliseconds
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df = df.set_index("time").sort_index()
        return df

    def get_prices(self, instrument: str) -> tuple[float, float, float]:
        """Return (bid, ask, mid) for an instrument."""
        symbol = _INST.get(instrument, instrument)
        resp = requests.get(
            f"{self._base}/derivatives/api/v3/tickers",
            timeout=5,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("result") != "success":
            raise RuntimeError(f"Kraken Futures tickers error: {body}")

        for ticker in body.get("tickers", []):
            if ticker.get("symbol") == symbol:
                bid = float(ticker.get("bid", 0) or 0)
                ask = float(ticker.get("ask", 0) or 0)
                if bid == 0 or ask == 0:
                    last = float(ticker.get("last", 0) or 0)
                    bid = ask = last
                return bid, ask, (bid + ask) / 2

        raise RuntimeError(f"No ticker found for {symbol}")

    # ── Private endpoints ─────────────────────────────────────────────────────

    def submit_market_order(
        self,
        instrument: str,
        signed_units: float,
        tp_price: float | None = None,
        stop_price: float | None = None,
    ) -> dict:
        """
        Place a market order, then attach stop-loss and take-profit orders on the exchange.
        signed_units are in coin terms (e.g. BTC). Internally converted to
        USD contract size (each contract = $1).
        Returns {"filled": True} or {"filled": False, "error": ...}.
        """
        symbol = _INST.get(instrument, instrument)
        side   = "buy" if signed_units > 0 else "sell"
        close_side = "sell" if signed_units > 0 else "buy"

        # PF_* perpetuals are coin-denominated — round to exchange-specified decimal precision
        precision  = _PRECISION.get(symbol, 4)
        size_coins = round(abs(signed_units), precision)
        min_coins  = _MIN_COINS.get(symbol, 10 ** -precision)
        if size_coins < min_coins:
            return {"filled": False, "error": f"size {size_coins} below minimum {min_coins} coins"}

        # Log available margin before attempting order to aid diagnosis
        try:
            _, _, mid = self.get_prices(instrument)
            notional  = size_coins * mid
        except Exception:
            mid      = 0.0
            notional = 0.0
        try:
            acct = self._private("GET", "/derivatives/api/v3/accounts")
            flex = acct.get("accounts", {}).get("flex", {})
            avail = flex.get("availableMargin", "?")
            pv    = flex.get("portfolioValue", "?")
            log.info("[kraken_futures] pre-order check: portfolioValue=%s availableMargin=%s coins=%.6f notional≈$%.0f",
                     pv, avail, size_coins, notional)
        except Exception:
            pass

        size_str = f"{size_coins:.{precision}f}"
        data = {
            "orderType": "mkt",
            "symbol":    symbol,
            "side":      side,
            "size":      size_str,
        }
        result = self._private("POST", "/derivatives/api/v3/sendorder", data)
        if result.get("result") != "success":
            err = result.get("error", result)
            log.error("[kraken_futures] order failed %s %s: %s", side, symbol, err)
            return {"filled": False, "error": str(err)}

        send_status = result.get("sendStatus", {})
        order_id    = send_status.get("order_id", "")
        status      = send_status.get("status", "")
        log.info("[kraken_futures] order %s %s coins=%.6f notional≈$%.0f status=%s id=%s",
                 side, symbol, size_coins, notional, status, order_id)

        # Only treat "placed" or "filled" as a successful fill
        _FILLED = {"placed", "filled", "partiallyFilled"}
        if status not in _FILLED:
            log.error("[kraken_futures] order not filled %s %s: status=%s", side, symbol, status)
            return {"filled": False, "error": f"order status: {status}"}

        # Place stop-loss order on the exchange for hard protection
        if stop_price and stop_price > 0:
            sl_data = {
                "orderType": "stp",
                "symbol":    symbol,
                "side":      close_side,
                "size":      size_str,
                "stopPrice": str(round(stop_price, 6)),
            }
            try:
                sl_result = self._private("POST", "/derivatives/api/v3/sendorder", sl_data)
                if sl_result.get("result") == "success":
                    sl_id = sl_result.get("sendStatus", {}).get("order_id", "")
                    log.info("[kraken_futures] SL order placed at %s id=%s", stop_price, sl_id)
                else:
                    log.warning("[kraken_futures] SL order failed: %s", sl_result.get("error"))
            except Exception as exc:
                log.warning("[kraken_futures] SL order exception: %s", exc)

        # Place take-profit limit order on the exchange
        if tp_price and tp_price > 0:
            tp_data = {
                "orderType":  "lmt",
                "symbol":     symbol,
                "side":       close_side,
                "size":       size_str,
                "limitPrice": str(round(tp_price, 6)),
            }
            try:
                tp_result = self._private("POST", "/derivatives/api/v3/sendorder", tp_data)
                if tp_result.get("result") == "success":
                    tp_id = tp_result.get("sendStatus", {}).get("order_id", "")
                    log.info("[kraken_futures] TP order placed at %s id=%s", tp_price, tp_id)
                else:
                    log.warning("[kraken_futures] TP order failed: %s", tp_result.get("error"))
            except Exception as exc:
                log.warning("[kraken_futures] TP order exception: %s", exc)

        return {"filled": True, "order_id": order_id, "status": status}

    def close_trade(self, instrument: str, units: float) -> bool:
        """Cancel any open orders for this symbol, then close the position."""
        symbol    = _INST.get(instrument, instrument)

        # Cancel all open orders for this symbol first (removes SL/TP bracket orders)
        try:
            self._private("POST", "/derivatives/api/v3/cancelallorders", {"symbol": symbol})
        except Exception as exc:
            log.warning("[kraken_futures] cancelallorders error for %s: %s", symbol, exc)

        positions = self._get_open_positions()
        holding = next((p for p in positions if p.get("symbol") == symbol), None)

        if holding is None:
            # Position already closed (e.g. SL/TP triggered on exchange)
            log.info("[kraken_futures] close_trade: position for %s already closed", symbol)
            return True

        side       = "sell" if holding["side"] == "long" else "buy"
        size_coins = float(holding.get("size", 0))
        if size_coins <= 0:
            return True

        precision = _PRECISION.get(symbol, 4)
        data = {
            "orderType": "mkt",
            "symbol":    symbol,
            "side":      side,
            "size":      f"{size_coins:.{precision}f}",
        }
        result = self._private("POST", "/derivatives/api/v3/sendorder", data)
        if result.get("result") != "success":
            log.error("[kraken_futures] close_trade error: %s", result.get("error"))
            return False
        return True

    def get_account_summary(self) -> dict:
        """Return balance/equity info from the flex account."""
        result = self._private("GET", "/derivatives/api/v3/accounts")
        if result.get("result") != "success":
            log.warning("[kraken_futures] accounts response: %s", result)
            return {"balance": 0.0, "nav": 0.0, "currency": "USD"}

        accounts = result.get("accounts", {})
        log.info("[kraken_futures] accounts data: %s", accounts)

        # Prefer flex account (margin), fall back to cash
        flex     = accounts.get("flex", {})
        cash     = accounts.get("cash", {})
        cash_bal = 0.0
        try:
            balances = cash.get("balances", {})
            cash_bal = float(next(iter(balances.values()), 0) or 0)
        except Exception:
            pass

        portfolio_value = float(flex.get("portfolioValue", cash_bal) or cash_bal)

        # pnl may be at top level or nested under auxiliary/marginEquity
        aux = flex.get("auxiliary", {})
        pnl = (
            float(flex.get("pnl") or 0)
            or float(aux.get("pnl") or 0)
            or float(aux.get("unrealizedPnl") or 0)
        )

        # If pnl is still 0, calculate from open positions + current prices
        if pnl == 0.0:
            try:
                positions = self._get_open_positions()
                for pos in positions:
                    symbol = pos.get("symbol", "")
                    # reverse-map Kraken symbol → OANDA-style instrument
                    inst = next((k for k, v in _INST.items() if v == symbol), None)
                    if not inst:
                        continue
                    try:
                        _, _, mid = self.get_prices(inst)
                    except Exception:
                        continue
                    entry      = float(pos.get("price", 0) or 0)
                    size_coins = float(pos.get("size", 0) or 0)  # coin-denominated
                    pos_side   = 1 if pos.get("side") == "long" else -1
                    if entry > 0 and size_coins > 0:
                        # P&L in USD: coins × (mid - entry)
                        pnl += pos_side * size_coins * (mid - entry)
            except Exception as exc:
                log.warning("[kraken_futures] fallback pnl calc failed: %s", exc)

        margin_reqs  = flex.get("marginRequirements", {})
        margin_used  = (
            float(margin_reqs.get("im", 0) or 0)
            or float(margin_reqs.get("initialMargin", 0) or 0)
            or float(flex.get("initialMargin", 0) or 0)
        )
        margin_avail = float(flex.get("availableMargin", portfolio_value) or portfolio_value)

        return {
            "balance":       portfolio_value,
            "nav":           portfolio_value,
            "unrealized_pl": pnl,
            "margin_used":   margin_used,
            "margin_free":   margin_avail,
            "currency":      "USD",
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _private(self, method: str, endpoint: str, data: dict | None = None) -> dict:
        """Sign and execute a private API request."""
        data      = data or {}
        nonce     = str(int(time.time() * 1000))
        post_data = urllib.parse.urlencode(data) if method == "POST" else ""

        # Kraken Futures signature (per official CF REST v3 Python SDK):
        #   message  = postData + nonce + signing_path
        #   sha256   = SHA256(message)
        #   Authent  = Base64(HMAC-SHA512(Base64-Decode(apiSecret), sha256))
        # The signing path strips the /derivatives prefix:
        #   /derivatives/api/v3/accounts → /api/v3/accounts
        signing_path = endpoint.replace("/derivatives", "", 1)

        try:
            secret_bytes = base64.b64decode(self._secret.strip())
        except Exception as e:
            log.error("[kraken_futures] base64 decode failed: %s", e)
            secret_bytes = self._secret.strip().encode("utf-8")

        message      = (post_data + nonce + signing_path).encode("utf-8")
        sha256_hash  = hashlib.sha256(message).digest()
        signature    = base64.b64encode(
            hmac.new(secret_bytes, sha256_hash, hashlib.sha512).digest()
        ).decode()

        headers = {
            "APIKey":  self._key,
            "Nonce":   nonce,
            "Authent": signature,
        }

        url = f"{self._base}{endpoint}"
        if method == "POST":
            resp = requests.post(url, data=data, headers=headers, timeout=10)
        else:
            resp = requests.get(url, headers=headers, timeout=10)

        if resp.status_code != 200:
            log.error("[kraken_futures] %s %s → HTTP %s: %s",
                      method, endpoint, resp.status_code, resp.text[:300])
        resp.raise_for_status()
        return resp.json()

    def _get_open_positions(self) -> list[dict]:
        result = self._private("GET", "/derivatives/api/v3/openpositions")
        if result.get("result") != "success":
            log.error("[kraken_futures] openpositions error: %s", result.get("error"))
            return []
        return result.get("openPositions", [])
