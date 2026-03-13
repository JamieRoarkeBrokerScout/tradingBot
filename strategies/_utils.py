# strategies/_utils.py
"""Shared data-fetching and indicator helpers."""
from __future__ import annotations

import logging
import time
from datetime import datetime

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger("utils")


# ─── OANDA history ────────────────────────────────────────────────────────────

def oanda_history(api, instrument: str, start: datetime, end: datetime,
                  granularity: str) -> pd.DataFrame:
    """Fetch OANDA OHLCV history with exponential backoff on HTTP 429."""
    delay = config.OANDA_BACKOFF_BASE
    last_exc: Exception | None = None

    for attempt in range(config.OANDA_MAX_RETRIES):
        try:
            df = api.get_history(
                instrument=instrument,
                start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                granularity=granularity,
                price="M",
            )
            return df
        except Exception as exc:
            last_exc = exc
            if "429" in str(exc) or "TooManyRequests" in str(exc.__class__.__name__):
                log.warning("Rate limit on %s; retry in %.1fs (attempt %d/%d)",
                            instrument, delay, attempt + 1, config.OANDA_MAX_RETRIES)
                time.sleep(delay)
                delay = min(delay * 2, config.OANDA_BACKOFF_MAX)
            else:
                raise

    raise RuntimeError(f"Max retries reached fetching {instrument}") from last_exc


# ─── Indicator helpers ────────────────────────────────────────────────────────

def atr_series(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int) -> pd.Series:
    """Wilder ATR as a rolling mean of True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def atr_scalar(df: pd.DataFrame, period: int = 14) -> float:
    """Return the latest ATR value from an OHLC DataFrame."""
    return float(
        atr_series(
            df["h"].astype(float),
            df["l"].astype(float),
            df["c"].astype(float),
            period,
        ).iloc[-1]
    )


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Exponential-smoothed RSI (Wilder)."""
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, np.inf)
    return 100 - (100 / (1 + rs))
