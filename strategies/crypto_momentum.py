# strategies/crypto_momentum.py
"""
CryptoMomentumStrategy — multi-timeframe momentum for BTC/ETH/SOL on Kraken Futures.

Timeframes:
  H1  — EMA-50 broad trend filter (≈ 2-day trend)
  M15 — RSI(14), MACD(12/26/9), ATR(14), EMA-50, volume

Entry (Long):
  1. Price > H1 EMA-50         (broad trend up)
  2. Price > M15 EMA-50        (local trend up)
  3. RSI crosses above 52      (within last CROSS_LOOKBACK bars)
  4. MACD line > Signal line   (momentum confirming)
  5. Volume ≥ 1.2× 20-bar avg (real move, not noise — skipped if no volume data)

Entry (Short): mirror of all conditions above.

Sizing:
  - Targets 3× NAV leverage (meaningful crypto exposure)
  - Reduced to 1.5× if ATR/price > 2.5% (high-volatility regime)
  - Hard cap: CRYPTO_MAX_RISK_PCT (5% NAV) per trade

Exits (first reason wins):
  - Hard stop:        1.5× ATR
  - Take profit:      3.0× ATR
  - Trailing stop:    activates at 1×ATR profit; trails 0.8×ATR behind price
  - RSI reversal:     long exits when RSI < 45; short when RSI > 55
  - MACD cross-back:  MACD line crosses against trade direction
  - 65%-to-TP fade:   price ≥ 65% of the way to TP but momentum fading
  - Age:              7 days
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


def _ema(series, period: int):
    return series.ewm(span=period, adjust=False).mean()


def _macd(close, fast: int, slow: int, signal: int):
    """Return (macd_line, signal_line) series."""
    macd_line   = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line


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
    bar_count:    int      = 0


class CryptoMomentumStrategy(SafeguardsBase):
    """
    Multi-timeframe momentum for crypto — RSI + MACD + volume on M15,
    with H1 EMA-50 broad trend filter and dynamic volatility-adjusted sizing.
    """

    strategy_name   = "crypto"
    trades_weekends = True   # crypto is 24/7

    def __init__(self, api) -> None:
        self._api          = api
        self._trades:      dict[str, _Trade]   = {}
        self._last_signal: dict[str, datetime] = {}
        self._last_tick = _utcnow() - timedelta(seconds=config.CRYPTO_POLL_SECONDS)

    # ── Main tick ─────────────────────────────────────────────────────────────

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

    # ── Exit management ───────────────────────────────────────────────────────

    def _manage_exits(self, prices: dict[str, float]) -> list[Signal]:
        signals: list[Signal] = []

        for inst, trade in list(self._trades.items()):
            price = prices.get(inst)
            if price is None:
                continue

            age_days = (_utcnow() - trade.opened_at).total_seconds() / 86_400
            reason: Optional[str] = None

            # ── Trailing stop ──────────────────────────────────────────────
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
                    log.info("[crypto] trailing stop activated on %s @ %.2f", inst, price)

            # ── Hard stop ──────────────────────────────────────────────────
            if reason is None:
                if trade.direction == +1 and price <= trade.stop_price:
                    reason = "stop_loss"
                elif trade.direction == -1 and price >= trade.stop_price:
                    reason = "stop_loss"

            # ── Take profit ────────────────────────────────────────────────
            if reason is None:
                if trade.direction == +1 and price >= trade.tp_price:
                    reason = "take_profit"
                elif trade.direction == -1 and price <= trade.tp_price:
                    reason = "take_profit"

            # ── Age exit ───────────────────────────────────────────────────
            if reason is None and age_days > config.CRYPTO_MAX_AGE_DAYS:
                reason = "time_exit"

            # ── Indicator exits (RSI, MACD, 65%-to-TP fade) ───────────────
            trade.bar_count += 1
            if reason is None and trade.bar_count >= config.CRYPTO_RSI_MIN_HOLD_BARS:
                try:
                    df    = self._fetch_candles(inst)
                    close = df["c"].astype(float)

                    rsi_s              = compute_rsi(close, config.CRYPTO_RSI_PERIOD)
                    macd_line, sig_line = _macd(
                        close,
                        config.CRYPTO_MACD_FAST,
                        config.CRYPTO_MACD_SLOW,
                        config.CRYPTO_MACD_SIGNAL,
                    )

                    latest_rsi  = float(rsi_s.iloc[-1])
                    macd_now    = float(macd_line.iloc[-1])
                    macd_prev   = float(macd_line.iloc[-2]) if len(macd_line) >= 2 else macd_now
                    signal_now  = float(sig_line.iloc[-1])
                    signal_prev = float(sig_line.iloc[-2]) if len(sig_line) >= 2 else signal_now

                    rsi_exit_long  = config.CRYPTO_RSI_EXIT
                    rsi_exit_short = 100 - config.CRYPTO_RSI_EXIT

                    # RSI cross-back exit
                    if trade.direction == +1 and latest_rsi < rsi_exit_long:
                        reason = "rsi_cross"
                    elif trade.direction == -1 and latest_rsi > rsi_exit_short:
                        reason = "rsi_cross"

                    # MACD cross against position direction
                    if reason is None:
                        macd_crossed_down = macd_prev >= signal_prev and macd_now < signal_now
                        macd_crossed_up   = macd_prev <= signal_prev and macd_now > signal_now
                        if trade.direction == +1 and macd_crossed_down:
                            reason = "macd_cross"
                        elif trade.direction == -1 and macd_crossed_up:
                            reason = "macd_cross"

                    # 65%-to-TP + momentum fading → early exit
                    if reason is None and trade.tp_price != trade.entry_price:
                        tp_dist   = abs(trade.tp_price - trade.entry_price)
                        gain      = (price - trade.entry_price) * trade.direction
                        pct_to_tp = gain / tp_dist if tp_dist > 0 else 0.0
                        momentum_fading = (
                            (trade.direction == +1 and latest_rsi < 55) or
                            (trade.direction == -1 and latest_rsi > 45)
                        )
                        if pct_to_tp >= 0.65 and momentum_fading:
                            reason = "partial_tp_reversal"

                    log.info("[crypto] exit-check %s rsi=%.1f macd=%.4f sig=%.4f reason=%s",
                             inst, latest_rsi, macd_now, signal_now, reason or "none")
                except Exception:
                    log.exception("[crypto] exit indicator fetch failed for %s", inst)

            if reason:
                log.info("[crypto] exit %s reason=%s price=%.2f", inst, reason, price)
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

            # ── Fetch M15 and H1 candles ───────────────────────────────────
            try:
                df    = self._fetch_candles(inst)
                df_h1 = self._fetch_h1_candles(inst)
            except Exception as exc:
                log.warning("[crypto] failed to fetch %s: %s", inst, exc)
                continue

            min_bars = max(
                config.CRYPTO_MA_PERIOD,
                config.CRYPTO_MACD_SLOW + config.CRYPTO_MACD_SIGNAL,
            )
            if len(df) < min_bars or len(df_h1) < config.CRYPTO_H1_MA_PERIOD:
                continue

            close  = df["c"].astype(float)
            high   = df["h"].astype(float)
            low    = df["l"].astype(float)

            # ── Indicators ────────────────────────────────────────────────
            atr_s               = atr_series(high, low, close, config.CRYPTO_ATR_PERIOD)
            rsi_s               = compute_rsi(close, config.CRYPTO_RSI_PERIOD)
            ema_m15             = _ema(close, config.CRYPTO_MA_PERIOD)
            macd_line, sig_line = _macd(
                close,
                config.CRYPTO_MACD_FAST,
                config.CRYPTO_MACD_SLOW,
                config.CRYPTO_MACD_SIGNAL,
            )

            # H1 broad trend filter
            close_h1  = df_h1["c"].astype(float)
            ema_h1    = _ema(close_h1, config.CRYPTO_H1_MA_PERIOD)

            last_close   = float(close.iloc[-1])
            last_atr     = float(atr_s.iloc[-1])
            last_rsi     = float(rsi_s.iloc[-1])
            last_ema_m15 = float(ema_m15.iloc[-1])
            last_ema_h1  = float(ema_h1.iloc[-1])
            last_macd    = float(macd_line.iloc[-1])
            last_signal  = float(sig_line.iloc[-1])
            atr_pct      = last_atr / last_close if last_close > 0 else 0.0

            # ── Volume confirmation (optional — graceful if no data) ───────
            vol_ok = True
            vol_col = next((c for c in ("volume", "v", "tickVolume") if c in df.columns), None)
            if vol_col:
                volume  = df[vol_col].astype(float)
                if len(volume) >= config.CRYPTO_VOLUME_LOOKBACK + 1:
                    avg_vol  = float(volume.iloc[-(config.CRYPTO_VOLUME_LOOKBACK + 1):-1].mean())
                    last_vol = float(volume.iloc[-1])
                    if avg_vol > 0:
                        vol_ok = last_vol >= config.CRYPTO_VOLUME_MULT * avg_vol

            log.info(
                "[crypto] %s close=%.2f rsi=%.1f macd=%.4f/sig=%.4f "
                "ema_m15=%.2f ema_h1=%.2f atr_pct=%.4f vol_ok=%s",
                inst, last_close, last_rsi, last_macd, last_signal,
                last_ema_m15, last_ema_h1, atr_pct, vol_ok,
            )

            if atr_pct < config.CRYPTO_MIN_ATR_PCT:
                log.info("[crypto] %s skip: atr_pct=%.4f below min %.4f",
                         inst, atr_pct, config.CRYPTO_MIN_ATR_PCT)
                continue

            # ── RSI crossover within last N bars ──────────────────────────
            n = len(rsi_s)
            lookback = config.CRYPTO_CROSS_LOOKBACK
            rsi_cross_long = any(
                rsi_s.iloc[-(i + 2)] <= config.CRYPTO_RSI_LONG
                and rsi_s.iloc[-(i + 1)] > config.CRYPTO_RSI_LONG
                for i in range(min(lookback, n - 2))
            )
            rsi_cross_short = any(
                rsi_s.iloc[-(i + 2)] >= config.CRYPTO_RSI_SHORT
                and rsi_s.iloc[-(i + 1)] < config.CRYPTO_RSI_SHORT
                for i in range(min(lookback, n - 2))
            )

            macd_bullish = last_macd > last_signal
            macd_bearish = last_macd < last_signal
            h1_up        = last_close > last_ema_h1
            h1_down      = last_close < last_ema_h1
            m15_up       = last_close > last_ema_m15
            m15_down     = last_close < last_ema_m15

            direction: Optional[int] = None
            if rsi_cross_long  and m15_up   and h1_up   and macd_bullish and vol_ok:
                direction = +1
            elif rsi_cross_short and m15_down and h1_down and macd_bearish and vol_ok:
                direction = -1

            if direction is None:
                log.info(
                    "[crypto] %s skip: rsi_xL=%s rsi_xS=%s m15_up=%s h1_up=%s "
                    "macd_bull=%s vol_ok=%s",
                    inst, rsi_cross_long, rsi_cross_short, m15_up, h1_up,
                    macd_bullish, vol_ok,
                )
                continue

            stop_dist = config.CRYPTO_STOP_ATR_MULT * last_atr
            stop      = last_close - direction * stop_dist
            tp        = last_close + direction * config.CRYPTO_TP_ATR_MULT * last_atr

            # ── Dynamic leverage: halve in high-vol regime ────────────────
            target_lev = config.CRYPTO_TARGET_LEVERAGE
            if atr_pct > config.CRYPTO_HIGH_VOL_THRESH:
                target_lev = config.CRYPTO_HIGH_VOL_LEV
                log.info("[crypto] %s high-vol regime (atr_pct=%.4f) → leverage %.1f×",
                         inst, atr_pct, target_lev)

            units_target = (nav * target_lev) / last_close if last_close > 0 else 0
            if units_target <= 0:
                continue

            risk_pct = (units_target * stop_dist) / nav if nav > 0 else 1.0
            units = (
                (nav * config.CRYPTO_MAX_RISK_PCT) / stop_dist
                if risk_pct > config.CRYPTO_MAX_RISK_PCT
                else units_target
            )

            learner_features = {
                "rsi": last_rsi, "atr_pct": atr_pct, "direction": direction,
                "macd_bull": float(macd_bullish), "h1_trend": float(h1_up),
            }
            allow, block_reason = get_learner().evaluate_entry(self.strategy_name, learner_features)
            if not allow:
                log.info("[crypto] learner blocked %s: %s", inst, block_reason)
                continue

            sig = Signal(
                instrument=inst, direction=direction, units=units,
                stop_price=stop, tp_price=tp, strategy=self.strategy_name,
                meta={
                    "action":    "open",
                    "rsi":       last_rsi,
                    "atr":       last_atr,
                    "atr_pct":   atr_pct,
                    "direction": direction,
                    "macd":      last_macd,
                    "h1_ema":    last_ema_h1,
                    "stop_dist": stop_dist,
                    "leverage":  target_lev,
                },
            )

            if not self.approve_trade(sig):
                continue

            log.info(
                "[crypto] ENTRY %s dir=%+d rsi=%.1f macd=%.4f atr=%.2f "
                "units=%.6f lev=%.1f× stop=%.2f tp=%.2f",
                inst, direction, last_rsi, last_macd, last_atr,
                units, target_lev, stop, tp,
            )

            self._trades[inst] = _Trade(
                instrument=inst, direction=direction, units=units,
                entry_price=last_close, stop_price=stop, tp_price=tp,
                atr=last_atr,
            )
            self._last_signal[inst] = _utcnow()
            signals.append(sig)

        return signals

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _fetch_candles(self, instrument: str):
        end   = _utcnow()
        start = end - timedelta(hours=72)   # 288 M15 bars — enough for all indicators
        return oanda_history(self._api, instrument, start, end, config.CRYPTO_GRANULARITY)

    def _fetch_h1_candles(self, instrument: str):
        end   = _utcnow()
        start = end - timedelta(hours=config.CRYPTO_H1_MA_PERIOD + 10)
        return oanda_history(self._api, instrument, start, end, "H1")

    # ── NAV helper ────────────────────────────────────────────────────────────

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
