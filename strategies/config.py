# strategies/config.py
# All strategy parameters — no hardcoded values in strategy files.

# ─── Safeguard limits ─────────────────────────────────────────────────────────
HALT_DAILY_LOSS_USD      = 200.0      # block + hard-stop if daily PnL hits this (20% of $1k account)
HALT_DRAWDOWN_PCT        = 0.15       # 15% NAV drawdown
HALT_MAX_TRADE_SIZE_PCT  = 0.10       # single trade > 10% NAV blocked (allows 1 unit SPX/XAU on $1k)
HALT_MAX_OPEN_POSITIONS  = 12
HALT_MAX_LEVERAGE        = 100.0      # multiple independent accounts — per-account leverage not tracked globally
HALT_MARKET_BUFFER_MIN   = 15         # minutes before/after session boundary
HALT_CONSECUTIVE_LOSSES  = 5
HALT_FRIDAY_CUTOFF_UTC   = 20         # no new opens on Friday at/after this hour (UTC)
                                      # 20:00 UTC = 3 PM ET = 12 PM PT

# ─── Stat-Arb ─────────────────────────────────────────────────────────────────
STAT_ARB_PAIRS = [
    ("XAU_USD", "XAG_USD"),
    ("EUR_USD", "GBP_USD"),
    ("NAS100_USD", "SPX500_USD"),
]
STAT_ARB_LOOKBACK_DAYS   = 60
STAT_ARB_POLL_SECONDS    = 300        # 5 minutes
STAT_ARB_ENTRY_Z         = 1.5
STAT_ARB_EXIT_Z          = 0.5
STAT_ARB_EMERGENCY_Z     = 3.5
STAT_ARB_MAX_AGE_DAYS    = 30
STAT_ARB_NAV_PCT         = 0.05       # 5% NAV per leg
STAT_ARB_STOP_ATR_MULT   = 2.5
STAT_ARB_MIN_CORRELATION = 0.40
STAT_ARB_MIN_SPREAD_STD  = 0.003

# ─── Momentum ─────────────────────────────────────────────────────────────────
MOMENTUM_INSTRUMENTS     = ["SPX500_USD", "XAU_USD", "NAS100_USD"]
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
VOL_ENTRY_RATIO_MIN      = 1.08
VOL_ENTRY_RATIO_MAX      = 2.0
VOL_KILL_RATIO           = 2.0
VOL_VIX_DISABLE          = 30
VOL_NAV_PCT              = 0.10       # 10% NAV risk budget — sets minimum 1 unit
VOL_STOP_ATR_MULT        = 1.0        # used to compute ideal size; stop adjusted to hit exact budget
VOL_TP_ATR_MULT          = 0.8
VOL_MAX_EXPOSURE_PCT     = 0.20       # 20% NAV hard cap
VOL_MAX_AGE_DAYS         = 5
VOL_CLOSE_RATIO          = 1.0        # exit when iv_rv drops below this

# ─── Crypto Momentum ──────────────────────────────────────────────────────────
CRYPTO_INSTRUMENTS     = ["BTC_USD", "ETH_USD", "SOL_USD"]
CRYPTO_POLL_SECONDS    = 300         # 5 minutes
CRYPTO_CANDLES         = 200         # bars to fetch
CRYPTO_GRANULARITY     = "M15"       # M15: 4× smaller ATR → tight stops → 3× leverage achievable
CRYPTO_RSI_PERIOD      = 14
CRYPTO_ATR_PERIOD      = 14
CRYPTO_MA_PERIOD       = 50          # 50 × M15 = 12.5 h trend filter
CRYPTO_RSI_LONG        = 55          # RSI must cross above this → bullish
CRYPTO_RSI_SHORT       = 45          # RSI must cross below this → bearish
CRYPTO_RSI_EXIT        = 42          # exit long when RSI < 42; exit short when RSI > 58
CRYPTO_CROSS_LOOKBACK  = 3           # accept RSI crossover within last N bars
CRYPTO_TARGET_LEVERAGE = 3.0         # target 3× NAV notional (minimum meaningful size)
CRYPTO_MAX_RISK_PCT    = 0.05        # cap single-trade risk at 5% NAV
CRYPTO_STOP_ATR_MULT   = 2.0         # 2.0× M15 ATR — wider to survive M15 noise
CRYPTO_TP_ATR_MULT     = 4.0         # 4.0× M15 ATR — 2:1 RR maintained
CRYPTO_TRAIL_TRIGGER   = 1.5         # ATR profit before trailing activates
CRYPTO_TRAIL_STOP      = 2.5         # trailing stop distance in ATR
CRYPTO_MIN_ATR_PCT     = 0.001       # 0.1% minimum ATR
CRYPTO_MAX_OPEN        = 2           # max 1 per instrument, 2 total
CRYPTO_MIN_GAP_HOURS   = 2           # minimum hours before re-entering same instrument
CRYPTO_MAX_AGE_DAYS    = 7
CRYPTO_RSI_MIN_HOLD_BARS = 4         # min M15 bars (1 hour) before RSI exit can fire

