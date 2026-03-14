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
    "LTC_USD": "PF_LTCUSD",
    "XRP_USD": "PF_XRPUSD",
    "BCH_USD": "PF_BCHUSD",
}

# OANDA granularity → Kraken Futures chart resolution
_GRAN: dict[str, str] = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D": "1d", "W": "1w",
}

# Minimum order size in USD per instrument
_MIN_USD: dict[str, float] = {
    "PF_XBTUSD": 1.0,
    "PF_ETHUSD": 1.0,
    "PF_LTCUSD": 1.0,
    "PF_XRPUSD": 1.0,
    "PF_BCHUSD": 1.0,
}


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

    def submit_market_order(self, instrument: str, signed_units: float) -> dict:
        """
        Place a market order.
        signed_units are in coin terms (e.g. BTC). Internally converted to
        USD contract size (each contract = $1).
        Returns {"filled": True} or {"filled": False, "error": ...}.
        """
        symbol = _INST.get(instrument, instrument)
        side   = "buy" if signed_units > 0 else "sell"

        # Convert coin units → USD contract size using current mid price
        try:
            _, _, mid = self.get_prices(instrument)
        except Exception as exc:
            return {"filled": False, "error": f"price fetch failed: {exc}"}

        size_usd = max(1, round(abs(signed_units) * mid))
        min_usd  = _MIN_USD.get(symbol, 1.0)
        if size_usd < min_usd:
            return {"filled": False, "error": f"size ${size_usd} below minimum ${min_usd}"}

        data = {
            "orderType": "mkt",
            "symbol":    symbol,
            "side":      side,
            "size":      str(int(size_usd)),
        }
        result = self._private("POST", "/derivatives/api/v3/sendorder", data)
        if result.get("result") != "success":
            err = result.get("error", result)
            log.error("[kraken_futures] order failed %s %s: %s", side, symbol, err)
            return {"filled": False, "error": str(err)}

        send_status = result.get("sendStatus", {})
        order_id    = send_status.get("order_id", "")
        status      = send_status.get("status", "")
        log.info("[kraken_futures] order %s %s size=$%d status=%s id=%s",
                 side, symbol, size_usd, status, order_id)
        return {"filled": True, "order_id": order_id, "status": status}

    def close_trade(self, instrument: str, units: float) -> bool:
        """Close an open position by submitting the opposite direction market order."""
        symbol    = _INST.get(instrument, instrument)
        positions = self._get_open_positions()

        holding = None
        for pos in positions:
            if pos.get("symbol") == symbol:
                holding = pos
                break

        if holding is None:
            log.warning("[kraken_futures] close_trade: no open position for %s", symbol)
            return False

        side     = "sell" if holding["side"] == "long" else "buy"
        size_usd = int(float(holding.get("size", 0)))
        if size_usd < 1:
            return False

        data = {
            "orderType": "mkt",
            "symbol":    symbol,
            "side":      side,
            "size":      str(size_usd),
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

        # Prefer flex account (margin), fall back to cash
        flex     = accounts.get("flex", {})
        cash     = accounts.get("cash", {})
        cash_bal = 0.0
        try:
            # cash.balances may be keyed by currency code in various cases
            balances = cash.get("balances", {})
            cash_bal = float(next(iter(balances.values()), 0) or 0)
        except Exception:
            pass

        portfolio_value = float(flex.get("portfolioValue", cash_bal) or cash_bal)
        pnl             = float(flex.get("pnl", 0) or 0)
        margin_reqs     = flex.get("marginRequirements", {})
        margin_used     = float(margin_reqs.get("initialMargin", 0) or 0)
        margin_avail    = float(flex.get("availableMargin", portfolio_value) or portfolio_value)

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

        # Kraken Futures signature (official spec):
        #   SHA256(postData + nonce + endpointPath) → HMAC-SHA512(decoded_secret, sha256)
        secret_clean = self._secret.replace(" ", "+")
        try:
            secret_padded = secret_clean + "=" * (-len(secret_clean) % 4)
            secret_bytes  = base64.b64decode(secret_padded)
        except Exception as e:
            log.error("[kraken_futures] base64 decode failed: %s", e)
            secret_bytes = secret_clean.encode("utf-8")
        sha256_hash = hashlib.sha256((post_data + nonce + endpoint).encode("utf-8")).digest()
        signature   = base64.b64encode(
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
