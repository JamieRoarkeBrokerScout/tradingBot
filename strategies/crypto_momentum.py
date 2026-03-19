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
    bar_count:    int      = 0       # M15 bars since entry (incremented each tick cycle)


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

            # 65%-to-TP reversal exit: if we've reached 65% of the way to TP but price
            # is now moving sideways/against us (RSI fading), lock in the gain and exit.
            trade.bar_count += 1
            if reason is None and trade.bar_count >= config.CRYPTO_RSI_MIN_HOLD_BARS:
                try:
                    latest_rsi = self._latest_rsi(inst)
                    rsi_exit_long  = config.CRYPTO_RSI_EXIT          # < 45
                    rsi_exit_short = 100 - config.CRYPTO_RSI_EXIT    # > 55

                    # Standard RSI cross-back exit
                    if trade.direction == +1 and latest_rsi < rsi_exit_long:
                        reason = "rsi_cross"
                    elif trade.direction == -1 and latest_rsi > rsi_exit_short:
                        reason = "rsi_cross"

                    # 65% to TP + momentum fading → exit early and reset
                    if reason is None and trade.tp_price != trade.entry_price:
                        tp_dist    = abs(trade.tp_price - trade.entry_price)
                        gain       = (price - trade.entry_price) * trade.direction
                        pct_to_tp  = gain / tp_dist if tp_dist > 0 else 0.0
                        # RSI fading: falling from high (long) or rising from low (short)
                        momentum_reversing = (
                            (trade.direction == +1 and latest_rsi < 55) or
                            (trade.direction == -1 and latest_rsi > 45)
                        )
                        if pct_to_tp >= 0.65 and momentum_reversing:
                            reason = "partial_tp_reversal"
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
            prev_rsi   = float(rsi_s.iloc[-2]) if len(rsi_s) >= 2 else last_rsi
            last_ma    = float(ma.iloc[-1])

            atr_pct = last_atr / last_close if last_close > 0 else 0.0

            log.info("[crypto] %s close=%.2f rsi=%.1f(prev=%.1f) ma%d=%.2f atr_pct=%.4f",
                     inst, last_close, last_rsi, prev_rsi, config.CRYPTO_MA_PERIOD, last_ma, atr_pct)

            if atr_pct < config.CRYPTO_MIN_ATR_PCT:
                log.info("[crypto] %s skip: atr_pct=%.4f below min %.4f",
                         inst, atr_pct, config.CRYPTO_MIN_ATR_PCT)
                continue

            # RSI crossover within last CRYPTO_CROSS_LOOKBACK bars
            lookback = getattr(config, 'CRYPTO_CROSS_LOOKBACK', 3)
            n = len(rsi_s)
            rsi_cross_long  = any(
                rsi_s.iloc[-(i + 2)] <= config.CRYPTO_RSI_LONG
                and rsi_s.iloc[-(i + 1)] > config.CRYPTO_RSI_LONG
                for i in range(min(lookback, n - 2))
            )
            rsi_cross_short = any(
                rsi_s.iloc[-(i + 2)] >= config.CRYPTO_RSI_SHORT
                and rsi_s.iloc[-(i + 1)] < config.CRYPTO_RSI_SHORT
                for i in range(min(lookback, n - 2))
            )

            direction: Optional[int] = None
            if rsi_cross_long and last_close > last_ma:
                direction = +1
            elif rsi_cross_short and last_close < last_ma:
                direction = -1

            if direction is None:
                trend = "above" if last_close > last_ma else "below"
                log.info("[crypto] %s skip: no RSI cross (rsi=%.1f long=%d short=%d ma_trend=%s)",
                         inst, last_rsi, config.CRYPTO_RSI_LONG, config.CRYPTO_RSI_SHORT, trend)
                continue

            stop_dist = config.CRYPTO_STOP_ATR_MULT * last_atr
            stop      = last_close - direction * stop_dist
            tp        = last_close + direction * config.CRYPTO_TP_ATR_MULT * last_atr

            # Target-leverage sizing: aim for 3× NAV notional, cap risk at MAX_RISK_PCT
            units_target = (nav * config.CRYPTO_TARGET_LEVERAGE) / last_close if last_close > 0 else 0
            if units_target <= 0:
                continue
            risk_pct = (units_target * stop_dist) / nav if nav > 0 else 1.0
            if risk_pct > config.CRYPTO_MAX_RISK_PCT:
                # ATR stop is too wide for this account size — scale down to fit risk cap
                units = (nav * config.CRYPTO_MAX_RISK_PCT) / stop_dist
            else:
                units = units_target

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
        # 72 h gives 288 M15 bars — enough for MA50 + RSI warmup on 24/7 crypto
        start = end - timedelta(hours=72)
        return oanda_history(self._api, instrument, start, end, config.CRYPTO_GRANULARITY)

    def _latest_rsi(self, instrument: str) -> float:
        df    = self._fetch_candles(instrument)
        close = df["c"].astype(float)
        return float(compute_rsi(close, config.CRYPTO_RSI_PERIOD).iloc[-1])

    def _nav_safe(self) -> float:
        """Use the Kraken account balance — not OANDA — for position sizing."""
        now = _utcnow()
        if (
            not hasattr(self, "_kraken_nav_cache")
            or (now - getattr(self, "_kraken_nav_ts", now)).total_seconds() > 300
        ):
            try:
                summary = self._api.get_account_summary()
                nav = float(summary.get("nav", summary.get("balance", 0)) or 0)
                if nav > 0:
                    self._kraken_nav_cache: float = nav
                    self._kraken_nav_ts = now
                    log.info("[crypto] Kraken NAV updated: $%.2f", nav)
            except Exception as exc:
                log.warning("[crypto] Kraken NAV fetch failed: %s", exc)
        cached = getattr(self, "_kraken_nav_cache", 0.0)
        return cached if cached > 0 else 1_000.0
