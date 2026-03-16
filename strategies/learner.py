# strategies/learner.py
"""
StrategyLearner — feature-bucketed win-rate analysis.

No heavy ML libraries; uses plain Python dicts to track win rate per
(strategy, feature_bucket) tuple.  Falls back to "allow" when there is
insufficient data.  Blocks an entry only when a specific bucket has
>= BUCKET_MIN samples AND win rate < WIN_RATE_FLOOR.

Feature buckets per strategy
─────────────────────────────
momentum    : RSI band (10-pt) × ATR% tier (low/mid/high) × direction
stat_arb    : pair × leg × |z| band × correlation band
vol_premium : IV/RV ratio band × estimated-VIX band
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("learner")

MIN_SAMPLES    = 20    # need this many total trades before learning kicks in
BUCKET_MIN     = 15    # min samples per bucket before blocking
WIN_RATE_FLOOR = 0.30  # block if bucket win rate < 30%
CACHE_TTL_SECS = 300   # re-read DB every 5 minutes


# ─── Bucket functions ─────────────────────────────────────────────────────────

def _momentum_bucket(features: dict) -> str:
    rsi       = float(features.get("rsi", 50.0))
    atr_pct   = float(features.get("atr_pct", 0.0))
    direction = int(features.get("direction", 0))

    rsi_band = int(rsi // 10) * 10          # 0, 10, 20, … 90

    if atr_pct < 0.005:
        atr_tier = "low"
    elif atr_pct < 0.010:
        atr_tier = "mid"
    else:
        atr_tier = "high"

    dir_str = "long" if direction > 0 else "short"
    return f"rsi{rsi_band}_{atr_tier}_{dir_str}"


def _stat_arb_bucket(features: dict) -> str:
    z    = float(features.get("z", 0.0))
    corr = float(features.get("corr", 0.0))
    pair = str(features.get("pair", "unknown"))
    leg  = str(features.get("leg", "A"))

    z_band   = min(int(abs(z)), 4)          # 0,1,2,3,4+
    corr_band = "high" if corr >= 0.80 else "mid" if corr >= 0.65 else "low"
    return f"{pair}_{leg}_z{z_band}_{corr_band}"


def _vol_premium_bucket(features: dict) -> str:
    ratio = float(features.get("iv_rv_ratio", 1.0))
    vix   = float(features.get("vix_est", 20.0))

    if ratio < 1.3:
        ratio_band = "low"
    elif ratio < 1.5:
        ratio_band = "mid"
    else:
        ratio_band = "high"

    vix_band = "low" if vix < 20 else "mid" if vix < 25 else "high"
    return f"ratio_{ratio_band}_vix_{vix_band}"


def _crypto_bucket(features: dict) -> str:
    rsi       = float(features.get("rsi", 50.0))
    atr_pct   = float(features.get("atr_pct", 0.0))
    direction = int(features.get("direction", 0))

    rsi_band = int(rsi // 10) * 10          # 0, 10, 20, … 90

    if atr_pct < 0.010:
        atr_tier = "low"
    elif atr_pct < 0.025:
        atr_tier = "mid"
    else:
        atr_tier = "high"

    dir_str = "long" if direction > 0 else "short"
    return f"rsi{rsi_band}_{atr_tier}_{dir_str}"


_BUCKET_FNS = {
    "momentum":    _momentum_bucket,
    "stat_arb":    _stat_arb_bucket,
    "vol_premium": _vol_premium_bucket,
    "crypto":      _crypto_bucket,
}


# ─── StrategyLearner ──────────────────────────────────────────────────────────

class StrategyLearner:
    def __init__(self) -> None:
        self._cache:    dict[str, list[dict]] = {}   # strategy_name -> trades
        self._cache_ts: dict[str, float]      = {}   # strategy_name -> last load time

    def evaluate_entry(
        self, strategy_name: str, features: dict[str, Any]
    ) -> tuple[bool, str]:
        """
        Returns (allow: bool, reason: str).
        allow=True  → proceed with entry
        allow=False → skip entry (historically poor bucket performance)
        """
        bucket_fn = _BUCKET_FNS.get(strategy_name)
        if bucket_fn is None:
            return True, "no_learner"

        trades = self._get_trades(strategy_name)
        if len(trades) < MIN_SAMPLES:
            return True, f"insufficient_data({len(trades)}<{MIN_SAMPLES})"

        bucket = bucket_fn(features)

        wins = total = 0
        for t in trades:
            try:
                meta = json.loads(t.get("entry_metadata") or "{}")
            except (ValueError, TypeError):
                continue
            if bucket_fn(meta) != bucket:
                continue
            total += 1
            if float(t.get("raw_pl", 0.0)) > 0:
                wins += 1

        if total < BUCKET_MIN:
            return True, f"bucket_thin({total}<{BUCKET_MIN})"

        win_rate = wins / total
        if win_rate < WIN_RATE_FLOOR:
            log.info(
                "[learner] blocking %s bucket=%s win_rate=%.0f%% (%d/%d)",
                strategy_name, bucket, win_rate * 100, wins, total,
            )
            return False, f"bucket_blocked(win_rate={win_rate:.0%},n={total})"

        return True, f"ok(win_rate={win_rate:.0%},n={total})"

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_trades(self, strategy_name: str) -> list[dict]:
        now = time.monotonic()
        if (now - self._cache_ts.get(strategy_name, 0.0)) < CACHE_TTL_SECS:
            return self._cache.get(strategy_name, [])

        try:
            from database.database import get_trades_for_learner
            trades = get_trades_for_learner(strategy_name)
        except Exception:
            log.warning("[learner] DB read failed for %s", strategy_name, exc_info=True)
            trades = self._cache.get(strategy_name, [])

        self._cache[strategy_name]    = trades
        self._cache_ts[strategy_name] = now
        return trades


# ── Module-level singleton ────────────────────────────────────────────────────

_learner: StrategyLearner | None = None


def get_learner() -> StrategyLearner:
    global _learner
    if _learner is None:
        _learner = StrategyLearner()
    return _learner