# ─── Daily Target ─────────────────────────────────────────────────────────────
DT_INSTRUMENTS      = ["EUR_USD", "GBP_USD", "NAS100_USD", "XAU_USD", "SPX500_USD"]
DT_TARGET_PCT       = 0.02    # stop for the day after +2% NAV daily P&L
DT_LOSS_LIMIT_PCT   = 0.03    # hard stop for the day at -3% NAV daily loss
DT_POLL_SECONDS     = 300     # 5 minutes
DT_GRANULARITY      = "M15"
DT_RSI_PERIOD       = 14
DT_ATR_PERIOD       = 14
DT_MA_PERIOD        = 20      # 20-period MA on M15 = 5 hours
DT_RSI_LONG         = 55      # gentler than momentum — more frequent signals
DT_RSI_SHORT        = 45
DT_NAV_PCT          = 0.02    # risk 2% NAV per trade; one win ≈ 2.7% (2×ATR TP vs 1.5×ATR SL)
DT_STOP_ATR_MULT    = 1.5
DT_TP_ATR_MULT      = 2.0
DT_MIN_ATR_PCT      = 0.001   # 0.1% minimum volatility
DT_MAX_OPEN         = 3
DT_MIN_GAP_HOURS    = 1
DT_MAX_AGE_HOURS    = 24

# ─── Scalp (5-min high-frequency) ────────────────────────────────────────────
# ~$200 account. Fast EMA crossover + RSI filter. High leverage, quick exits.
# OANDA minimum granularity is M5; M3 is not supported.
SCALP_INSTRUMENTS    = ["NAS100_USD", "XAU_USD", "GBP_USD"]
SCALP_POLL_SECONDS   = 60        # check every 60 s — need to be fast for 5-min bars
SCALP_GRANULARITY    = "M5"      # 5-min candles
SCALP_CANDLES        = 120       # 10 hours of 5-min bars for warm-up
SCALP_EMA_FAST       = 8
SCALP_EMA_SLOW       = 21
SCALP_RSI_PERIOD     = 14
SCALP_RSI_LONG       = 50        # RSI just above midline — enter on momentum
SCALP_RSI_SHORT      = 50        # RSI just below midline — enter on momentum
SCALP_ATR_PERIOD     = 14
SCALP_STOP_ATR_MULT  = 0.8       # tight stop — cut fast
SCALP_TP_ATR_MULT    = 1.6       # 2:1 R:R — take profit quickly
SCALP_NAV_PCT        = 0.15      # risk 15% NAV per trade ($30 on $200) — high leverage
SCALP_MAX_OPEN       = 2         # max concurrent positions
SCALP_MIN_GAP_MINS   = 5         # 5 min cooldown — re-enter quickly
SCALP_MAX_AGE_BARS   = 6         # force-close after 30 min
SCALP_MIN_ATR_PCT    = 0.0002    # skip only if completely flat
SCALP_CROSS_LOOKBACK = 3         # accept EMA cross within last 3 bars (not just last bar)
SCALP_MAX_LEVERAGE   = 200.0     # cap: 200× lets $200 account hold 1+ units of NAS100/XAU

# ─── OANDA / network ──────────────────────────────────────────────────────────
OANDA_BACKOFF_BASE       = 1.0
OANDA_BACKOFF_MAX        = 60.0
OANDA_MAX_RETRIES        = 5

# ─── Filesystem ───────────────────────────────────────────────────────────────
HALT_REPORT_PATH         = "HALT_REPORT.json"
ALERTS_LOG_PATH          = "alerts.log"
STATE_FILE_PATH          = "strategy_state.json"
