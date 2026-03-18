# strategies/scalp.py
"""
ScalpStrategy — fast 5-min EMA-crossover scalper.  "Swings to the fences."

Account:     ~$200 dedicated account
Instruments: NAS100_USD, XAU_USD, GBP_USD
Timeframe:   M5 (OANDA minimum practical; M3 not supported)
Poll:        every 60 seconds

Entry:
  Long:  8-EMA just crossed above 21-EMA AND RSI > 55
  Short: 8-EMA just crossed below 21-EMA AND RSI < 45

Sizing:  5% NAV per trade (≈ $10 on $200)
Stop:    1.5× ATR (tight)
TP:      4.5× ATR (3:1 R:R)
Exit:    SL / TP / force-close after 15 bars (75 min)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .base import SafeguardsBase, Signal
from ._utils import oanda_history, atr_series, rsi as compute_rsi

log = logging.getLogger("scalp")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Trade:
    instrument:  str
    direction:   int
    units:       float
    entry_price: float
    stop_price:  float
    tp_price:    float
    atr:         float
    opened_at:   datetime = field(default_factory=_utcnow)
    bar_count:   int = 0


class ScalpStrategy(SafeguardsBase):
    """High-frequency 5-min EMA-crossover scalper for a ~$200 account."""

    strategy_name      = "scalp"
    trades_weekends    = False
    max_trade_size_pct = 0.20   # allow up to 20% NAV risk per trade (high-leverage account)

    def __init__(self, api) -> None:
        self._api          = api
        self._trades:      dict[str, _Trade]   = {}
        self._last_signal: dict[str, datetime] = {}
        self._last_tick    = _utcnow() - timedelta(seconds=config.SCALP_POLL_SECONDS)

    def tick(self, current_prices: dict[str, float] | None = None) -> list[Signal]:
        if self.is_halted:
            return []

        now = _utcnow()
        if (now - self._last_tick).total_seconds() < config.SCALP_POLL_SECONDS:
            return []
        self._last_tick = now

        signals: list[Signal] = []
        try:
            prices = current_prices or {}
            if prices:
                signals += self._manage_exits(prices)
            signals += self._scan_entries()
        except Exception:
            log.exception("[scalp] unhandled exception in tick()")
        return signals

    # ── Exit management ────────────────────────────────────────────────────────

    def _manage_exits(self, prices: dict[str, float]) -> list[Signal]:
        signals = []
        for inst, trade in list(self._trades.items()):
            price = prices.get(inst)
            if price is None:
                continue

            trade.bar_count += 1
            reason: Optional[str] = None

            if trade.direction == +1:
                if price <= trade.stop_price:
                    reason = "stop_loss"
                elif price >= trade.tp_price:
                    reason = "take_profit"
            else:
                if price >= trade.stop_price:
                    reason = "stop_loss"
                elif price <= trade.tp_price:
                    reason = "take_profit"

            if reason is None and trade.bar_count >= config.SCALP_MAX_AGE_BARS:
                reason = "time_exit"

            if reason:
                log.info("[scalp] exit %s reason=%s price=%.5f", inst, reason, price)
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

    # ── Entry scanning ─────────────────────────────────────────────────────────

    def _scan_entries(self) -> list[Signal]:
        signals = []
        nav = self._nav_safe()

        if len(self._trades) >= config.SCALP_MAX_OPEN:
            return signals

        for inst in config.SCALP_INSTRUMENTS:
            if inst in self._trades:
                continue
            if len(self._trades) + len(signals) >= config.SCALP_MAX_OPEN:
                break

            # Cooldown: don't re-enter the same instrument too quickly
            last = self._last_signal.get(inst)
            if last and (_utcnow() - last).total_seconds() < config.SCALP_MIN_GAP_MINS * 60:
                continue

            try:
                df = self._fetch_candles(inst)
            except Exception as exc:
                log.warning("[scalp] failed to fetch %s: %s", inst, exc)
                continue

            if len(df) < config.SCALP_EMA_SLOW + 2:
                continue

            close = df["c"].astype(float)
            high  = df["h"].astype(float)
            low   = df["l"].astype(float)

            ema_fast = close.ewm(span=config.SCALP_EMA_FAST, adjust=False).mean()
            ema_slow = close.ewm(span=config.SCALP_EMA_SLOW, adjust=False).mean()
            atr_s    = atr_series(high, low, close, config.SCALP_ATR_PERIOD)
            rsi_s    = compute_rsi(close, config.SCALP_RSI_PERIOD)

            last_close  = float(close.iloc[-1])
            last_atr    = float(atr_s.iloc[-1])
            last_rsi    = float(rsi_s.iloc[-1])
            atr_pct     = last_atr / last_close if last_close > 0 else 0.0

            # EMA values: current and previous bar
            ema_f_now  = float(ema_fast.iloc[-1])
            ema_f_prev = float(ema_fast.iloc[-2])
            ema_s_now  = float(ema_slow.iloc[-1])
            ema_s_prev = float(ema_slow.iloc[-2])

            log.info("[scalp] %s close=%.5f rsi=%.1f ema_fast=%.5f ema_slow=%.5f atr_pct=%.4f",
                     inst, last_close, last_rsi, ema_f_now, ema_s_now, atr_pct)

            if atr_pct < config.SCALP_MIN_ATR_PCT:
                log.debug("[scalp] %s skip: ATR too low (%.4f)", inst, atr_pct)
                continue

            # Detect EMA crossover within last SCALP_CROSS_LOOKBACK bars
            # (not just the final bar) so a recent cross still triggers entry
            lookback = getattr(config, 'SCALP_CROSS_LOOKBACK', 3)
            n = len(ema_fast)
            bullish_cross = any(
                ema_fast.iloc[-(i + 2)] <= ema_slow.iloc[-(i + 2)]
                and ema_fast.iloc[-(i + 1)] > ema_slow.iloc[-(i + 1)]
                for i in range(min(lookback, n - 2))
            )
            bearish_cross = any(
                ema_fast.iloc[-(i + 2)] >= ema_slow.iloc[-(i + 2)]
                and ema_fast.iloc[-(i + 1)] < ema_slow.iloc[-(i + 1)]
                for i in range(min(lookback, n - 2))
            )

            direction: Optional[int] = None
            if bullish_cross and last_rsi > config.SCALP_RSI_LONG:
                direction = +1
            elif bearish_cross and last_rsi < config.SCALP_RSI_SHORT:
                direction = -1

            if direction is None:
                continue

            stop_dist = config.SCALP_STOP_ATR_MULT * last_atr
            stop      = last_close - direction * stop_dist
            tp        = last_close + direction * config.SCALP_TP_ATR_MULT * last_atr
            units     = (nav * config.SCALP_NAV_PCT) / stop_dist if stop_dist > 0 else 0

            if units <= 0:
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

            log.info("[scalp] ENTRY %s dir=%+d rsi=%.1f ema_cross atr=%.5f units=%.2f stop=%.5f tp=%.5f",
                     inst, direction, last_rsi, last_atr, units, stop, tp)

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
        start = end - timedelta(hours=10)   # 120 M5 bars
        return oanda_history(self._api, instrument, start, end, config.SCALP_GRANULARITY)

    def _nav_safe(self) -> float:
        from .base import _nav
        return _nav if _nav > 0 else 200.0   # default to $200 if not yet updated
