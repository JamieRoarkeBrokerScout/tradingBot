#!/usr/bin/env python3
"""
Live momentum trader for Oanda using tpqoa / v20.

Features:
- Volatility-normalized momentum signal on mid prices.
- Index vs FX awareness (pip_size, spread units).
- Per-trade SL/TP in "points" for indices/CFDs, "pips" for FX.
- Basic risk controls: max trades/day, daily loss/gain stops.
- Optional session filter and regime (volatility) filter.
- Tick CSV export and trade CSV logging.
- Panic flatten on Ctrl-C.
- Adaptive threshold_k after loss streaks.
- R-based trailing stop that constantly locks in profit.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import tpqoa
import v20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_bar_length(bar_length: str) -> pd.Timedelta:
    """
    Convert bar_length like "1min", "3min", "5min", "10s" into a pandas Timedelta.
    """
    bar_length = bar_length.strip().lower()
    if bar_length.endswith("min"):
        n = int(bar_length.replace("min", ""))
        return pd.Timedelta(minutes=n)
    if bar_length.endswith("s"):
        n = int(bar_length.replace("s", ""))
        return pd.Timedelta(seconds=n)
    raise ValueError(f"Unsupported bar_length: {bar_length}")


def floor_time_to_bar(ts: pd.Timestamp, bar_td: pd.Timedelta) -> pd.Timestamp:
    """
    Floor timestamp to bar boundary given bar length.
    Works with tz-aware Timestamps.
    """
    delta_ns = bar_td.value
    t_ns = ts.value
    floored = (t_ns // delta_ns) * delta_ns
    return pd.Timestamp(floored, tz=ts.tz)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


@dataclass
class TradeState:
    """
    Tracks the current open position & parameters.
    """

    position: int = 0  # +1 long, -1 short, 0 flat
    entry_units: int = 0
    entry_price: Optional[float] = None
    entry_time: Optional[datetime] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    max_fav_excursion: float = 0.0  # in index points/pips
    max_adv_excursion: float = 0.0  # worst adverse move
    moved_to_BE: bool = False
    partial_taken: bool = False


# ---------------------------------------------------------------------------
# Momentum Trader
# ---------------------------------------------------------------------------

class MomentumTraderLive:
    """
    Live momentum trader on Oanda streaming prices.
    """

    def __init__(
        self,
        api: tpqoa.tpqoa,
        instrument: str,
        bar_length: str = "1min",
        momentum: int = 6,
        units: int = 3,
        max_position_units: int = 3,
        threshold_k: float = 0.6,
        max_spread_pips: float = 2.0,
        per_trade_sl: float = 10.0,
        per_trade_tp: float = 30.0,
        max_trades_per_day: int = 200,
        max_daily_loss: float = 300.0,
        max_daily_gain: float = 400.0,
        use_regime_filter: bool = False,
        regime_lookback: int = 48,
        regime_vol_min: float = 0.0,
        use_session_filter: bool = False,
        session_start_hour: int = 13,
        session_end_hour: int = 21,
        export_ticks: bool = True,
        min_flip_strength: float = 1.5,
        trailing_mode: str = "R_trailing",
        verbose_ticks: bool = False,
        verbose_heartbeats: bool = False,
        heartbeat_log_every: int = 20,
        data_dir: Path = Path("data"),
        logs_dir: Path = Path("logs"),
        status_log_interval: float = 30.0,
    ) -> None:
        # Oanda API (already configured via tpqoa)
        self.api = api

        self.instrument = instrument
        self.bar_length_str = bar_length
        self.bar_td = parse_bar_length(bar_length)
        self.momentum = momentum
        self.units = units
        self.max_position_units = max_position_units

        # dynamic threshold: base + adaptive
        self.base_threshold_k = threshold_k
        self.threshold_k = threshold_k
        self.consec_losses: int = 0

        self.max_spread_pips = max_spread_pips
        self.per_trade_sl = per_trade_sl
        self.per_trade_tp = per_trade_tp
        self.max_trades_per_day = max_trades_per_day
        self.max_daily_loss = max_daily_loss
        self.max_daily_gain = max_daily_gain
        self.use_regime_filter = use_regime_filter
        self.regime_lookback = regime_lookback
        self.regime_vol_min = regime_vol_min
        self.use_session_filter = use_session_filter
        self.session_start_hour = session_start_hour
        self.session_end_hour = session_end_hour
        self.export_ticks = export_ticks
        self.min_flip_strength = min_flip_strength
        self.trailing_mode = trailing_mode
        self.verbose_ticks = verbose_ticks
        self.verbose_heartbeats = verbose_heartbeats
        self.heartbeat_log_every = heartbeat_log_every

        # status / diagnostics
        self.tick_count = 0
        self.last_tick_mid: Optional[float] = None
        self.last_tick_spread: Optional[float] = None
        self.last_hb_time: Optional[str] = None  # RFC3339 str from Oanda
        self._last_status_log_ts: float = time.monotonic()
        self.status_log_interval: float = status_log_interval

        self.data_dir = data_dir
        self.logs_dir = logs_dir
        ensure_dir(self.data_dir / "ticks")
        ensure_dir(self.data_dir / "trades")
        ensure_dir(self.logs_dir)

        # pip sizing / instrument type
        index_like_prefixes = (
            "SPX500_",
            "US500_",
            "NAS100_",
            "US30_",
            "DE30_",
            "UK100_",
            "JP225_",
            "XAU_",
            "XAG_",
            "XPT_",
            "XPD_",
            "WTICO_",
            "BCO_",
        )

        if instrument.endswith("_JPY"):
            # JPY FX: 0.01
            self.pip_size = 0.01
        elif instrument.startswith(index_like_prefixes):
            # Indices/CFDs/metals/energies: treat 1.0 as "point"
            self.pip_size = 1.0
        else:
            # Regular FX: 0.0001
            self.pip_size = 0.0001

        # FX vs index / CFD flag (used only for labels & spread units)
        self.is_fx = ("_" in self.instrument) and not self.instrument.startswith(
            index_like_prefixes
        )

        if self.is_fx:
            print(
                f"[meta] {instrument}: FX, pip_size={self.pip_size}, "
                f"SL={per_trade_sl} pips, TP={per_trade_tp} pips",
                flush=True,
            )
        else:
            print(
                f"[meta] {instrument}: INDEX/CFD, pip_size={self.pip_size}, "
                f"SL={per_trade_sl} pts, TP={per_trade_tp} pts",
                flush=True,
            )

        # state
        self.trade_state = TradeState()
        self.daily_pnl: float = 0.0
        self.trades_today: int = 0
        self.current_date: Optional[datetime.date] = None

        # bar construction
        self.current_bar_start: Optional[pd.Timestamp] = None
        nobody = None
        self.current_bar_open: Optional[float] = None
        self.current_bar_high: Optional[float] = None
        self.current_bar_low: Optional[float] = None
        self.current_bar_close: Optional[float] = None

        # store history
        self.closes: Deque[float] = deque(maxlen=1000)
        self.returns: Deque[float] = deque(maxlen=1000)

        # tick CSV handler
        self.tick_file_path: Optional[Path] = None
        self.tick_csv_writer: Optional[csv.writer] = None
        self.tick_file_handle = None

        # trade CSV path
        self.trades_file_path: Optional[Path] = None

        # debug log file (not heavily used)
        self.log_file_path: Path = self.logs_dir / f"live_{self.instrument}.log"

    # ------------------------------------------------------------------
    # Logging & file helpers
    # ---------------------------------------------------------------------------

    def _open_tick_file(self, day: datetime.date) -> None:
        if not self.export_ticks:
            return
        if self.tick_file_handle is not None and self.tick_file_path is not None:
            if str(day) in self.tick_file_path.name:
                return
            self.tick_file_handle.close()
            self.tick_file_handle = None

        fname = f"ticks_{self.instrument}_{day.isoformat()}.csv"
        path = self.data_dir / "ticks" / fname
        make_header = not path.exists()
        self.tick_file_handle = path.open("a", newline="")
        self.tick_csv_writer = csv.writer(self.tick_file_handle)
        if make_header:
            self.tick_csv_writer.writerow(["time", "bid", "ask", "mid"])
        self.tick_file_path = path
        print(f"[ticks] logging to {path}", flush=True)

    def _write_tick(self, ts: datetime, bid: float, ask: float, mid: float) -> None:
        if not self.export_ticks:
            return
        day = ts.date()
        self._open_tick_file(day)
        if self.tick_csv_writer is not None:
            self.tick_csv_writer.writerow(
                [ts.isoformat(), f"{bid:.5f}", f"{ask:.5f}", f"{mid:.5f}"]
            )
            self.tick_file_handle.flush()

    def _ensure_trades_file(self, day: datetime.date) -> None:
        fname = f"trades_{self.instrument}_{day.isoformat()}.csv"
        path = self.data_dir / "trades" / fname
        if not path.exists():
            with path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(
                    [
                        "entry_time",
                        "exit_time",
                        "instrument",
                        "direction",
                        "entry_units",
                        "entry_price",
                        "exit_price",
                        "exit_reason",
                        "pl_points",
                        "pl_R",
                        "raw_pl",
                        "bar_length",
                        "momentum",
                        "threshold_k",
                        "per_trade_sl",
                        "per_trade_tp",
                        "trailing_mode",
                    ]
                )
        self.trades_file_path = path

    def _log_trade(
        self,
        exit_time: datetime,
        exit_price: float,
        exit_reason: str,
        realized_pl: float,
        pl_R: float,
        pl_points: float,
    ) -> None:
        """
        Log the *closed* trade as a single row with entry & exit info.
        """
        ts = self.trade_state
        if ts.entry_time is None or ts.entry_price is None or ts.position == 0:
            return

        day = exit_time.date()
        self._ensure_trades_file(day)

        direction = ts.position

        row = [
            ts.entry_time.isoformat(),
            exit_time.isoformat(),
            self.instrument,
            direction,
            ts.entry_units,
            f"{ts.entry_price:.5f}",
            f"{exit_price:.5f}",
            exit_reason,
            f"{pl_points:.5f}",
            f"{pl_R:.5f}",
            f"{realized_pl:.2f}",
            self.bar_length_str,
            self.momentum,
            self.threshold_k,
            self.per_trade_sl,
            self.per_trade_tp,
            self.trailing_mode,
        ]
        with self.trades_file_path.open("a", newline="") as f:
            csv.writer(f).writerow(row)

    def _reset_trade_state(self) -> None:
        self.trade_state = TradeState()

    # ------------------------------------------------------------------
    # Risk / filters
    # ---------------------------------------------------------------------------

    def _update_daily_state(self, now: datetime) -> None:
        if self.current_date is None or now.date() != self.current_date:
            print(
                f"[daily] new trading day {now.date()} "
                f"(pnl reset; trades_today reset; threshold_k reset)",
                flush=True,
            )
            self.current_date = now.date()
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.consec_losses = 0
            self.threshold_k = self.base_threshold_k

    def _within_session(self, now: datetime) -> bool:
        if not self.use_session_filter:
            return True
        h = now.hour
        return self.session_start_hour <= h < self.session_end_hour

    def _passes_spread_filter(self, bid: float, ask: float) -> bool:
        raw_spread = ask - bid
        if raw_spread <= 0:
            return False
        spread_pips = raw_spread / self.pip_size
        if spread_pips > self.max_spread_pips:
            unit_label = "pips" if self.is_fx else "pts"
            print(
                f"[filter] spread={spread_pips:.2f} {unit_label} "
                f"(raw={raw_spread:.5f}) > {self.max_spread_pips}, skip",
                flush=True,
            )
            return False
        return True

    def _passes_risk_limits(self) -> bool:
        if self.trades_today >= self.max_trades_per_day:
            print(f"[risk] max_trades_per_day reached: {self.trades_today}", flush=True)
            return False
        if self.daily_pnl <= -abs(self.max_daily_loss):
            print(f"[risk] max_daily_loss reached: {self.daily_pnl:.2f}", flush=True)
            return False
        if self.daily_pnl >= abs(self.max_daily_gain):
            print(f"[risk] max_daily_gain reached: {self.daily_pnl:.2f}", flush=True)
            return False
        return True

    # ------------------------------------------------------------------
    # Bar building & signal
    # ---------------------------------------------------------------------------

    def _on_new_bar(self, ts: pd.Timestamp, close_price: float) -> None:
        """
        Called whenever a bar completes with its closing price.
        """
        if len(self.closes) > 0:
            prev_close = self.closes[-1]
            ret = (close_price / prev_close) - 1.0
            self.returns.append(ret)
        else:
            ret = 0.0

        self.closes.append(close_price)

        # Warmup phase: need at least `momentum` returns
        if len(self.returns) < self.momentum:
            print(
                f"[bar] {ts} mid={close_price:.5f} "
                f"(warmup {len(self.returns)}/{self.momentum} returns)",
                flush=True,
            )
            return

        rets = np.array(list(self.returns)[-self.momentum:], dtype=float)
        mom_raw = rets.sum()
        vol = float(np.std(self.returns)) if len(self.returns) > 1 else 0.0
        if vol <= 0:
            print(
                f"[bar] {ts} mid={close_price:.5f} "
                f"ret={ret:+.5f} mom_raw={mom_raw:+.5f} vol={vol:.5f} "
                f"(zero vol, skip)",
                flush=True,
            )
            return

        sig = mom_raw / vol
        thr = self.threshold_k

        pattern = "NEUTRAL"
        if sig > thr:
            pattern = "BULL"
        elif sig < -thr:
            pattern = "BEAR"

        print(
            f"[bar] {ts} mid={close_price:.5f} "
            f"ret={ret:+.5f} mom_raw={mom_raw:+.5f} vol={vol:.5f} "
            f"thr={thr:.5f} sig={sig:+.2f} pattern={pattern}",
            flush=True,
        )

        now = ts.to_pydatetime()

        if self.use_regime_filter and len(self.returns) >= self.regime_lookback:
            rets_reg = np.array(
                list(self.returns)[-self.regime_lookback:], dtype=float
            )
            vol_reg = float(np.std(rets_reg))
            if vol_reg < self.regime_vol_min:
                print(
                    f"[regime] vol_reg={vol_reg:.5f} < {self.regime_vol_min:.5f}, skip",
                    flush=True,
                )
                return

        self._update_daily_state(now)
        if not self._within_session(now):
            print("[session] outside trading hours, skip signal", flush=True)
            return
        if not self._passes_risk_limits():
            print("[risk] risk limits breached, skip signal", flush=True)
            return

        self._handle_signal(sig, close_price, now, pattern)

    # ------------------------------------------------------------------
    # Trading logic
    # ---------------------------------------------------------------------------

    def _handle_signal(
        self, sig: float, price: float, now: datetime, pattern: str
    ) -> None:
        ts_state = self.trade_state

        # flat → open
        if ts_state.position == 0:
            if sig > self.threshold_k:
                self._open_position(
                    direction=1, price=price, now=now, pattern=f"LONG_{pattern}"
                )
            elif sig < -self.threshold_k:
                self._open_position(
                    direction=-1, price=price, now=now, pattern=f"SHORT_{pattern}"
                )
            return

        # manage existing trade
        self._manage_open_trade(price, now)

        # flip conditions
        if ts_state.position == 1 and sig < -self.threshold_k * self.min_flip_strength:
            print(
                f"[flip] long→short sig={sig:+.2f} < -thr*{self.min_flip_strength}",
                flush=True,
            )
            self._close_and_reverse(
                new_dir=-1, price=price, now=now, new_pattern=f"SHORT_{pattern}"
            )
        elif ts_state.position == -1 and sig > self.threshold_k * self.min_flip_strength:
            print(
                f"[flip] short→long sig={sig:+.2f} > thr*{self.min_flip_strength}",
                flush=True,
            )
            self._close_and_reverse(
                new_dir=1, price=price, now=now, new_pattern=f"LONG_{pattern}"
            )

    def _compute_sl_tp(self, direction: int, entry_price: float) -> Tuple[float, float]:
        """
        Compute SL / TP absolute prices given an entry.
        For indices/CFDs, per_trade_sl/tp are in "points".
        For FX, in "pips" (pip_size applied).
        """
        if self.is_fx:
            sl_dist = self.per_trade_sl * self.pip_size
            tp_dist = self.per_trade_tp * self.pip_size
        else:
            sl_dist = self.per_trade_sl * self.pip_size
            tp_dist = self.per_trade_tp * self.pip_size

        if direction > 0:
            sl = entry_price - sl_dist
            tp = entry_price + tp_dist
        else:
            sl = entry_price + sl_dist
            tp = entry_price - tp_dist

        return sl, tp

    def _open_position(
        self,
        direction: int,
        price: float,
        now: datetime,
        pattern: str,
    ) -> None:
        """
        Open a new market position.
        """
        if self.trade_state.position != 0:
            print("[open] attempting to open but already in a position, skip", flush=True)
            return

        units = self.units * direction
        if abs(units) > self.max_position_units:
            print(
                f"[open] requested units={units} > max_position_units={self.max_position_units}, skip",
                flush=True,
            )
            return

        print(
            f"[open] {self.instrument} direction={direction} units={units} "
            f"price≈{price:.5f} pattern={pattern}",
            flush=True,
        )

        try:
            res = self.api.create_order(
                self.instrument,
                units,
                suppress=True,
                ret=True,
            )
        except Exception as e:
            print(f"[open] order failed: {e}", flush=True)
            return

        res_str = str(res).lower()
        if "reject" in res_str:
            print(f"[open] order rejected by broker: {res}", flush=True)
            return

        exec_price = price
        try:
            if res.get("price") is not None:
                exec_price = float(res["price"])
        except Exception:
            pass

        sl, tp = self._compute_sl_tp(direction, exec_price)

        self.trade_state = TradeState(
            position=direction,
            entry_units=units,
            entry_price=exec_price,
            entry_time=now,
            sl_price=sl,
            tp_price=tp,
            max_fav_excursion=0.0,
            max_adv_excursion=0.0,
        )
        print(
            f"[open] filled at {exec_price:.5f}, SL={sl:.5f}, TP={tp:.5f}",
            flush=True,
        )

    def _close_position(self, price: float, now: datetime, reason: str) -> None:
        """
        Close current position via market order and log trade.
        """
        ts = self.trade_state
        if ts.position == 0 or ts.entry_units == 0 or ts.entry_price is None:
            print("[close] no open position to close", flush=True)
            return

        close_units = -ts.entry_units
        print(
            f"[close] {reason}: market close {self.instrument} "
            f"units={close_units} price≈{price:.5f}",
            flush=True,
        )

        try:
            res = self.api.create_order(
                self.instrument,
                close_units,
                suppress=True,
                ret=True,
            )
        except Exception as e:
            print(f"[close] order failed: {e}", flush=True)
            return

        res_str = str(res).lower()
        if "reject" in res_str:
            print(f"[close] order rejected by broker: {res}", flush=True)
            return

        exec_price = price
        pl_close = 0.0
        try:
            if res.get("price") is not None:
                exec_price = float(res["price"])
        except Exception:
            pass
        try:
            if res.get("pl") is not None:
                pl_close = float(res["pl"])
        except Exception:
            pass

        # compute PL in R for adaptive logic and logging
        direction = ts.position
        pl_points = (exec_price - ts.entry_price) * direction
        pl_R = pl_points / self.per_trade_sl if self.per_trade_sl > 0 else math.nan

        exit_dt = now
        try:
            self._log_trade(
                exit_time=exit_dt,
                exit_price=exec_price,
                exit_reason=reason,
                realized_pl=pl_close,
                pl_R=pl_R,
                pl_points=pl_points,
            )
        except Exception as e:
            print(f"[close] failed to log trade: {e}", flush=True)

        self.daily_pnl += pl_close
        self.trades_today += 1

        # ---- Adaptive threshold_k: bump after 3 consecutive losing trades ----
        if not math.isnan(pl_R):
            if pl_R < 0:
                self.consec_losses += 1
            else:
                self.consec_losses = 0

            # increase threshold_k after 3 consecutive losses
            if (
                self.consec_losses >= 3
                and self.threshold_k == self.base_threshold_k
            ):
                old_thr = self.threshold_k
                # bump: +30% of base (tweak this factor if you like)
                self.threshold_k = self.base_threshold_k * 1.3
                print(
                    f"[adapt] {self.consec_losses} consecutive losses → "
                    f"increasing threshold_k from {old_thr:.2f} to {self.threshold_k:.2f}",
                    flush=True,
                )

            # if we have a strong winner (>= 2R) while elevated, reset back to base
            if pl_R >= 2.0 and self.threshold_k > self.base_threshold_k:
                old_thr = self.threshold_k
                self.threshold_k = self.base_threshold_k
                self.consec_losses = 0
                print(
                    f"[adapt] strong winner {pl_R:.2f}R → "
                    f"resetting threshold_k from {old_thr:.2f} to {self.threshold_k:.2f} "
                    f"and clearing loss streak.",
                    flush=True,
                )

        print(
            f"[close] closed at {exec_price:.5f}, pl={pl_close:.2f}, "
            f"pl_R={pl_R:.2f}, daily_pnl={self.daily_pnl:.2f}, "
            f"trades_today={self.trades_today}",
            flush=True,
        )

        self._reset_trade_state()

    def _close_and_reverse(
        self, new_dir: int, price: float, now: datetime, new_pattern: str
    ) -> None:
        """
        Close current position, then open new one in opposite direction
        (subject to risk limits).
        """
        self._close_position(price, now, reason="FLIP")
        if self._passes_risk_limits():
            self._open_position(new_dir, price, now, pattern=new_pattern)

    def _manage_open_trade(self, price: float, now: datetime) -> None:
        """
        Manage open trade: SL/TP & trailing logic.
        """
        ts = self.trade_state
        if ts.position == 0 or ts.entry_price is None:
            return

        direction = ts.position
        dist = (price - ts.entry_price) * direction
        ts.max_fav_excursion = max(ts.max_fav_excursion, dist)
        ts.max_adv_excursion = min(ts.max_adv_excursion, dist)

        # hard SL/TP
        if ts.sl_price is not None:
            if (direction > 0 and price <= ts.sl_price) or (
                direction < 0 and price >= ts.sl_price
            ):
                print("[manage] SL hit", flush=True)
                self._close_position(price, now, reason="SL")
                return
        if ts.tp_price is not None:
            if (direction > 0 and price >= ts.tp_price) or (
                direction < 0 and price <= ts.tp_price
            ):
                print("[manage] TP hit", flush=True)
                self._close_position(price, now, reason="TP")
                return

        R = self.per_trade_sl
        if self.trailing_mode == "none" or R <= 0:
            return

        # --- Classic "R behind price" trailing: always keep SL R points behind ---
        if self.trailing_mode == "R_trailing":
            # only trail if in profit
            if dist <= 0:
                return

            if direction > 0:
                # Long: SL lags current price by R, but never moves down
                new_sl = price - R
                if ts.sl_price is None:
                    ts.sl_price = new_sl
                    print(
                        f"[trail] init R_trailing SL at {ts.sl_price:.2f} "
                        f"(price={price:.2f}, R={R:.2f})",
                        flush=True,
                    )
                else:
                    if new_sl > ts.sl_price:
                        ts.sl_price = new_sl
                        print(
                            f"[trail] R_trailing long: moved SL to {ts.sl_price:.2f} "
                            f"(price={price:.2f}, dist={dist:.2f}, R={R:.2f})",
                            flush=True,
                        )
            else:
                # Short: SL lags current price by R above, but never moves up (for short)
                new_sl = price + R
                if ts.sl_price is None:
                    ts.sl_price = new_sl
                    print(
                        f"[trail] init R_trailing SL at {ts.sl_price:.2f} "
                        f"(price={price:.2f}, R={R:.2f})",
                        flush=True,
                    )
                else:
                    if new_sl < ts.sl_price:
                        ts.sl_price = new_sl
                        print(
                            f"[trail] R_trailing short: moved SL to {ts.sl_price:.2f} "
                            f"(price={price:.2f}, dist={dist:.2f}, R={R:.2f})",
                            flush=True,
                        )
            return

        # --- Existing BE_after_1R / halfR_-0.5R_BE_1R logic (unchanged) ---
        if self.trailing_mode in ("BE_after_1R", "halfR_-0.5R_BE_1R"):
            # move to BE once price reaches +1R
            if not ts.moved_to_BE and dist >= R:
                ts.sl_price = ts.entry_price
                ts.moved_to_BE = True
                print(
                    f"[trail] moved SL to BE at dist={dist:.2f} (>= 1R={R:.2f})",
                    flush=True,
                )

            # before BE, optionally keep a -0.5R hard stop instead of full -1R
            if self.trailing_mode == "halfR_-0.5R_BE_1R" and not ts.moved_to_BE:
                halfR = 0.5 * R
                if dist < R:
                    if direction > 0:
                        ts.sl_price = ts.entry_price - halfR
                    else:
                        ts.sl_price = ts.entry_price + halfR

    # ------------------------------------------------------------------
    # Status & panic flatten
    # ---------------------------------------------------------------------------

    def _log_status(self) -> None:
        mid = self.last_tick_mid
        spread = self.last_tick_spread
        hb = self.last_hb_time

        parts = [f"[status] ticks={self.tick_count}"]
        if mid is not None:
            parts.append(f"mid={mid:.2f}")
        if spread is not None:
            unit_label = "pips" if self.is_fx else "pts"
            parts.append(f"spread={spread:.2f} {unit_label}")
        if hb is not None:
            parts.append(f"last_hb={hb}")

        print(" ".join(parts), flush=True)

    def panic_flatten_instrument(self, reason: str = "PANIC") -> None:
        """
        Best-effort flatten of the currently tracked position in this instrument.
        """
        ts = self.trade_state
        if ts.entry_units == 0 or ts.position == 0 or ts.entry_price is None:
            print("[panic] no tracked open position to flatten.", flush=True)
            return

        close_units = -ts.entry_units
        print(
            f"[panic] attempting to flatten {self.instrument}: "
            f"entry_units={ts.entry_units}, close_units={close_units}",
            flush=True,
        )

        try:
            res = self.api.create_order(
                self.instrument,
                close_units,
                ret=True,
                suppress=True,
            )
        except Exception as e:
            print(f"[panic] flatten order failed: {e}", flush=True)
            return

        print(f"[panic] flatten response: {res}", flush=True)
        res_str = str(res).lower()
        if "reject" in res_str:
            print(f"[panic] flatten rejected by broker: {res}", flush=True)
            return

        pl_close = 0.0
        price = ts.entry_price
        try:
            pl_close = float(res.get("pl", 0.0))
        except Exception:
            pass
        try:
            if res.get("price") is not None:
                price = float(res.get("price"))
        except Exception:
            pass

        exit_dt = datetime.now(timezone.utc)
        try:
            # For panic flatten, just log pl_R based on final exit versus entry
            direction = ts.position
            pl_points = (price - ts.entry_price) * direction
            pl_R = pl_points / self.per_trade_sl if self.per_trade_sl > 0 else math.nan
            self._log_trade(
                exit_time=exit_dt,
                exit_price=price,
                exit_reason=reason,
                realized_pl=pl_close,
                pl_R=pl_R,
                pl_points=pl_points,
            )
        except Exception as e:
            print(f"[panic] failed to log panic flatten trade: {e}", flush=True)

        self.daily_pnl += pl_close
        self.trades_today += 1

        print(
            f"[panic] flattened {self.instrument}: pl={pl_close:.2f}, "
            f"daily_pnl={self.daily_pnl:.2f}, trades_today={self.trades_today}",
            flush=True,
        )

        self._reset_trade_state()

    # ------------------------------------------------------------------
    # Streaming loop
    # ---------------------------------------------------------------------------

    def run(self) -> None:
        """
        Main streaming loop: subscribe to pricing stream with v20 and
        feed ticks into bar builder and strategy.
        """
        # Extract credentials from tpqoa instance (already configured)
        access_token = getattr(self.api, "access_token", None)
        account_id = getattr(self.api, "account_id", None)
        environment = getattr(self.api, "environment", None)

        cfg = getattr(self.api, "config", None)
        if cfg is not None and (access_token is None or account_id is None or environment is None):
            try:
                section = cfg["oanda"]
                if access_token is None:
                    access_token = section["access_token"]
                if account_id is None:
                    account_id = section["account_id"]
                if environment is None:
                    environment = section.get("environment", "practice")
            except Exception as e:
                print(f"[run] failed to read credentials from config: {e}", flush=True)

        if access_token is None or account_id is None or environment is None:
            print(
                "[run] Missing Oanda credentials (access_token/account_id/environment). "
                "Check your pyalgo.cfg / tpqoa config.",
                flush=True,
            )
            sys.exit(1)

        env_name = str(environment).lower()
        if "practice" in env_name or "demo" in env_name:
            api_domain = "practice"
            stream_host = "stream-fxpractice.oanda.com"
        else:
            api_domain = "live"
            stream_host = "stream-fxtrade.oanda.com"

        client = v20.Context(
            stream_host,
            "443",
            token=access_token,
            datetime_format="RFC3339",
        )

        print(
            f"[stream] connecting pricing stream for {self.instrument} "
            f"on account {account_id} ({api_domain})...",
            flush=True,
        )

        params = {"instruments": self.instrument, "snapshot": True}
        self._last_status_log_ts = time.monotonic()

        while True:
            try:
                rsp = client.pricing.stream(accountID=account_id, **params)
                print("[stream] stream object created, entering loop...", flush=True)

                hb_count = 0

                for msg_type, msg in rsp.parts():
                    now_mono = time.monotonic()

                    # classify messages by attributes, not msg_type string
                    if hasattr(msg, "bids") and hasattr(msg, "asks"):
                        self._on_price_message(msg)
                    else:
                        if hasattr(msg, "time"):
                            self.last_hb_time = msg.time
                            hb_count += 1
                            if (
                                self.verbose_heartbeats
                                and hb_count % self.heartbeat_log_every == 0
                            ):
                                print(f"[hb] heartbeat at {msg.time}", flush=True)

                    # periodic status
                    if now_mono - self._last_status_log_ts >= self.status_log_interval:
                        self._log_status()
                        self._last_status_log_ts = now_mono

            except KeyboardInterrupt:
                now = datetime.now(timezone.utc)
                print(
                    "[main] Ctrl-C received. Attempting to flatten position...",
                    flush=True,
                )
                try:
                    self.panic_flatten_instrument(reason="CTRL_C")
                except Exception as e:
                    print(f"[main] panic flatten failed: {e}", flush=True)
                print("[main] exiting cleanly after Ctrl-C.", flush=True)
                break
            except Exception as e:
                print(f"[main] unexpected error in stream: {e}", flush=True)
                time.sleep(5.0)
                print("[main] reconnecting pricing stream...", flush=True)

        if self.tick_file_handle is not None:
            self.tick_file_handle.close()
            self.tick_file_handle = None

    def _on_price_message(self, msg: v20.pricing.Price) -> None:
        """
        Handler for streaming price messages:
        - updates tick counters & diagnostics
        - writes ticks (if export enabled)
        - applies spread filter
        - updates bars and possibly signals
        """
        ts = pd.Timestamp(msg.time).tz_convert("UTC")

        bids = msg.bids
        asks = msg.asks
        if not bids or not asks:
            return

        bid = float(bids[0].price)
        ask = float(asks[0].price)
        mid = 0.5 * (bid + ask)

        if not np.isfinite(mid) or mid <= 0:
            return

        # tick diagnostics
        self.tick_count += 1
        self.last_tick_mid = mid
        self.last_tick_spread = ask - bid

        if self.tick_count == 1:
            unit_label = "pips" if self.is_fx else "pts"
            print(
                f"[tick] first price at {msg.time}: "
                f"bid={bid:.5f} ask={ask:.5f} mid={mid:.5f} "
                f"spread={self.last_tick_spread:.5f} {unit_label}",
                flush=True,
            )

        if self.verbose_ticks and self.tick_count % 1000 == 0:
            unit_label = "pips" if self.is_fx else "pts"
            print(
                f"[tick] #{self.tick_count} mid={mid:.5f} "
                f"spread={self.last_tick_spread:.5f} {unit_label}",
                flush=True,
            )

        # log tick to CSV
        self._write_tick(ts.to_pydatetime(), bid, ask, mid)

        # spread filter
        if not self._passes_spread_filter(bid, ask):
            return

        # bar building
        self._update_bars(ts, mid)

    def _update_bars(self, ts: pd.Timestamp, mid: float) -> None:
        """
        Update current bar with new tick & detect completed bars.
        """
        if self.current_bar_start is None:
            self.current_bar_start = floor_time_to_bar(ts, self.bar_td)
            self.current_bar_open = mid
            self.current_bar_high = mid
            self.current_bar_low = mid
            self.current_bar_close = mid
            print(
                f"[bar] starting first bar at {self.current_bar_start} mid={mid:.5f}",
                flush=True,
            )
            return

        bar_start = floor_time_to_bar(ts, self.bar_td)
        if bar_start == self.current_bar_start:
            self.current_bar_high = max(self.current_bar_high, mid)
            self.current_bar_low = min(self.current_bar_low, mid)
            self.current_bar_close = mid
        else:
            close_ts = self.current_bar_start + self.bar_td
            if self.current_bar_close is not None:
                self._on_new_bar(close_ts, self.current_bar_close)

            self.current_bar_start = bar_start
            self.current_bar_open = mid
            self.current_bar_high = mid
            self.current_bar_low = mid
            self.current_bar_close = mid
            print(
                f"[bar] new bar started at {self.current_bar_start} mid={mid:.5f}",
                flush=True,
            )


# ---------------------------------------------------------------------------
# CLI / Profiles
# ---------------------------------------------------------------------------

def build_profile_args(profile: str) -> Dict:
    """
    Convenience profile presets so CLI can be short.
    """
    profile = profile.lower()

    if profile == "nas_a":
        # NAS100_USD profile (3min)
        return dict(
            instrument="NAS100_USD",
            bar_length="3min",
            momentum=6,
            threshold_k=1.8,          # bumped base threshold a bit
            units=1,
            max_position_units=1,
            per_trade_sl=20.0,
            per_trade_tp=60.0,
            max_spread_pips=2.0,
            trailing_mode="R_trailing",
            use_session_filter=True,
            session_start_hour=13,
            session_end_hour=21,
            export_ticks=True,
            min_flip_strength=2.0,
        )

    if profile == "xag_a":
        # XAG_USD profile (1min)
        return dict(
            instrument="XAG_USD",
            bar_length="1min",
            momentum=6,
            threshold_k=1.6,          # slightly stricter for XAG
            units=400,
            max_position_units=400,
            per_trade_sl=0.4,
            per_trade_tp=0.8,
            max_spread_pips=3.0,
            trailing_mode="R_trailing",
            use_session_filter=True,
            session_start_hour=13,
            session_end_hour=21,
            export_ticks=True,
            min_flip_strength=2.0,
        )
    

    if profile == "xcu_a":
        # Copper (XCU_USD) profile (3min, conservative sizing)
        return dict(
            instrument="XCU_USD",   # Oanda copper CFD
            bar_length="3min",
            momentum=6,
            threshold_k=1.4,        # similar selectivity to XAG
            units=10,               # start small, you can bump later
            max_position_units=20,
            # NOTE: XCU_USD is treated as FX in this script (pip_size=0.0001),
            # so these are in *pips*:
            # 150 pips = 0.015, 450 pips = 0.045
            per_trade_sl=150.0,
            per_trade_tp=450.0,
            max_spread_pips=40.0,   # generous to avoid filter spam early
            trailing_mode="R_trailing",
            use_session_filter=True,
            session_start_hour=13,
            session_end_hour=21,
            export_ticks=True,
            min_flip_strength=1.8,
        )
    

    return {}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live momentum trader (Oanda).")

    p.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Named profile (e.g. nas_a, xag_a) that sets instrument & core params.",
    )

    p.add_argument("--instrument", type=str, default="SPX500_USD")
    p.add_argument("--bar-length", type=str, default="1min")
    p.add_argument("--momentum", type=int, default=6)
    p.add_argument("--units", type=int, default=3)
    p.add_argument("--max-position-units", type=int, default=3)
    p.add_argument("--threshold-k", type=float, default=0.6)
    p.add_argument("--max-spread-pips", type=float, default=2.0)

    p.add_argument("--per-trade-sl", type=float, default=10.0)
    p.add_argument("--per-trade-tp", type=float, default=30.0)

    p.add_argument("--max-trades-per-day", type=int, default=200)
    p.add_argument("--max-daily-loss", type=float, default=300.0)
    p.add_argument(
        "--max-daily-gain",
        type=float,
        default=400.0,
        help="Max daily profit in account currency before stopping new trades.",
    )

    p.add_argument("--use-regime-filter", action="store_true")
    p.add_argument("--regime-lookback", type=int, default=48)
    p.add_argument("--regime-vol-min", type=float, default=0.0)

    p.add_argument("--use-session-filter", action="store_true")
    p.add_argument("--session-start-hour", type=int, default=13)
    p.add_argument("--session-end-hour", type=int, default=21)

    p.add_argument(
        "--no-export-ticks",
        action="store_true",
        help="Disable tick CSV export (data/ticks).",
    )

    p.add_argument(
        "--min-flip-strength",
        type=float,
        default=1.5,
        help="Multiple of vol-threshold required to flip an existing position.",
    )

    p.add_argument(
        "--trailing-mode",
        type=str,
        default="R_trailing",
        help=(
            "Trailing mode: 'none', 'BE_after_1R', "
            "'halfR_-0.5R_BE_1R', 'R_trailing' (SL always R behind price "
            "in favorable direction, locking in profit as trade moves)."
        ),
    )

    p.add_argument(
        "--verbose-ticks",
        action="store_true",
        help="Print occasional tick debug lines.",
    )

    p.add_argument(
        "--verbose-heartbeats",
        action="store_true",
        help="Print periodic heartbeats from the pricing stream.",
    )

    p.add_argument(
        "--heartbeat-log-every",
        type=int,
        default=20,
        help="When verbose-heartbeats is enabled, log one heartbeat every N heartbeats.",
    )

    p.add_argument(
        "--status-log-interval",
        type=float,
        default=30.0,
        help="Seconds between [status] lines.",
    )

    p.add_argument(
        "--config",
        type=str,
        default=os.path.expanduser("~/.config/pyalgo.cfg"),
        help="Path to tpqoa/pyalgo config file (default: ~/.config/pyalgo.cfg).",
    )

    return p.parse_args()


def main() -> None:
    args = parse_args()

    profile_kwargs: Dict = {}
    if args.profile:
        profile_kwargs = build_profile_args(args.profile)
        if profile_kwargs:
            print(f"[profile] using profile '{args.profile}' -> {profile_kwargs}")
        else:
            print(f"[profile] unknown profile '{args.profile}', ignoring", flush=True)

    # base kwargs from CLI
    kwargs: Dict = dict(
        instrument=args.instrument,
        bar_length=args.bar_length,
        momentum=args.momentum,
        units=args.units,
        max_position_units=args.max_position_units,
        threshold_k=args.threshold_k,
        max_spread_pips=args.max_spread_pips,
        per_trade_sl=args.per_trade_sl,
        per_trade_tp=args.per_trade_tp,
        max_trades_per_day=args.max_trades_per_day,
        max_daily_loss=args.max_daily_loss,
        max_daily_gain=args.max_daily_gain,
        use_regime_filter=args.use_regime_filter,
        regime_lookback=args.regime_lookback,
        regime_vol_min=args.regime_vol_min,
        use_session_filter=args.use_session_filter,
        session_start_hour=args.session_start_hour,
        session_end_hour=args.session_end_hour,
        export_ticks=not args.no_export_ticks,
        min_flip_strength=args.min_flip_strength,
        trailing_mode=args.trailing_mode,
        verbose_ticks=args.verbose_ticks,
        verbose_heartbeats=args.verbose_heartbeats,
        heartbeat_log_every=args.heartbeat_log_every,
        status_log_interval=args.status_log_interval,
    )

    # profile overrides (profile wins)
    kwargs.update(profile_kwargs)

    print("[main] final live trader params:")
    for k in sorted(kwargs.keys()):
        print(f"  {k:20s} = {kwargs[k]}")
    print(flush=True)

    # tpqoa connection using your pyalgo.cfg-style file
    api = tpqoa.tpqoa(args.config)

    trader = MomentumTraderLive(
        api=api,
        **kwargs,
    )

    trader.run()


if __name__ == "__main__":
    main()