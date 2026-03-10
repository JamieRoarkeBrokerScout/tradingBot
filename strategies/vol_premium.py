# strategies/vol_premium.py
"""
VolPremiumStrategy — short volatility when IV > RV by ≥ 15%.

Instrument:  SPX500_USD
Poll:        every 15 minutes
iv_proxy  =  20-day ATR / close
rv_proxy  =  30-day rolling mean of iv_proxy
iv_rv_ratio = iv_proxy / rv_proxy

ENTRY:       1.15 <= ratio < 2.0  →  SHORT SPX500_USD
KILL SWITCH (fires before entry check):
    ratio >= 2.0      →  DO NOT enter; close any open position immediately
    est. VIX > 30     →  disable strategy entirely, close all, set enabled=False
EXPOSURE CAP: vol positions never exceed 20% NAV — block new entries if hit
EXIT:        stop / TP / ratio < 1.0 / age > 5d
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .base import SafeguardsBase, Signal
from ._utils import oanda_history, atr_series

log = logging.getLogger("vol_premium")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _VolTrade:
    units:       float
    entry_price: float
    stop_price:  float
    tp_price:    float
    atr:         float
    opened_at:   datetime = field(default_factory=_utcnow)


class VolPremiumStrategy(SafeguardsBase):
    """
    Sells volatility premium when IV/RV ratio is elevated but not tail-risk.

    Hard caps:
        - iv_rv_ratio >= 2.0           → kill switch (close + no entry)
        - estimated VIX > 30           → disable strategy entirely
        - total vol exposure >= 20% NAV → block new entries
    """

    strategy_name = "vol_premium"

    def __init__(self, api) -> None:
        self._api     = api
        self._trade:   Optional[_VolTrade] = None
        self._enabled: bool = True
        self._last_tick = _utcnow() - timedelta(seconds=config.VOL_POLL_SECONDS)

    # ── Main entry point ──────────────────────────────────────────────────────

    def tick(self, current_price: float | None = None) -> list[Signal]:
        if self.is_halted or not self._enabled:
            return []

        now = _utcnow()
        if (now - self._last_tick).total_seconds() < config.VOL_POLL_SECONDS:
            return []
        self._last_tick = now

        signals: list[Signal] = []
        try:
            signals = self._run(current_price)
        except Exception:
            log.exception("[%s] unhandled exception in tick()", self.strategy_name)
            closed = [config.VOL_INSTRUMENT] if self._trade else []
            self.trigger_hard_stop(
                reason="unhandled exception in VolPremiumStrategy.tick()",
                positions_closed=closed,
            )
        return signals

    # ── Core logic ────────────────────────────────────────────────────────────

    def _run(self, current_price: Optional[float]) -> list[Signal]:
        signals: list[Signal] = []
        nav = self._nav_safe()

        try:
            iv_proxy, rv_proxy, atr, last_close = self._vol_metrics()
        except Exception:
            log.warning("[%s] failed to compute vol metrics", self.strategy_name)
            return []

        iv_rv_ratio   = iv_proxy / rv_proxy if rv_proxy > 0 else 0.0
        price         = current_price if current_price is not None else last_close
        estimated_vix = iv_proxy * 100 * (252 ** 0.5)

        log.debug("[%s] iv=%.5f rv=%.5f ratio=%.3f vix_est=%.1f",
                  self.strategy_name, iv_proxy, rv_proxy, iv_rv_ratio, estimated_vix)

        # ── KILL SWITCHES (fire before any entry check) ────────────────────
        if iv_rv_ratio >= config.VOL_KILL_RATIO:
            log.warning("[%s] KILL: ratio=%.3f >= %.1f",
                        self.strategy_name, iv_rv_ratio, config.VOL_KILL_RATIO)
            if self._trade:
                signals += self._close("kill_iv_rv_ratio", price)
            return signals

        if estimated_vix > config.VOL_VIX_DISABLE:
            log.warning("[%s] VIX est=%.1f > %d — disabling strategy",
                        self.strategy_name, estimated_vix, config.VOL_VIX_DISABLE)
            self._enabled = False
            if self._trade:
                signals += self._close("vix_disable", price)
            return signals

        # ── Exit checks ───────────────────────────────────────────────────
        if self._trade:
            if current_price is None:
                return signals

            trade     = self._trade
            age_days  = (_utcnow() - trade.opened_at).total_seconds() / 86_400
            reason: Optional[str] = None

            if price >= trade.stop_price:
                reason = "stop_loss"
            elif price <= trade.tp_price:
                reason = "take_profit"
            elif iv_rv_ratio < config.VOL_CLOSE_RATIO:
                reason = "iv_rv_normalised"
            elif age_days > config.VOL_MAX_AGE_DAYS:
                reason = "time_exit"

            if reason:
                signals += self._close(reason, price)
            return signals

        # ── Entry ─────────────────────────────────────────────────────────
        if config.VOL_ENTRY_RATIO_MIN <= iv_rv_ratio < config.VOL_ENTRY_RATIO_MAX:
            # Exposure cap
            if nav > 0:
                current_exposure = (self._trade.units * price) if self._trade else 0.0
                if current_exposure / nav >= config.VOL_MAX_EXPOSURE_PCT:
                    log.info("[%s] exposure cap reached (%.1f%% NAV)",
                             self.strategy_name, config.VOL_MAX_EXPOSURE_PCT * 100)
                    return signals

            units = (nav * config.VOL_NAV_PCT) / price if price > 0 else 0
            if units <= 0:
                return signals

            stop = price + config.VOL_STOP_ATR_MULT * atr   # SHORT → stop above entry
            tp   = price - config.VOL_TP_ATR_MULT   * atr   # SHORT → TP below entry

            sig = Signal(
                instrument=config.VOL_INSTRUMENT,
                direction=-1,
                units=units,
                stop_price=stop,
                tp_price=tp,
                strategy=self.strategy_name,
                meta={"action": "open", "iv_rv_ratio": iv_rv_ratio, "vix_est": estimated_vix},
            )

            if not self.approve_trade(sig):
                return signals

            log.info("[%s] entry SHORT ratio=%.3f vix_est=%.1f",
                     self.strategy_name, iv_rv_ratio, estimated_vix)

            self._trade = _VolTrade(
                units=units, entry_price=price,
                stop_price=stop, tp_price=tp, atr=atr,
            )
            signals.append(sig)

        return signals

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _close(self, reason: str, price: float) -> list[Signal]:
        if not self._trade:
            return []
        log.info("[%s] closing trade reason=%s price=%.5f", self.strategy_name, reason, price)
        sig = Signal(
            instrument=config.VOL_INSTRUMENT,
            direction=+1,
            units=self._trade.units,
            stop_price=0.0,
            strategy=self.strategy_name,
            meta={"action": "close", "reason": reason},
        )
        self._trade = None
        return [sig]

    def _vol_metrics(self):
        """Returns (iv_proxy, rv_proxy, atr, last_close)."""
        end      = _utcnow()
        lookback = config.VOL_RV_PERIOD + config.VOL_IV_ATR_PERIOD + 15
        start    = end - timedelta(days=lookback)

        hist  = oanda_history(self._api, config.VOL_INSTRUMENT, start, end, "D")
        close = hist["c"].astype(float)
        high  = hist["h"].astype(float)
        low   = hist["l"].astype(float)

        atr_s     = atr_series(high, low, close, config.VOL_IV_ATR_PERIOD)
        iv_series = atr_s / close
        rv_series = iv_series.rolling(config.VOL_RV_PERIOD).mean()

        return (
            float(iv_series.iloc[-1]),
            float(rv_series.iloc[-1]),
            float(atr_s.iloc[-1]),
            float(close.iloc[-1]),
        )

    def _nav_safe(self) -> float:
        from .base import _nav
        return _nav if _nav > 0 else 100_000.0
