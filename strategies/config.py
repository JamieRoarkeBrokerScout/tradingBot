# strategies/config.py
# All strategy parameters — no hardcoded values in strategy files.

# ─── Safeguard limits ─────────────────────────────────────────────────────────
HALT_DAILY_LOSS_USD      = 200.0      # block + hard-stop if daily PnL hits this (20% of $1k account)
HALT_DRAWDOWN_PCT        = 0.15       # 15% NAV drawdown
HALT_MAX_TRADE_SIZE_PCT  = 0.10       # single trade > 10% NAV blocked (allows 1 unit SPX/XAU on $1k)
HALT_MAX_OPEN_POSITIONS  = 12
HALT_MAX_LEVERAGE        = 10.0       # total portfolio leverage (higher for small accounts)
HALT_MARKET_BUFFER_MIN   = 15         # minutes before/after session boundary
HALT_CONSECUTIVE_LOSSES  = 5

# ─── Stat-Arb ─────────────────────────────────────────────────────────────────
STAT_ARB_PAIRS = [
    ("XAU_USD", "XAG_USD"),
    ("EUR_USD", "GBP_USD"),
]
STAT_ARB_LOOKBACK_DAYS   = 60
STAT_ARB_POLL_SECONDS    = 300        # 5 minutes
STAT_ARB_ENTRY_Z         = 2.0
STAT_ARB_EXIT_Z          = 0.5
STAT_ARB_EMERGENCY_Z     = 3.5
STAT_ARB_MAX_AGE_DAYS    = 30
STAT_ARB_NAV_PCT         = 0.05       # 5% NAV per leg
STAT_ARB_STOP_ATR_MULT   = 2.5
STAT_ARB_MIN_CORRELATION = 0.65
STAT_ARB_MIN_SPREAD_STD  = 0.003

# ─── Momentum ─────────────────────────────────────────────────────────────────
MOMENTUM_INSTRUMENTS     = ["SPX500_USD", "XAU_USD"]
MOMENTUM_POLL_SECONDS    = 300        # 5 minutes
MOMENTUM_CANDLES         = 50
MOMENTUM_GRANULARITY     = "H1"
MOMENTUM_RSI_PERIOD      = 14
MOMENTUM_ATR_PERIOD      = 14
MOMENTUM_MA_PERIOD       = 200
MOMENTUM_VOLUME_LOOKBACK = 20
MOMENTUM_RSI_LONG        = 60
MOMENTUM_RSI_SHORT       = 40
MOMENTUM_VOLUME_MULT     = 1.8
MOMENTUM_NAV_PCT         = 0.06       # 6% NAV → ~1 unit SPX500/XAU on $1k account
MOMENTUM_STOP_ATR_MULT   = 2.0
MOMENTUM_TP_ATR_MULT     = 3.5
MOMENTUM_TRAIL_TRIGGER   = 1.0        # ATR multiples profit before trailing activates
MOMENTUM_TRAIL_STOP      = 1.5        # ATR multiples trailing stop distance
MOMENTUM_MAX_OPEN        = 2
MOMENTUM_MIN_GAP_HOURS   = 4
MOMENTUM_MIN_ATR_PCT     = 0.003      # 0.3% of price minimum ATR
MOMENTUM_MAX_AGE_DAYS    = 10
MOMENTUM_RSI_EXIT_LEVEL  = 50

# ─── Vol Premium ──────────────────────────────────────────────────────────────
VOL_INSTRUMENT           = "SPX500_USD"
VOL_POLL_SECONDS         = 900        # 15 minutes
VOL_IV_ATR_PERIOD        = 20
VOL_RV_PERIOD            = 30
VOL_ENTRY_RATIO_MIN      = 1.15
VOL_ENTRY_RATIO_MAX      = 2.0
VOL_KILL_RATIO           = 2.0
VOL_VIX_DISABLE          = 30
VOL_NAV_PCT              = 0.05       # 5% NAV → ~1 unit SPX500 on $1k account
VOL_STOP_ATR_MULT        = 1.5
VOL_TP_ATR_MULT          = 0.8
VOL_MAX_EXPOSURE_PCT     = 0.20       # 20% NAV hard cap
VOL_MAX_AGE_DAYS         = 5
VOL_CLOSE_RATIO          = 1.0        # exit when iv_rv drops below this

# ─── OANDA / network ──────────────────────────────────────────────────────────
OANDA_BACKOFF_BASE       = 1.0
OANDA_BACKOFF_MAX        = 60.0
OANDA_MAX_RETRIES        = 5

# ─── Filesystem ───────────────────────────────────────────────────────────────
HALT_REPORT_PATH         = "HALT_REPORT.json"
ALERTS_LOG_PATH          = "alerts.log"
STATE_FILE_PATH          = "strategy_state.json"
