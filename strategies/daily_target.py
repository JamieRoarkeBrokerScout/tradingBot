# strategies/daily_target.py
"""
DailyTargetStrategy — sole purpose: get +2% NAV per day, then stop.

Instruments: EUR_USD, GBP_USD, NAS100_USD, XAU_USD, SPX500_USD
Poll:        every 5 minutes
Timeframe:   M15 bars

Entry logic:
  Long:  RSI > 55 AND close > 20-period MA
  Short: RSI < 45 AND close < 20-period MA

Daily limits (reset at 00:00 UTC):
  +2% NAV (realized + unrealized) → close all positions, stop new entries
  -3% NAV                         → close all positions, halt for the day
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import config
from .base import SafeguardsBase, Signal
from ._utils import oanda_history, atr_series, rsi as compute_rsi

log = logging.getLogger("daily_target")


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


class DailyTargetStrategy(SafeguardsBase):
    """Target +2% NAV per day; hard-stop at -3% daily loss."""

    strategy_name   = "daily_target"
    trades_weekends = False   # respects weekend blackout

    def __init__(self, api) -> None:
        self._api          = api
        self._trades:      dict[str, _Trade]   = {}
        self._last_signal: dict[str, datetime] = {}
        self._last_tick    = _utcnow() - timedelta(seconds=config.DT_POLL_SECONDS)
        self._trading_date: date = _utcnow().date()
        self._target_reached: bool = False
        self._daily_halted:   bool = False

    def tick(self, current_prices: dict[str, float] | None = None) -> list[Signal]:
        if self.is_halted:
            return []

        now = _utcnow()
        if (now - self._last_tick).total_seconds() < config.DT_POLL_SECONDS:
            return []
        self._last_tick = now

        # Reset daily limits on new calendar day (UTC)
        today = now.date()
        if today != self._trading_date:
            self._trading_date    = today
            self._target_reached  = False
            self._daily_halted    = False
            log.info("[daily_target] New trading day %s — daily limits reset", today)

        prices  = current_prices or {}
        signals: list[Signal] = []

        try:
            nav       = self._nav_safe()
            daily_pl  = self._realized_pl_today() + self._unrealized_pl(prices)
            daily_pct = daily_pl / nav if nav > 0 else 0.0

            if daily_pct >= config.DT_TARGET_PCT:
                if not self._target_reached:
                    log.info(
                        "[daily_target] +2%% target reached (%.2f%% / $%.2f) — "
                        "closing positions and stopping for today",
                        daily_pct * 100, daily_pl,
                    )
                    self._target_reached = True
                    signals += self._close_all("daily_target_reached")
                return signals

            if daily_pct <= -config.DT_LOSS_LIMIT_PCT:
                if not self._daily_halted:
                    log.info(
                        "[daily_target] -3%% loss limit hit (%.2f%% / $%.2f) — "
                        "closing positions and halting for today",
                        daily_pct * 100, daily_pl,
                    )
                    self._daily_halted = True
                    signals += self._close_all("daily_loss_limit")
                return signals

            if self._target_reached or self._daily_halted:
                return signals

            if prices:
                signals += self._manage_exits(prices)
            signals += self._scan_entries()

        except Exception:
            log.exception("[daily_target] unhandled exception in tick()")

        return signals

    # ── Daily P&L helpers ──────────────────────────────────────────────────────

    def _realized_pl_today(self) -> float:
        """Sum of raw_pl for all daily_target trades closed today (UTC)."""
        today = _utcnow().strftime("%Y-%m-%d")
        try:
            from database.database import DB_PATH
            conn = sqlite3.connect(str(DB_PATH))
            row  = conn.execute(
                "SELECT COALESCE(SUM(raw_pl), 0) FROM trades "
                "WHERE strategy_name=? AND date(exit_time)=?",
                ("daily_target", today),
            ).fetchone()
            conn.close()
            return float(row[0])
        except Exception:
            return 0.0

    def _unrealized_pl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for inst, trade in self._trades.items():
            price = prices.get(inst)
            if price:
                total += (price - trade.entry_price) * trade.direction * trade.units
        return total

    # ── Signal generators ──────────────────────────────────────────────────────

    def _close_all(self, reason: str) -> list[Signal]:
        signals = []
        for inst, trade in list(self._trades.items()):
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

    def _manage_exits(self, prices: dict[str, float]) -> list[Signal]:
        signals = []
        for inst, trade in list(self._trades.items()):
            price = prices.get(inst)
            if price is None:
                continue

            age_hours = (_utcnow() - trade.opened_at).total_seconds() / 3_600
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

            if reason is None and age_hours > config.DT_MAX_AGE_HOURS:
                reason = "time_exit"

            if reason:
                log.info("[daily_target] exit %s reason=%s", inst, reason)
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
        signals = []
        nav = self._nav_safe()

        if len(self._trades) >= config.DT_MAX_OPEN:
            return signals

        for inst in config.DT_INSTRUMENTS:
            if inst in self._trades:
                continue
            if len(self._trades) + len(signals) >= config.DT_MAX_OPEN:
                break

            last = self._last_signal.get(inst)
            if last and (_utcnow() - last).total_seconds() < config.DT_MIN_GAP_HOURS * 3_600:
                continue

            try:
                df = self._fetch_candles(inst)
            except Exception as exc:
                log.warning("[daily_target] failed to fetch %s: %s", inst, exc)
                continue

            if len(df) < config.DT_MA_PERIOD:
                continue

            close  = df["c"].astype(float)
            high   = df["h"].astype(float)
            low    = df["l"].astype(float)
            atr_s  = atr_series(high, low, close, config.DT_ATR_PERIOD)
            rsi_s  = compute_rsi(close, config.DT_RSI_PERIOD)
            ma     = close.rolling(config.DT_MA_PERIOD).mean()

            last_close = float(close.iloc[-1])
            last_atr   = float(atr_s.iloc[-1])
            last_rsi   = float(rsi_s.iloc[-1])
            last_ma    = float(ma.iloc[-1])
            atr_pct    = last_atr / last_close if last_close > 0 else 0.0

            log.info("[daily_target] %s close=%.4f rsi=%.1f ma%d=%.4f atr_pct=%.4f",
                     inst, last_close, last_rsi, config.DT_MA_PERIOD, last_ma, atr_pct)

            if atr_pct < config.DT_MIN_ATR_PCT:
                continue

            direction: Optional[int] = None
            if last_rsi > config.DT_RSI_LONG and last_close > last_ma:
                direction = +1
            elif last_rsi < config.DT_RSI_SHORT and last_close < last_ma:
                direction = -1

            if direction is None:
                continue

            stop      = last_close - direction * config.DT_STOP_ATR_MULT * last_atr
            tp        = last_close + direction * config.DT_TP_ATR_MULT   * last_atr
            stop_dist = abs(last_close - stop)

            units = (nav * config.DT_NAV_PCT) / stop_dist if stop_dist > 0 else 0
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

            log.info("[daily_target] entry %s dir=%+d rsi=%.1f atr_pct=%.4f units=%.2f",
                     inst, direction, last_rsi, atr_pct, units)

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
        start = end - timedelta(hours=48)   # 192 M15 bars — enough for MA + RSI warmup
        return oanda_history(self._api, instrument, start, end, config.DT_GRANULARITY)

    def _nav_safe(self) -> float:
        """Read NAV from this strategy's own OANDA account (5-min cache)."""
        now = _utcnow()
        if (not hasattr(self, "_dt_nav_cache")
                or (now - getattr(self, "_dt_nav_ts", now)).total_seconds() > 300):
            try:
                summary = self._api.get_account_summary()
                nav = float(summary.get("NAV", summary.get("balance", 0)) or 0)
                if nav > 0:
                    self._dt_nav_cache: float = nav
                    self._dt_nav_ts = now
            except Exception as exc:
                log.warning("[daily_target] NAV fetch failed: %s", exc)
        cached = getattr(self, "_dt_nav_cache", 0.0)
        if cached > 0:
            return cached
        from .base import _nav
        return _nav if _nav > 0 else 1_000.0
