# strategies/base.py
"""
SafeguardsBase — shared risk controls wrapped around every strategy.

Shared state (_halted, _daily_pnl, etc.) is class-level and protected by a
threading.Lock so all strategy instances running in the same process share
the same risk counters.

Strategies NEVER call the broker.  They return Signal objects; the runner
calls approve_trade() then submits the order.
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

log = logging.getLogger("safeguards")

# ─── Shared state (module-level, one per process) ─────────────────────────────
_lock               = threading.Lock()
_halted: bool       = False
_daily_pnl: float   = 0.0
_open_pos: int      = 0
_leverage: float    = 0.0
_consec_loss: int   = 0
_nav: float         = 0.0          # refreshed by runner every N seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── Signal ───────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """
    A trade instruction returned by a strategy.
    The runner is the ONLY place that calls the broker API.
    """
    instrument: str
    direction:  int             # +1 long / -1 short
    units:      float           # position size in instrument base units
    stop_price: float           # absolute stop-loss price (0 = market close)
    tp_price:   Optional[float] = None
    strategy:   str             = ""
    meta:       dict            = field(default_factory=dict)


# ─── SafeguardsBase ───────────────────────────────────────────────────────────

class SafeguardsBase:
    """
    Mixin base for all strategy classes.

    Required in subclass:
        strategy_name: str  (class attribute)
    """
    strategy_name: str = "base"

    # ── Approval gate ─────────────────────────────────────────────────────────

    def approve_trade(self, signal: Signal) -> bool:
        """
        Run every safeguard check.  Return True only when ALL pass.
        Any failure is logged and the trade is silently skipped.
        Called by the runner — strategies must not call this themselves;
        instead they return signals and let the runner gate them.
        """
        global _halted, _daily_pnl, _open_pos, _leverage, _consec_loss, _nav
        with _lock:
            blocks: list[str] = []

            if _halted:
                blocks.append("BOT_HALTED")

            if _daily_pnl <= -config.HALT_DAILY_LOSS_USD:
                blocks.append(f"daily_pnl={_daily_pnl:.2f}")

            if _nav > 0:
                dd = abs(min(0.0, _daily_pnl)) / _nav
                if dd >= config.HALT_DRAWDOWN_PCT:
                    blocks.append(f"drawdown={dd:.2%}")

                size_pct = (signal.units * signal.stop_price) / _nav if signal.stop_price else 0
                if size_pct > config.HALT_MAX_TRADE_SIZE_PCT:
                    blocks.append(f"trade_size={size_pct:.2%}")

            if _open_pos >= config.HALT_MAX_OPEN_POSITIONS:
                blocks.append(f"open_positions={_open_pos}")

            if _leverage > config.HALT_MAX_LEVERAGE:
                blocks.append(f"leverage={_leverage:.2f}x")

            if _consec_loss >= config.HALT_CONSECUTIVE_LOSSES:
                blocks.append(f"consecutive_losses={_consec_loss}")

            if _near_session_boundary():
                blocks.append("session_boundary")

            if blocks:
                log.info("[%s] BLOCKED: %s", self.strategy_name, " | ".join(blocks))
                return False

            return True

    # ── State update helpers (called by runner after fills) ───────────────────

    def record_fill(self, realised_pnl: float) -> None:
        global _daily_pnl, _consec_loss, _open_pos
        with _lock:
            _daily_pnl += realised_pnl
            _open_pos   = max(0, _open_pos - 1)
            if realised_pnl < 0:
                _consec_loss += 1
            else:
                _consec_loss = 0

    def on_position_open(self, leverage_contrib: float = 0.0) -> None:
        global _open_pos, _leverage
        with _lock:
            _open_pos += 1
            _leverage  = max(0.0, _leverage + leverage_contrib)

    def on_position_close(self, leverage_contrib: float = 0.0) -> None:
        global _open_pos, _leverage
        with _lock:
            _open_pos = max(0, _open_pos - 1)
            _leverage = max(0.0, _leverage - leverage_contrib)

    @classmethod
    def update_nav(cls, nav: float) -> None:
        global _nav
        with _lock:
            _nav = nav

    # ── Hard stop ─────────────────────────────────────────────────────────────

    def trigger_hard_stop(
        self,
        reason: str,
        positions_closed: list[str],
        nav_at_halt: float = 0.0,
    ) -> None:
        """
        1. Set BOT_HALTED = True
        2. Write HALT_REPORT.json
        3. Append to alerts.log
        4. Bot cannot restart until operator calls clear_halt()
        """
        global _halted
        with _lock:
            _halted = True

        report: dict[str, Any] = {
            "reason":           reason,
            "timestamp":        _utcnow().isoformat(),
            "positions_closed": positions_closed,
            "nav_at_halt":      nav_at_halt,
            "breached_limit":   reason,
        }
        Path(config.HALT_REPORT_PATH).write_text(json.dumps(report, indent=2))

        with Path(config.ALERTS_LOG_PATH).open("a") as fh:
            fh.write(f"[HALT] {_utcnow().isoformat()} | {reason}\n")

        log.critical("HARD STOP: %s | report → %s", reason, config.HALT_REPORT_PATH)

    @classmethod
    def clear_halt(cls, review_notes: str = "") -> None:
        """Operator must call this before the bot can trade again after a halt."""
        global _halted
        with _lock:
            _halted = False
        log.info("Halt cleared. Notes: %s", review_notes)

    @property
    def is_halted(self) -> bool:
        return _halted


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _near_session_boundary() -> bool:
    """True if current UTC time is within HALT_MARKET_BUFFER_MIN of midnight."""
    now = _utcnow().time()
    buf = config.HALT_MARKET_BUFFER_MIN
    return now < dtime(0, buf) or now >= dtime(23, 60 - buf)
