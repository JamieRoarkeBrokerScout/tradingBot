# strategies/stat_arb.py
"""
StatArbStrategy — log-spread z-score pairs trading.

Pairs:     XAU_USD / XAG_USD   and   SPX500_USD / BCO_USD
Data:      60 days of daily closes
Poll:      every 5 minutes
Signals:   Signal objects only — no direct broker calls
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from . import config
from .base import SafeguardsBase, Signal
from ._utils import oanda_history, atr_scalar

log = logging.getLogger("stat_arb")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── Internal position tracking ───────────────────────────────────────────────

@dataclass
class _Leg:
    instrument: str
    units:      float
    direction:  int
    entry_price: float
    stop_price:  float


@dataclass
class _Position:
    pair_key:  str
    leg_a:     _Leg
    leg_b:     _Leg
    opened_at: datetime = field(default_factory=_utcnow)


# ─── Strategy ─────────────────────────────────────────────────────────────────

class StatArbStrategy(SafeguardsBase):
    """
    Statistical arbitrage on co-integrated pairs.

    Entry:  z >= +2.0  →  SHORT A, LONG  B
            z <= -2.0  →  LONG  A, SHORT B
    Exit:   |z| <= 0.5 (mean reversion)
            |z| >= 3.5 (emergency divergence)
            age > 30 days (time exit)
    Filters:
        - pair already has open position → skip
        - 60d correlation < 0.75 → skip
        - spread std < 0.005 → skip
    """

    strategy_name = "stat_arb"

    def __init__(self, api) -> None:
        self._api    = api
        self._open:  dict[str, _Position] = {}
        self._last_tick = _utcnow() - timedelta(seconds=config.STAT_ARB_POLL_SECONDS)

    # ── Main entry point ──────────────────────────────────────────────────────

    def tick(self) -> list[Signal]:
        if self.is_halted:
            return []

        now = _utcnow()
        if (now - self._last_tick).total_seconds() < config.STAT_ARB_POLL_SECONDS:
            return []
        self._last_tick = now

        signals: list[Signal] = []
        try:
            signals += self._manage_exits()
            signals += self._scan_entries()
        except Exception:
            log.exception("[%s] unhandled exception in tick()", self.strategy_name)
            self.trigger_hard_stop(
                reason="unhandled exception in StatArbStrategy.tick()",
                positions_closed=list(self._open.keys()),
            )
        return signals

    # ── Exit management ───────────────────────────────────────────────────────

    def _manage_exits(self) -> list[Signal]:
        signals: list[Signal] = []
        for pair_key, pos in list(self._open.items()):
            inst_a, inst_b = pair_key.split("/")
            try:
                _, _, z, _, _ = self._fetch_metrics(inst_a, inst_b)
            except Exception as exc:
                log.warning("[%s] could not compute z for %s: %s; skipping exit", self.strategy_name, pair_key, exc)
                continue

            age_days = (_utcnow() - pos.opened_at).total_seconds() / 86_400
            reason: Optional[str] = None

            if abs(z) <= config.STAT_ARB_EXIT_Z:
                reason = "mean_reversion"
            elif abs(z) >= config.STAT_ARB_EMERGENCY_Z:
                reason = "emergency_divergence"
            elif age_days > config.STAT_ARB_MAX_AGE_DAYS:
                reason = "time_exit"

            if reason:
                log.info("[%s] exit %s z=%.3f age=%.1fd reason=%s",
                         self.strategy_name, pair_key, z, age_days, reason)
                for leg in (pos.leg_a, pos.leg_b):
                    signals.append(Signal(
                        instrument=leg.instrument,
                        direction=-leg.direction,
                        units=leg.units,
                        stop_price=0.0,
                        strategy=self.strategy_name,
                        meta={"action": "close", "reason": reason, "pair": pair_key},
                    ))
                del self._open[pair_key]

        return signals

    # ── Entry scanning ────────────────────────────────────────────────────────

    def _scan_entries(self) -> list[Signal]:
        signals: list[Signal] = []
        nav = self._nav_safe()

        for inst_a, inst_b in config.STAT_ARB_PAIRS:
            pair_key = f"{inst_a}/{inst_b}"
            if pair_key in self._open:
                continue

            try:
                closes_a, closes_b, z, corr, spread_std = self._fetch_metrics(inst_a, inst_b)
            except Exception as exc:
                log.warning("[%s] fetch failed for %s: %s", self.strategy_name, pair_key, exc)
                continue

            # Filters
            if corr < config.STAT_ARB_MIN_CORRELATION:
                log.debug("[%s] %s corr=%.3f below min", self.strategy_name, pair_key, corr)
                continue
            if spread_std < config.STAT_ARB_MIN_SPREAD_STD:
                log.debug("[%s] %s spread_std=%.5f below min", self.strategy_name, pair_key, spread_std)
                continue

            if z >= config.STAT_ARB_ENTRY_Z:
                dir_a, dir_b = -1, +1
            elif z <= -config.STAT_ARB_ENTRY_Z:
                dir_a, dir_b = +1, -1
            else:
                continue

            price_a = float(closes_a.iloc[-1])
            price_b = float(closes_b.iloc[-1])
            if price_a <= 0 or price_b <= 0:
                continue

            atr_a = self._daily_atr(inst_a)
            atr_b = self._daily_atr(inst_b)

            stop_dist_a = config.STAT_ARB_STOP_ATR_MULT * atr_a
            stop_dist_b = config.STAT_ARB_STOP_ATR_MULT * atr_b
            size_a = (nav * config.STAT_ARB_NAV_PCT) / stop_dist_a if stop_dist_a > 0 else 0
            size_b = (nav * config.STAT_ARB_NAV_PCT) / stop_dist_b if stop_dist_b > 0 else 0
            if size_a < 1 or size_b < 1:
                log.debug("[%s] skipping %s: size_a=%.3f size_b=%.3f both need >=1",
                          self.strategy_name, pair_key, size_a, size_b)
                continue

            stop_a = price_a - dir_a * config.STAT_ARB_STOP_ATR_MULT * atr_a
            stop_b = price_b - dir_b * config.STAT_ARB_STOP_ATR_MULT * atr_b

            sig_a = Signal(
                instrument=inst_a, direction=dir_a, units=size_a,
                stop_price=stop_a, strategy=self.strategy_name,
                meta={"action": "open", "leg": "A", "pair": pair_key, "z": z,
                      "stop_dist": stop_dist_a},
            )
            sig_b = Signal(
                instrument=inst_b, direction=dir_b, units=size_b,
                stop_price=stop_b, strategy=self.strategy_name,
                meta={"action": "open", "leg": "B", "pair": pair_key, "z": z,
                      "stop_dist": stop_dist_b},
            )

            if not (self.approve_trade(sig_a) and self.approve_trade(sig_b)):
                continue

            log.info("[%s] entry %s z=%.3f dir_a=%+d dir_b=%+d",
                     self.strategy_name, pair_key, z, dir_a, dir_b)

            self._open[pair_key] = _Position(
                pair_key=pair_key,
                leg_a=_Leg(inst_a, size_a, dir_a, price_a, stop_a),
                leg_b=_Leg(inst_b, size_b, dir_b, price_b, stop_b),
            )
            signals += [sig_a, sig_b]

        return signals

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _fetch_metrics(self, inst_a: str, inst_b: str):
        """Returns (closes_a, closes_b, z_score, correlation, spread_std)."""
        end   = _utcnow()
        start = end - timedelta(days=config.STAT_ARB_LOOKBACK_DAYS + 10)

        hist_a = oanda_history(self._api, inst_a, start, end, "D")
        hist_b = oanda_history(self._api, inst_b, start, end, "D")

        closes_a = hist_a["c"].astype(float)
        closes_b = hist_b["c"].astype(float)

        # Align on common dates — different instruments have different trading calendars
        common = closes_a.index.intersection(closes_b.index)
        closes_a = closes_a.loc[common].tail(config.STAT_ARB_LOOKBACK_DAYS)
        closes_b = closes_b.loc[common].tail(config.STAT_ARB_LOOKBACK_DAYS)

        if len(closes_a) < 30:
            raise ValueError(f"insufficient aligned bars: {len(closes_a)}")

        log_a = np.log(closes_a.values)
        log_b = np.log(closes_b.values)
        spread = log_a - log_b

        mu     = spread.mean()
        sigma  = spread.std(ddof=1)
        z      = float((spread[-1] - mu) / sigma) if sigma > 0 else 0.0
        corr   = float(np.corrcoef(log_a, log_b)[0, 1])

        return closes_a, closes_b, z, corr, float(sigma)

    def _daily_atr(self, instrument: str, period: int = 14) -> float:
        end   = _utcnow()
        start = end - timedelta(days=period + 10)
        hist  = oanda_history(self._api, instrument, start, end, "D")
        return atr_scalar(hist, period)

    def _nav_safe(self) -> float:
        from .base import _nav
        return _nav if _nav > 0 else 100_000.0
