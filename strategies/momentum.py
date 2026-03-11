# strategies/momentum.py
"""
MomentumStrategy — RSI + volume + 200MA trend filter on H1 bars.

Instruments: SPX500_USD, XAU_USD
Poll:        every 5 minutes
Signals:     Signal objects only — no direct broker calls
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .base import SafeguardsBase, Signal
from ._utils import oanda_history, atr_series, rsi as compute_rsi

log = logging.getLogger("momentum")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── Internal trade tracking ──────────────────────────────────────────────────

@dataclass
class _Trade:
    instrument:   str
    direction:    int
    units:        float
    entry_price:  float
    stop_price:   float
    tp_price:     float
    atr:          float
    trail_active: bool     = False
    trail_stop:   float    = 0.0
    opened_at:    datetime = field(default_factory=_utcnow)


# ─── Strategy ─────────────────────────────────────────────────────────────────

class MomentumStrategy(SafeguardsBase):
    """
    Momentum entries on RSI extremes with volume confirmation and MA trend filter.

    Long:   RSI > 60  AND volume > 1.8× avg  AND price > 200MA
    Short:  RSI < 40  AND volume > 1.8× avg  AND price < 200MA
    Size:   1.2% NAV
    Stop:   2.0× ATR from entry
    TP:     3.5× ATR from entry
    Trail:  activates once 1× ATR in profit; stop at 1.5× ATR

    Exits:  stop / TP / trailing stop / RSI cross-back through 50 / age > 10d
    Filters:
        - Max 2 open momentum positions
        - Min 4 h between signals on same instrument
        - ATR < 0.3% of price → skip
    """

    strategy_name = "momentum"

    def __init__(self, api) -> None:
        self._api         = api
        self._trades:      dict[str, _Trade]    = {}
        self._last_signal: dict[str, datetime]  = {}
        self._last_tick = _utcnow() - timedelta(seconds=config.MOMENTUM_POLL_SECONDS)

    # ── Main entry point ──────────────────────────────────────────────────────

    def tick(self, current_prices: dict[str, float] | None = None) -> list[Signal]:
        if self.is_halted:
            return []

        now = _utcnow()
        if (now - self._last_tick).total_seconds() < config.MOMENTUM_POLL_SECONDS:
            return []
        self._last_tick = now

        signals: list[Signal] = []
        try:
            if current_prices:
                signals += self._manage_exits(current_prices)
            signals += self._scan_entries()
        except Exception:
            log.exception("[%s] unhandled exception in tick()", self.strategy_name)
            self.trigger_hard_stop(
                reason="unhandled exception in MomentumStrategy.tick()",
                positions_closed=list(self._trades.keys()),
            )
        return signals

    # ── Exit management ───────────────────────────────────────────────────────

    def _manage_exits(self, prices: dict[str, float]) -> list[Signal]:
        signals: list[Signal] = []

        for inst, trade in list(self._trades.items()):
            price = prices.get(inst)
            if price is None:
                continue

            age_days   = (_utcnow() - trade.opened_at).total_seconds() / 86_400
            reason: Optional[str] = None

            # Update or activate trailing stop
            if trade.trail_active:
                if trade.direction == +1:
                    new_trail = price - config.MOMENTUM_TRAIL_STOP * trade.atr
                    trade.trail_stop = max(trade.trail_stop, new_trail)
                    if price <= trade.trail_stop:
                        reason = "trailing_stop"
                else:
                    new_trail = price + config.MOMENTUM_TRAIL_STOP * trade.atr
                    trade.trail_stop = min(trade.trail_stop, new_trail)
                    if price >= trade.trail_stop:
                        reason = "trailing_stop"
            else:
                gain = (price - trade.entry_price) * trade.direction
                if gain >= config.MOMENTUM_TRAIL_TRIGGER * trade.atr:
                    trade.trail_active = True
                    if trade.direction == +1:
                        trade.trail_stop = price - config.MOMENTUM_TRAIL_STOP * trade.atr
                    else:
                        trade.trail_stop = price + config.MOMENTUM_TRAIL_STOP * trade.atr
                    log.info("[%s] trailing stop activated on %s", self.strategy_name, inst)

            # Hard stop
            if reason is None:
                if trade.direction == +1 and price <= trade.stop_price:
                    reason = "stop_loss"
                elif trade.direction == -1 and price >= trade.stop_price:
                    reason = "stop_loss"

            # Take profit
            if reason is None:
                if trade.direction == +1 and price >= trade.tp_price:
                    reason = "take_profit"
                elif trade.direction == -1 and price <= trade.tp_price:
                    reason = "take_profit"

            # Age exit
            if reason is None and age_days > config.MOMENTUM_MAX_AGE_DAYS:
                reason = "time_exit"

            # RSI cross back through 50
            if reason is None:
                try:
                    latest_rsi = self._latest_rsi(inst)
                    if trade.direction == +1 and latest_rsi < config.MOMENTUM_RSI_EXIT_LEVEL:
                        reason = "rsi_cross"
                    elif trade.direction == -1 and latest_rsi > config.MOMENTUM_RSI_EXIT_LEVEL:
                        reason = "rsi_cross"
                except Exception:
                    pass

            if reason:
                log.info("[%s] exit %s reason=%s", self.strategy_name, inst, reason)
                signals.append(Signal(
                    instrument=inst,
                    direction=-trade.direction,
                    units=trade.units,
                    stop_price=0.0,
                    strategy=self.strategy_name,
                    meta={"action": "close", "reason": reason},
                ))
                del self._trades[inst]

        return signals

    # ── Entry scanning ────────────────────────────────────────────────────────

    def _scan_entries(self) -> list[Signal]:
        signals: list[Signal] = []
        nav = self._nav_safe()

        if len(self._trades) >= config.MOMENTUM_MAX_OPEN:
            return signals

        for inst in config.MOMENTUM_INSTRUMENTS:
            if inst in self._trades:
                continue
            if len(self._trades) + len(signals) >= config.MOMENTUM_MAX_OPEN:
                break

            # Minimum gap between signals on same instrument
            last = self._last_signal.get(inst)
            if last and (_utcnow() - last).total_seconds() < config.MOMENTUM_MIN_GAP_HOURS * 3_600:
                continue

            try:
                df = self._fetch_candles(inst)
            except Exception:
                log.warning("[%s] failed to fetch %s", self.strategy_name, inst)
                continue

            if len(df) < config.MOMENTUM_MA_PERIOD:
                continue

            close  = df["c"].astype(float)
            high   = df["h"].astype(float)
            low    = df["l"].astype(float)
            vol    = df["volume"].astype(float) if "volume" in df.columns else close * 0

            atr_s   = atr_series(high, low, close, config.MOMENTUM_ATR_PERIOD)
            rsi_s   = compute_rsi(close, config.MOMENTUM_RSI_PERIOD)
            ma200   = close.rolling(config.MOMENTUM_MA_PERIOD).mean()
            avg_vol = vol.rolling(config.MOMENTUM_VOLUME_LOOKBACK).mean()

            last_close = float(close.iloc[-1])
            last_atr   = float(atr_s.iloc[-1])
            last_rsi   = float(rsi_s.iloc[-1])
            last_ma    = float(ma200.iloc[-1])
            last_vol   = float(vol.iloc[-1])
            last_avgv  = float(avg_vol.iloc[-1]) if not avg_vol.isna().iloc[-1] else 0.0

            if last_close > 0 and (last_atr / last_close) < config.MOMENTUM_MIN_ATR_PCT:
                continue

            vol_ok    = last_avgv > 0 and last_vol >= config.MOMENTUM_VOLUME_MULT * last_avgv
            direction: Optional[int] = None

            if last_rsi > config.MOMENTUM_RSI_LONG and vol_ok and last_close > last_ma:
                direction = +1
            elif last_rsi < config.MOMENTUM_RSI_SHORT and vol_ok and last_close < last_ma:
                direction = -1

            if direction is None:
                continue

            stop = last_close - direction * config.MOMENTUM_STOP_ATR_MULT * last_atr
            tp   = last_close + direction * config.MOMENTUM_TP_ATR_MULT   * last_atr

            stop_dist = abs(last_close - stop)
            units = (nav * config.MOMENTUM_NAV_PCT) / stop_dist if stop_dist > 0 else 0
            if units < 1:
                log.debug("[%s] skipping %s: units %.3f < 1 (nav=%.0f, stop_dist=%.4f)",
                          self.strategy_name, inst, units, nav, stop_dist)
                continue

            sig = Signal(
                instrument=inst, direction=direction, units=units,
                stop_price=stop, tp_price=tp, strategy=self.strategy_name,
                meta={"action": "open", "rsi": last_rsi, "atr": last_atr,
                      "stop_dist": stop_dist},
            )

            if not self.approve_trade(sig):
                continue

            log.info("[%s] entry %s dir=%+d rsi=%.1f atr=%.5f",
                     self.strategy_name, inst, direction, last_rsi, last_atr)

            self._trades[inst] = _Trade(
                instrument=inst, direction=direction, units=units,
                entry_price=last_close, stop_price=stop, tp_price=tp,
                atr=last_atr,
            )
            self._last_signal[inst] = _utcnow()
            signals.append(sig)

        return signals

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _fetch_candles(self, instrument: str):
        end   = _utcnow()
        # Need enough bars for the 200-period MA
        hours = max(config.MOMENTUM_CANDLES, config.MOMENTUM_MA_PERIOD) + 30
        start = end - timedelta(hours=hours)
        return oanda_history(self._api, instrument, start, end, config.MOMENTUM_GRANULARITY)

    def _latest_rsi(self, instrument: str) -> float:
        df    = self._fetch_candles(instrument)
        close = df["c"].astype(float)
        return float(compute_rsi(close, config.MOMENTUM_RSI_PERIOD).iloc[-1])

    def _nav_safe(self) -> float:
        from .base import _nav
        return _nav if _nav > 0 else 100_000.0
