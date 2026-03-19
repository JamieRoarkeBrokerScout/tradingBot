# TradingBot

A multi-strategy, multi-broker automated trading system with a live dashboard. Six independent bots run concurrently, each on its own dedicated account, managed through a single web UI.

---

## Strategies

| Bot | Timeframe | Instruments | Broker | Account |
|---|---|---|---|---|
| **Stat-Arb** | H1 | EUR/USD–GBP/USD, NAS100–SPX500 | OANDA | ~$1k |
| **Momentum** | H1 | SPX500, NAS100, XAU | OANDA | ~$1k |
| **Vol-Premium** | H1 | SPX500 (sell vol when IV > RV) | OANDA | ~$1k |
| **Crypto** | M15 | BTC, ETH, SOL | Kraken Futures | ~$1k |
| **Daily-Target** | H1 | EUR/USD, GBP/USD, NAS100, SPX500 | OANDA | ~$1k |
| **Scalp** | M5 | NAS100, XAU, GBP/USD | OANDA | ~$200 |

All bots share a common safeguards layer: daily loss halt, drawdown halt, consecutive-loss halt, leverage cap, and a manual-close cooldown that prevents the bot from immediately re-entering an instrument you closed by hand.

---

## Tech Stack

- **Backend:** Python 3.11, Flask, SQLite, Gunicorn
- **Frontend:** React 18, TypeScript, Vite, Tailwind CSS, Recharts
- **Brokers:** OANDA v20 REST API (via `tpqoa`), Kraken Futures REST API
- **Deployment:** Docker on [Railway](https://railway.app)

---

## Local Setup

### Prerequisites

- Python 3.11+
- Node.js 20+
- npm 9+
- A [free OANDA practice account](https://www.oanda.com/us-en/trading/demo-account/) (for OANDA bots)
- A [Kraken Futures demo account](https://demo-futures.kraken.com/) (for the crypto bot)
- `git` installed

### 1. Clone

```bash
git clone https://github.com/your-username/tradingBot.git
cd tradingBot
```

### 2. Install dependencies + create .env

```bash
make env
```

This copies `.env.example` → `.env` and installs all Python and Node packages. You only need to run this once.

### 3. Configure credentials

Open `.env` in your editor and fill in your credentials:

```bash
# Generate a secret key first
make secret
# Copy the output into SECRET_KEY= in .env
```

#### OANDA credentials

1. Log into [fxpractice.oanda.com](https://fxpractice.oanda.com)
2. **My Account → Manage API Access → Generate Access Token**
3. Copy the token — it's shown once only
4. Your Account ID is visible on the account overview page (format: `101-002-XXXXXXXX-001`)

You need **one token** (same for all sub-accounts) and a **separate Account ID per bot**. Create sub-accounts via the OANDA portal — each bot should have its own sub-account so balances and P&L stay isolated.

```env
# Example — stat-arb bot on sub-account -003
JAMIE_STAT_ARB_ACCOUNT_ID=101-002-12345678-003
JAMIE_STAT_ARB_ACCESS_TOKEN=abc123...your-token-here
JAMIE_STAT_ARB_ACCOUNT_TYPE=practice
```

> **Important:** If you create a new OANDA sub-account *after* generating your token, you must regenerate the token — OANDA tokens only cover accounts that existed at generation time.

#### Kraken Futures credentials (crypto bot only)

1. Log into [futures.kraken.com](https://futures.kraken.com) (or [demo-futures.kraken.com](https://demo-futures.kraken.com) for paper trading)
2. **Settings → API Keys → Create Key** — enable "General" and "Trade" permissions
3. Copy the **Public Key** and **Private Key** (shown once)

```env
JAMIE_CRYPTO_ACCOUNT_ID=your-kraken-public-key      # Public Key
JAMIE_CRYPTO_ACCESS_TOKEN=your-kraken-private-key   # Private Key
JAMIE_CRYPTO_ACCOUNT_TYPE=kraken_futures             # or kraken_futures_demo
```

### 4. Run locally

```bash
make dev
```

This starts the Flask API on [http://localhost:5000](http://localhost:5000) and the Vite dev server on [http://localhost:5173](http://localhost:5173).

The dashboard is served from [http://localhost:5173](http://localhost:5173). Hot-reload is enabled for the frontend.

**First login:** the default credentials are `admin` / `password`. Change the password immediately via the Settings panel.

---

## Available Make commands

```
make env         Copy .env.example → .env and install all dependencies
make dev         Run API + frontend with hot-reload
make api         Run Flask API only
make frontend    Run Vite dev server only
make build       Build frontend for production
make secret      Generate a random SECRET_KEY
make clean-db    Delete local SQLite database (irreversible)
make clean       Remove __pycache__ and build artefacts
```

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | Flask session secret. Generate with `make secret`. |
| `JAMIE_<BOT>_ACCOUNT_ID` | Per bot | OANDA sub-account ID or Kraken public key |
| `JAMIE_<BOT>_ACCESS_TOKEN` | Per bot | OANDA access token or Kraken private key |
| `JAMIE_<BOT>_ACCOUNT_TYPE` | Per bot | `practice`, `live`, `kraken_futures`, or `kraken_futures_demo` |
| `OANDA_ACCOUNT_ID` | Optional | Fallback OANDA account for price feeds |
| `OANDA_ACCESS_TOKEN` | Optional | Fallback OANDA token |
| `OANDA_ACCOUNT_TYPE` | Optional | `practice` or `live` |
| `PORT` | Auto | Set by Railway automatically — leave unset locally |

Bot keys: `STAT_ARB`, `MOMENTUM`, `VOL_PREMIUM`, `CRYPTO`, `DAILY_TARGET`, `SCALP`

---

## Dashboard

The web UI lets you:

- **Monitor** all open positions, P&L, and account balances in real time
- **Enable / disable** individual bots without restarting
- **Manually close** any open trade (a 4-hour cooldown prevents the bot from immediately re-entering)
- **View trade history** with per-strategy win rates and cumulative P&L
- **Configure credentials** for each bot via the API Settings panel

---

## Deploying to Railway

1. Fork this repo
2. Create a new project on [railway.app](https://railway.app) → **Deploy from GitHub repo**
3. Add all the env vars from your `.env` file in the Railway dashboard (**Variables** tab)
4. Railway detects the `Dockerfile` and deploys automatically
5. Add a **Volume** mounted at `/app/data` to persist the SQLite database across redeploys

The app listens on `$PORT` (Railway sets this automatically). No `PORT` env var needed.

---

## Project Structure

```
tradingBot/
├── api/
│   └── server.py          # Flask API — auth, trade endpoints, strategy control
├── database/
│   └── database.py        # SQLite helpers — trades, open positions, state
├── strategies/
│   ├── base.py            # SafeguardsBase — shared risk controls for all bots
│   ├── config.py          # All strategy parameters (edit here to tune bots)
│   ├── runner.py          # Orchestrator — spawns bots, routes signals, submits orders
│   ├── _utils.py          # Shared indicators (RSI, ATR, OHLCV fetch)
│   ├── stat_arb.py
│   ├── momentum.py
│   ├── vol_premium.py
│   ├── crypto_momentum.py
│   ├── daily_target.py
│   ├── scalp.py
│   └── brokers/
│       ├── kraken.py          # Kraken spot adapter
│       └── kraken_futures.py  # Kraken Futures adapter
├── frontend/
│   └── src/
│       └── components/    # React dashboard components
├── data/                  # SQLite DB + runtime state (gitignored)
├── .env.example           # Template — copy to .env
├── Makefile               # Dev workflow shortcuts
├── requirements.txt       # Python dependencies
├── Dockerfile             # Production build
└── railway.toml           # Railway deployment config
```

---

## Tuning the Bots

All parameters live in [strategies/config.py](strategies/config.py). Key sections:

- `SCALP_*` — scalp bot: EMA periods, stop/TP multipliers, leverage cap
- `CRYPTO_*` — crypto bot: RSI thresholds, target leverage, M15 granularity
- `MOMENTUM_*` — momentum bot: RSI levels, MA periods, ATR stop multiplier
- `DT_*` — daily-target bot: 2% daily target, instruments
- `VOL_*` — vol-premium bot: IV/RV ratio thresholds
- `HALT_*` — global safeguards: daily loss %, max leverage, max drawdown

---

## Disclaimer

This software is for educational and research purposes. Trading financial instruments carries significant risk of loss. Past performance of these strategies on practice accounts does not guarantee future results. Never trade with money you cannot afford to lose.
