# strategies/crypto_momentum.py
"""
CryptoMomentumStrategy — RSI + 50MA trend filter on H1 bars for BTC/ETH.

Instruments: BTC_USD, ETH_USD
Poll:        every 5 minutes (24/7 — no weekend blackout)
Signals:     Signal objects only — no direct broker calls

Long:  RSI > 60 AND price > 50MA
Short: RSI < 40 AND price < 50MA
Size:  2% NAV (smaller due to crypto volatility)
Stop:  2.5× ATR
TP:    4.0× ATR
Trail: activates once 1× ATR in profit; stop at 2× ATR
Exits: stop / TP / trailing stop / RSI midline cross / age > 7d
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .base import SafeguardsBase, Signal
from ._utils import oanda_history, atr_series, rsi as compute_rsi
from .learner import get_learner

log = logging.getLogger("crypto")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


class CryptoMomentumStrategy(SafeguardsBase):
    """
    Momentum strategy for crypto — identical logic to MomentumStrategy but
    tuned for 24/7 markets and higher volatility.
    """

    strategy_name   = "crypto"
    trades_weekends = True   # crypto is 24/7 — bypass weekend blackout

    def __init__(self, api) -> None:
        self._api         = api
        self._trades:      dict[str, _Trade]   = {}
        self._last_signal: dict[str, datetime] = {}
        self._last_tick = _utcnow() - timedelta(seconds=config.CRYPTO_POLL_SECONDS)

    def tick(self, current_prices: dict[str, float] | None = None) -> list[Signal]:
        if self.is_halted:
            return []

        now = _utcnow()
        if (now - self._last_tick).total_seconds() < config.CRYPTO_POLL_SECONDS:
            return []
        self._last_tick = now

        signals: list[Signal] = []
        try:
            if current_prices:
                signals += self._manage_exits(current_prices)
            signals += self._scan_entries()
        except Exception:
            log.exception("[crypto] unhandled exception in tick()")
            self.trigger_hard_stop(
                reason="unhandled exception in CryptoMomentumStrategy.tick()",
                positions_closed=list(self._trades.keys()),
            )
        return signals

    def _manage_exits(self, prices: dict[str, float]) -> list[Signal]:
        signals: list[Signal] = []

        for inst, trade in list(self._trades.items()):
            price = prices.get(inst)
            if price is None:
                continue

            age_days = (_utcnow() - trade.opened_at).total_seconds() / 86_400
            reason: Optional[str] = None

            # Trailing stop
            if trade.trail_active:
                if trade.direction == +1:
                    new_trail = price - config.CRYPTO_TRAIL_STOP * trade.atr
                    trade.trail_stop = max(trade.trail_stop, new_trail)
                    if price <= trade.trail_stop:
                        reason = "trailing_stop"
                else:
                    new_trail = price + config.CRYPTO_TRAIL_STOP * trade.atr
                    trade.trail_stop = min(trade.trail_stop, new_trail)
                    if price >= trade.trail_stop:
                        reason = "trailing_stop"
            else:
                gain = (price - trade.entry_price) * trade.direction
                if gain >= config.CRYPTO_TRAIL_TRIGGER * trade.atr:
                    trade.trail_active = True
                    if trade.direction == +1:
                        trade.trail_stop = price - config.CRYPTO_TRAIL_STOP * trade.atr
                    else:
                        trade.trail_stop = price + config.CRYPTO_TRAIL_STOP * trade.atr
                    log.info("[crypto] trailing stop activated on %s", inst)

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
            if reason is None and age_days > config.CRYPTO_MAX_AGE_DAYS:
                reason = "time_exit"

            # RSI midline cross-back
            if reason is None:
                try:
                    latest_rsi = self._latest_rsi(inst)
                    if trade.direction == +1 and latest_rsi < config.CRYPTO_RSI_EXIT:
                        reason = "rsi_cross"
                    elif trade.direction == -1 and latest_rsi > (100 - config.CRYPTO_RSI_EXIT):
                        reason = "rsi_cross"
                except Exception:
                    pass

            if reason:
                log.info("[crypto] exit %s reason=%s", inst, reason)
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

    def _scan_entries(self) -> list[Signal]:
        signals: list[Signal] = []
        nav = self._nav_safe()

        if len(self._trades) >= config.CRYPTO_MAX_OPEN:
            return signals

        for inst in config.CRYPTO_INSTRUMENTS:
            if inst in self._trades:
                continue
            if len(self._trades) + len(signals) >= config.CRYPTO_MAX_OPEN:
                break

            last = self._last_signal.get(inst)
            if last and (_utcnow() - last).total_seconds() < config.CRYPTO_MIN_GAP_HOURS * 3_600:
                continue

            try:
                df = self._fetch_candles(inst)
            except Exception as exc:
                log.warning("[crypto] failed to fetch %s: %s", inst, exc)
                continue

            if len(df) < config.CRYPTO_MA_PERIOD:
                continue

            close = df["c"].astype(float)
            high  = df["h"].astype(float)
            low   = df["l"].astype(float)

            atr_s  = atr_series(high, low, close, config.CRYPTO_ATR_PERIOD)
            rsi_s  = compute_rsi(close, config.CRYPTO_RSI_PERIOD)
            ma     = close.rolling(config.CRYPTO_MA_PERIOD).mean()

            last_close = float(close.iloc[-1])
            last_atr   = float(atr_s.iloc[-1])
            last_rsi   = float(rsi_s.iloc[-1])
            last_ma    = float(ma.iloc[-1])

            atr_pct = last_atr / last_close if last_close > 0 else 0.0

            log.info("[crypto] %s close=%.2f rsi=%.1f ma%d=%.2f atr_pct=%.4f",
                     inst, last_close, last_rsi, config.CRYPTO_MA_PERIOD, last_ma, atr_pct)

            if atr_pct < config.CRYPTO_MIN_ATR_PCT:
                log.info("[crypto] %s skip: atr_pct=%.4f below min %.4f",
                         inst, atr_pct, config.CRYPTO_MIN_ATR_PCT)
                continue

            direction: Optional[int] = None
            if last_rsi > config.CRYPTO_RSI_LONG and last_close > last_ma:
                direction = +1
            elif last_rsi < config.CRYPTO_RSI_SHORT and last_close < last_ma:
                direction = -1

            if direction is None:
                continue

            stop      = last_close - direction * config.CRYPTO_STOP_ATR_MULT * last_atr
            tp        = last_close + direction * config.CRYPTO_TP_ATR_MULT   * last_atr
            stop_dist = abs(last_close - stop)

            # Size by risk then apply leverage: (NAV × 2% × leverage) / stop distance
            units = (nav * config.CRYPTO_NAV_PCT * config.CRYPTO_LEVERAGE) / stop_dist if stop_dist > 0 else 0
            if units <= 0:
                continue

            learner_features = {
                "rsi": last_rsi, "atr_pct": atr_pct, "direction": direction,
            }
            allow, reason = get_learner().evaluate_entry(self.strategy_name, learner_features)
            if not allow:
                log.info("[crypto] learner blocked %s: %s", inst, reason)
                continue

            sig = Signal(
                instrument=inst, direction=direction, units=units,
                stop_price=stop, tp_price=tp, strategy=self.strategy_name,
                meta={"action": "open", "rsi": last_rsi, "atr": last_atr,
                      "atr_pct": atr_pct, "direction": direction,
                      "stop_dist": stop_dist},
            )

            if not self.approve_trade(sig):
                continue

            log.info("[crypto] entry %s dir=%+d rsi=%.1f atr=%.2f units=%.6f",
                     inst, direction, last_rsi, last_atr, units)

            self._trades[inst] = _Trade(
                instrument=inst, direction=direction, units=units,
                entry_price=last_close, stop_price=stop, tp_price=tp,
                atr=last_atr,
            )
            self._last_signal[inst] = _utcnow()
            signals.append(sig)

        return signals

    def _fetch_candles(self, instrument: str):
        end   = _utcnow()
        # Crypto is 24/7: 100 H1 bars = ~4.2 days. Use 6 days buffer.
        start = end - timedelta(days=6)
        return oanda_history(self._api, instrument, start, end, config.CRYPTO_GRANULARITY)

    def _latest_rsi(self, instrument: str) -> float:
        df    = self._fetch_candles(instrument)
        close = df["c"].astype(float)
        return float(compute_rsi(close, config.CRYPTO_RSI_PERIOD).iloc[-1])

    def _nav_safe(self) -> float:
        from .base import _nav
        return _nav if _nav > 0 else 100_000.0
