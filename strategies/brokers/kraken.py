# strategies/brokers/kraken.py
"""
KrakenBroker — drop-in broker adapter for the Kraken REST API.

Implements the same interface used by tpqoa in the runner:
  - get_history(instrument, start, end, granularity, price) → DataFrame
  - get_prices(instrument) → (bid, ask, mid)
  - submit_market_order(instrument, signed_units) → dict
  - close_trade(instrument, units) → bool
  - get_account_summary() → dict

Instrument names are translated OANDA-style → Kraken on the way in.
The runner stores the API key as account_id and secret as access_token,
with account_type="kraken".
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

log = logging.getLogger("kraken")

# ─── Instrument / granularity maps ────────────────────────────────────────────

_INST: dict[str, str] = {
    "BTC_USD": "XBTUSD",
    "ETH_USD": "ETHUSD",
    "LTC_USD": "LTCUSD",
    "XRP_USD": "XRPUSD",
    "BCH_USD": "BCHUSD",
}

_GRAN: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D": 1440, "W": 10080,
}

# Minimum order volume per instrument on Kraken
_MIN_VOL: dict[str, float] = {
    "XBTUSD": 0.0001,
    "ETHUSD": 0.01,
    "LTCUSD": 0.1,
    "XRPUSD": 10.0,
    "BCHUSD": 0.01,
}

_BASE_URL = "https://api.kraken.com"


class KrakenBroker:
    """Kraken REST API adapter with tpqoa-compatible interface."""

    account_id = "kraken"
    hostname   = "api.kraken.com"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._key    = api_key
        self._secret = api_secret

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
        pair     = _INST.get(instrument, instrument)
        interval = _GRAN.get(granularity, 60)
        since    = int(datetime.strptime(start, "%Y-%m-%dT%H:%M:%S")
                       .replace(tzinfo=timezone.utc).timestamp())

        resp = requests.get(
            f"{_BASE_URL}/0/public/OHLC",
            params={"pair": pair, "interval": interval, "since": since},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"Kraken OHLC error: {body['error']}")

        # Kraken returns the pair key sometimes with an X prefix — use first key
        result = body["result"]
        data   = result.get(pair) or result.get(next(
            (k for k in result if k != "last"), pair
        ))
        if not data:
            raise RuntimeError(f"No OHLC data returned for {pair}")

        df = pd.DataFrame(
            data,
            columns=["time", "o", "h", "l", "c", "vwap", "volume", "count"],
        )
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").sort_index()

        end_ts = pd.Timestamp(end, tz="UTC") if end else None
        if end_ts is not None:
            df = df[df.index <= end_ts]

        for col in ("o", "h", "l", "c", "volume"):
            df[col] = df[col].astype(float)
        return df

    def get_prices(self, instrument: str) -> tuple[float, float, float]:
        """Return (bid, ask, mid) for an instrument."""
        pair = _INST.get(instrument, instrument)
        resp = requests.get(
            f"{_BASE_URL}/0/public/Ticker",
            params={"pair": pair},
            timeout=5,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"Kraken Ticker error: {body['error']}")

        result = body["result"]
        key    = next(iter(result))
        bid    = float(result[key]["b"][0])
        ask    = float(result[key]["a"][0])
        return bid, ask, (bid + ask) / 2

    # ── Private endpoints ─────────────────────────────────────────────────────

    def submit_market_order(self, instrument: str, signed_units: float) -> dict:
        """
        Place a market order.
        signed_units > 0 = buy, < 0 = sell.
        Returns {"filled": True, "txid": ...} or {"filled": False, "error": ...}.
        """
        pair   = _INST.get(instrument, instrument)
        side   = "buy" if signed_units > 0 else "sell"
        volume = abs(signed_units)
        min_v  = _MIN_VOL.get(pair, 0.0001)

        if volume < min_v:
            return {"filled": False, "error": f"volume {volume} below minimum {min_v}"}

        data = {
            "pair":      pair,
            "type":      side,
            "ordertype": "market",
            "volume":    f"{volume:.8f}",
            "leverage":  "2",   # 2:1 margin — enables short selling
        }
        result = self._private("AddOrder", data)
        if result.get("error"):
            return {"filled": False, "error": result["error"]}

        txid = result.get("result", {}).get("txid", [])
        log.info("[kraken] order filled %s %s vol=%.6f txid=%s",
                 side, pair, volume, txid)
        return {"filled": True, "txid": txid}

    def close_trade(self, instrument: str, units: float) -> bool:
        """Close an open position (submit opposite-direction market order)."""
        # close_trade in tpqoa context means "close existing position"
        # On Kraken, we close by submitting a reduce-only opposite order.
        # First check which direction we hold.
        positions = self._get_open_positions()
        pair      = _INST.get(instrument, instrument)

        holding = None
        for pos_id, pos in positions.items():
            if pos.get("pair") == pair:
                holding = pos
                break

        if holding is None:
            log.warning("[kraken] close_trade: no open position found for %s", pair)
            return False

        side   = "sell" if holding["type"] == "buy" else "buy"
        volume = float(holding["vol"])

        data = {
            "pair":      pair,
            "type":      side,
            "ordertype": "market",
            "volume":    f"{volume:.8f}",
            "leverage":  "2",
            "reduce_only": True,
        }
        result = self._private("AddOrder", data)
        if result.get("error"):
            log.error("[kraken] close_trade error: %s", result["error"])
            return False
        return True

    def get_account_summary(self) -> dict:
        """Return balance/equity info."""
        balance = self._private("Balance")
        trade_b = self._private("TradeBalance", {"asset": "ZUSD"})

        if balance.get("error") or trade_b.get("error"):
            return {}

        b = trade_b.get("result", {})
        return {
            "balance":         float(b.get("tb", 0)),   # trade balance in USD
            "nav":             float(b.get("e",  0)),   # equity
            "unrealized_pl":   float(b.get("n",  0)),   # unrealised P&L
            "margin_used":     float(b.get("m",  0)),
            "margin_free":     float(b.get("mf", 0)),
            "currency":        "USD",
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _private(self, endpoint: str, data: dict | None = None) -> dict:
        data      = data or {}
        data["nonce"] = str(int(time.time() * 1000))
        urlpath   = f"/0/private/{endpoint}"
        postdata  = urllib.parse.urlencode(data)
        encoded   = (data["nonce"] + postdata).encode()
        message   = urlpath.encode() + hashlib.sha256(encoded).digest()
        signature = base64.b64encode(
            hmac.new(base64.b64decode(self._secret), message, hashlib.sha512).digest()
        ).decode()

        resp = requests.post(
            f"{_BASE_URL}{urlpath}",
            data=data,
            headers={"API-Key": self._key, "API-Sign": signature},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _get_open_positions(self) -> dict:
        result = self._private("OpenPositions")
        if result.get("error"):
            log.error("[kraken] OpenPositions error: %s", result["error"])
            return {}
        return result.get("result", {})
