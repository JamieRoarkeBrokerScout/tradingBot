import json
import os
import sys
import signal
import sqlite3
import subprocess
import tempfile
from functools import wraps

import requests as _requests
from pathlib import Path

# Ensure project root is on sys.path however the server is invoked
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import bcrypt
from dotenv import load_dotenv
from flask import Flask, jsonify, request, g, send_from_directory
from flask_cors import CORS

# Load .env from project root
load_dotenv(_project_root / ".env")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# Serve the built React frontend (production)
_FRONTEND_DIST = _project_root / "frontend" / "dist"
CORS(app, supports_credentials=True, origins="*")

BOT_SCRIPT           = _project_root / "bot" / "momentum_trader_live.py"
STRATEGY_STATE_FILE  = _project_root / "strategy_state.json"
STRATEGY_RUNNER      = _project_root / "strategies" / "runner.py"

_DEFAULT_STRATEGY_STATE = {
    "stat_arb":    {"enabled": False},
    "momentum":    {"enabled": False},
    "vol_premium": {"enabled": False},
    "crypto":      {"enabled": False},
}

# Global strategy runner process
strategy_runner_process = None

from database.database import (
    DB_PATH,
    init_db, seed_users,
    get_user_by_email, get_session, create_session, delete_session,
    get_user_token, get_all_user_tokens, upsert_user_token, ALL_BOT_KEYS,
    get_open_trades,
    get_strategy_states, upsert_strategy_state,
    record_closed_trade,
)

# Authorised users — plain passwords are hashed fresh at startup so there
# are no encoding issues with pre-computed hash strings.
_RAW_USERS = [
    ("jamieroarke18@gmail.com", "Jamieridingcobs1!"),
    ("jameslyons@gmail.com",    "Peteysbiggestfan!"),
]

SEED_USERS = [
    (email, bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode())
    for email, pw in _RAW_USERS
]

# Global bot state
bot_process = None
current_config = None
bot_owner_user_id = None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_token_from_request() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.headers.get("X-Session-Token")


def require_auth(f):
    """Decorator: reject requests without a valid session token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _get_token_from_request()
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        session = get_session(token)
        if not session:
            return jsonify({"error": "Unauthorized"}), 401
        g.user_id = session["user_id"]
        g.user_email = session["email"]
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").encode()

    user = get_user_by_email(email)
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401

    if not bcrypt.checkpw(password, user["password_hash"].encode()):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_session(user["id"], user["email"])
    return jsonify({"token": token, "email": user["email"], "user_id": user["id"]})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    token = _get_token_from_request()
    if token:
        delete_session(token)
    return jsonify({"status": "logged out"})


@app.route("/api/auth/session", methods=["GET"])
def auth_session():
    token = _get_token_from_request()
    if not token:
        return jsonify({"error": "No active session"}), 401
    session = get_session(token)
    if not session:
        return jsonify({"error": "No active session"}), 401
    return jsonify({
        "user_id": session["user_id"],
        "email": session["email"],
    })


# ---------------------------------------------------------------------------
# User OANDA token routes
# ---------------------------------------------------------------------------

@app.route("/api/user/tokens", methods=["GET"])
@require_auth
def get_tokens():
    """Return credentials for all bots for this user."""
    all_tokens = get_all_user_tokens(g.user_id)
    result = {}
    for bot_key in ALL_BOT_KEYS:
        row = all_tokens.get(bot_key)
        if row:
            result[bot_key] = {
                "configured":       True,
                "oanda_account_id": row["oanda_account_id"],
                "oanda_account_type": row["oanda_account_type"],
                "oanda_access_token_set": bool(row["oanda_access_token"]),
            }
        else:
            result[bot_key] = {"configured": False}
    return jsonify(result)


@app.route("/api/user/tokens", methods=["POST"])
@require_auth
def save_tokens():
    data         = request.get_json() or {}
    bot_key      = (data.get("bot_key") or "legacy_bot").strip()
    account_id   = (data.get("oanda_account_id") or "").strip()
    access_token = (data.get("oanda_access_token") or "").strip()
    account_type = (data.get("oanda_account_type") or "practice").strip()

    if bot_key not in ALL_BOT_KEYS:
        return jsonify({"error": f"Unknown bot_key: {bot_key}"}), 400
    if not account_id or not access_token:
        return jsonify({"error": "oanda_account_id and oanda_access_token are required"}), 400

    upsert_user_token(g.user_id, bot_key, account_id, access_token, account_type)
    return jsonify({"status": "saved", "bot_key": bot_key})


# ---------------------------------------------------------------------------
# Credential resolution for bot
# ---------------------------------------------------------------------------

def _resolve_credentials(user_id: int, bot_key: str = "legacy_bot"):
    """Return (account_id, access_token, account_type) for a specific bot, or None."""
    row = get_user_token(user_id, bot_key)
    if row:
        return row["oanda_account_id"], row["oanda_access_token"], row["oanda_account_type"]

    # Fall back to .env defaults
    account_id   = os.environ.get("OANDA_ACCOUNT_ID")
    access_token = os.environ.get("OANDA_ACCESS_TOKEN")
    account_type = os.environ.get("OANDA_ACCOUNT_TYPE", "practice")
    if account_id and access_token:
        return account_id, access_token, account_type

    return None


# Maps each user email to an env var prefix.
# Set JAMIE_STAT_ARB_ACCOUNT_ID etc. in Railway dashboard — persists forever,
# no volume needed. Each user's credentials stay completely separate.
_USER_ENV_PREFIX = {
    "jamieroarke18@gmail.com": "JAMIE",
    "jameslyons@gmail.com":    "JAMES",
}

_STRATEGY_ENV_KEY = {
    "stat_arb":    "STAT_ARB",
    "momentum":    "MOMENTUM",
    "vol_premium": "VOL_PREMIUM",
    "crypto":      "CRYPTO",
}


def seed_tokens_from_env() -> None:
    """Populate per-user OANDA tokens from env vars at every startup.

    Env var pattern:  {USER_PREFIX}_{STRATEGY_KEY}_ACCOUNT_ID
                      {USER_PREFIX}_{STRATEGY_KEY}_ACCESS_TOKEN
                      {USER_PREFIX}_{STRATEGY_KEY}_ACCOUNT_TYPE  (defaults to 'practice')

    Example:  JAMIE_STAT_ARB_ACCOUNT_ID, JAMIE_STAT_ARB_ACCESS_TOKEN
              JAMES_MOMENTUM_ACCOUNT_ID,  JAMES_MOMENTUM_ACCESS_TOKEN
    """
    total = 0
    for email, prefix in _USER_ENV_PREFIX.items():
        user = get_user_by_email(email)
        if not user:
            print(f"[seed_tokens] user not found in DB: {email}")
            continue
        user_id = user["id"]
        for bot_key, strat_key in _STRATEGY_ENV_KEY.items():
            account_id   = os.environ.get(f"{prefix}_{strat_key}_ACCOUNT_ID")
            access_token = os.environ.get(f"{prefix}_{strat_key}_ACCESS_TOKEN")
            account_type = os.environ.get(f"{prefix}_{strat_key}_ACCOUNT_TYPE", "practice")
            if account_id and access_token:
                try:
                    upsert_user_token(user_id, bot_key, account_id, access_token, account_type)
                    print(f"[seed_tokens] seeded user_id={user_id} ({email}) bot={bot_key}")
                    total += 1
                except Exception as exc:
                    print(f"[seed_tokens] ERROR user_id={user_id} bot={bot_key}: {exc}")
            else:
                print(f"[seed_tokens] no env vars for {prefix}_{strat_key}_ACCOUNT_ID / ACCESS_TOKEN")
    print(f"[seed_tokens] done — {total} tokens seeded")


def _build_creds_map(user_id: int) -> dict:
    """Build a dict of {bot_key: {account_id, access_token, account_type}} for all strategy bots."""
    creds_map = {}
    for bot_key in ["stat_arb", "momentum", "vol_premium"]:
        row = get_user_token(user_id, bot_key)
        if row:
            creds_map[bot_key] = {
                "account_id":   row["oanda_account_id"],
                "access_token": row["oanda_access_token"],
                "account_type": row["oanda_account_type"],
            }
    return creds_map


def _write_temp_cfg(account_id, access_token, account_type) -> str:
    cfg = (
        "[oanda]\n"
        f"account_id = {account_id}\n"
        f"access_token = {access_token}\n"
        f"account_type = {account_type}\n"
    )
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", prefix="oanda_", delete=False)
    tmp.write(cfg)
    tmp.flush()
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# Bot process helpers
# ---------------------------------------------------------------------------

def is_bot_running():
    global bot_process
    return bot_process is not None and bot_process.poll() is None


def is_runner_running():
    global strategy_runner_process
    return strategy_runner_process is not None and strategy_runner_process.poll() is None


def _load_strategy_state() -> dict:
    return get_strategy_states()


def _save_strategy_state(state: dict) -> None:
    for name, val in state.items():
        upsert_strategy_state(name, bool(val.get("enabled", False)))


def _start_runner(user_id: int) -> None:
    global strategy_runner_process
    if is_runner_running():
        return

    creds_map = _build_creds_map(user_id)
    if not creds_map:
        raise ValueError("No OANDA credentials configured. Add your API tokens in Settings.")

    # Write per-strategy credentials to a temp JSON file
    import tempfile
    creds_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="oanda_creds_", delete=False
    )
    json.dump(creds_map, creds_file)
    creds_file.flush()
    creds_file.close()

    cmd = [
        sys.executable, "-u", str(STRATEGY_RUNNER),
        "--creds", creds_file.name,
    ]
    strategy_runner_process = subprocess.Popen(
        cmd,
        cwd=str(_project_root),
        env={**os.environ, "PYTHONPATH": str(_project_root), "PYTHONUNBUFFERED": "1"},
    )
    print(f"Strategy runner started (PID {strategy_runner_process.pid})")


def _stop_runner() -> None:
    global strategy_runner_process
    if not is_runner_running():
        strategy_runner_process = None
        return
    try:
        strategy_runner_process.send_signal(signal.SIGTERM)
        try:
            strategy_runner_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            strategy_runner_process.kill()
            strategy_runner_process.wait()
    except Exception:
        pass
    strategy_runner_process = None
    print("Strategy runner stopped")


# ---------------------------------------------------------------------------
# Trade / stats routes
# ---------------------------------------------------------------------------

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# OANDA REST helpers (used by account + trade-close endpoints)
# ---------------------------------------------------------------------------

def _oanda_base_url(account_type: str) -> str:
    if account_type == "live":
        return "https://api-fxtrade.oanda.com/v3"
    return "https://api-fxpractice.oanda.com/v3"


def _oanda_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


def _fetch_oanda_prices(row: dict, instruments: list[str]) -> dict[str, float]:
    """Return {instrument: mid_price} for a list of instruments using one account's token."""
    base = _oanda_base_url(row["oanda_account_type"])
    resp = _requests.get(
        f"{base}/accounts/{row['oanda_account_id']}/pricing",
        headers=_oanda_headers(row["oanda_access_token"]),
        params={"instruments": ",".join(instruments)},
        timeout=5,
    )
    prices: dict[str, float] = {}
    if resp.status_code == 200:
        for p in resp.json().get("prices", []):
            bid = float((p.get("bids") or [{}])[0].get("price", 0))
            ask = float((p.get("asks") or [{}])[0].get("price", 0))
            if bid > 0 and ask > 0:
                prices[p["instrument"]] = (bid + ask) / 2
    return prices


# ---------------------------------------------------------------------------
# Open-trades endpoint (enhanced with live P&L)
# ---------------------------------------------------------------------------

@app.route("/api/open_trades", methods=["GET"])
@require_auth
def open_trades_route():
    trades = get_open_trades()
    if not trades:
        return jsonify([])

    all_tokens = get_all_user_tokens(g.user_id)

    # Pull unrealised P&L directly from OANDA's openTrades endpoint per strategy account.
    # OANDA uses the actual fill price so this is more accurate than our price calculation.
    # oanda_pl[strategy][instrument] = sum of unrealizedPL across all OANDA trades for that instrument
    # oanda_price[strategy][instrument] = average current price from OANDA
    oanda_pl:    dict[str, dict[str, float]] = {}
    oanda_price: dict[str, dict[str, float]] = {}

    for strategy in {t["strategy"] for t in trades}:
        row = all_tokens.get(strategy)
        if not row or not row.get("oanda_access_token"):
            continue
        try:
            base = _oanda_base_url(row["oanda_account_type"])
            resp = _requests.get(
                f"{base}/accounts/{row['oanda_account_id']}/openTrades",
                headers=_oanda_headers(row["oanda_access_token"]),
                timeout=5,
            )
            if resp.status_code == 200:
                oanda_pl[strategy]    = {}
                oanda_price[strategy] = {}
                for ot in resp.json().get("trades", []):
                    inst = ot.get("instrument", "")
                    oanda_pl[strategy][inst] = (
                        oanda_pl[strategy].get(inst, 0.0) + float(ot.get("unrealizedPL", 0))
                    )
                    # currentUnits is signed; price is the current market price stored by OANDA
                    if "price" in ot:
                        oanda_price[strategy][inst] = float(ot["price"])
        except Exception:
            pass

    # Fall back to mid-price fetch for strategies where OANDA call failed
    fallback_instruments = [
        t["instrument"] for t in trades
        if t["strategy"] not in oanda_pl
    ]
    prices: dict[str, float] = {}
    if fallback_instruments:
        token_row = next((r for r in all_tokens.values() if r.get("oanda_access_token")), None)
        if token_row:
            try:
                prices = _fetch_oanda_prices(token_row, list(set(fallback_instruments)))
            except Exception:
                pass

    for t in trades:
        strategy   = t["strategy"]
        instrument = t["instrument"]

        if strategy in oanda_pl and instrument in oanda_pl[strategy]:
            # OANDA's accurate unrealized P&L (accounts for actual fill price + spread)
            t["unrealized_pl"]  = oanda_pl[strategy][instrument]
            t["current_price"]  = oanda_price[strategy].get(instrument) or prices.get(instrument)
        else:
            # Fallback: mid-price estimate
            current = prices.get(instrument, 0.0)
            t["current_price"] = current if current > 0 else None
            if current > 0 and t.get("entry_price", 0) > 0:
                t["unrealized_pl"] = (current - t["entry_price"]) * t["direction"] * t["units"]
            else:
                t["unrealized_pl"] = None

    return jsonify(trades)


@app.route("/api/open_trades/<string:trade_key>/close", methods=["POST"])
@require_auth
def close_open_trade(trade_key):
    """Close a live position on OANDA and remove it from the open_trades table."""
    from database.database import delete_open_trade

    trades = get_open_trades()
    trade = next((t for t in trades if t["trade_key"] == trade_key), None)
    if not trade:
        return jsonify({"error": "Trade not found"}), 404

    strategy   = trade["strategy"]
    instrument = trade["instrument"]
    direction  = trade["direction"]

    # Get credentials — prefer the strategy's own account, fall back to any
    row = get_user_token(g.user_id, strategy)
    if not row:
        all_tokens = get_all_user_tokens(g.user_id)
        row = next(iter(all_tokens.values()), None)
    if not row:
        return jsonify({"error": "No OANDA credentials configured"}), 400

    try:
        base = _oanda_base_url(row["oanda_account_type"])
        url = f"{base}/accounts/{row['oanda_account_id']}/positions/{instrument}/close"
        hdrs = _oanda_headers(row["oanda_access_token"])

        # Try the expected direction first; if OANDA says no units, try the other direction.
        for attempt_body in (
            {"longUnits": "ALL"} if direction > 0 else {"shortUnits": "ALL"},
            {"shortUnits": "ALL"} if direction > 0 else {"longUnits": "ALL"},
        ):
            resp = _requests.put(url, headers=hdrs, json=attempt_body, timeout=10)
            if resp.status_code in (200, 201):
                # Parse exit price and P&L from OANDA response
                try:
                    body     = resp.json()
                    fill_txn = body.get("longOrderFillTransaction") or body.get("shortOrderFillTransaction") or {}
                    raw_pl   = float(fill_txn.get("pl", 0) or 0)
                    exit_price = float(fill_txn.get("price", trade.get("entry_price", 0)) or 0)
                    entry_price = float(trade.get("entry_price", 0) or 0)
                    units       = float(trade.get("units", 1) or 1)
                    entry_time  = trade.get("entry_time", "")
                    from datetime import datetime, timezone
                    exit_time = datetime.now(timezone.utc).isoformat()
                    record_closed_trade(
                        instrument=instrument,
                        direction=direction,
                        units=units,
                        entry_price=entry_price,
                        exit_price=exit_price if exit_price > 0 else entry_price,
                        entry_time=entry_time,
                        exit_time=exit_time,
                        exit_reason="manual_close",
                        raw_pl=raw_pl,
                        strategy_name=strategy,
                    )
                except Exception:
                    pass  # recording failure must not block the close response
                delete_open_trade(trade_key)
                return jsonify({"status": "closed", "trade_key": trade_key})
            err_text = resp.text or ""
            # If no units in this direction, try the opposite
            if "NO_UNITS_TO_CLOSEOUT" in err_text:
                continue
            break

        # Position doesn't exist on OANDA (already closed by stop/TP, or stale DB record).
        # Clean up the DB row so the UI stops showing it.
        already_gone = (
            resp.status_code == 404
            or "CLOSEOUT_POSITION_DOESNT_EXIST" in err_text
            or "NO_UNITS_TO_CLOSEOUT" in err_text
        )
        if already_gone:
            delete_open_trade(trade_key)
            return jsonify({"status": "removed_stale", "trade_key": trade_key})

        return jsonify({"error": f"OANDA {resp.status_code}: {err_text[:300]}"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Account summary endpoint
# ---------------------------------------------------------------------------

@app.route("/api/account", methods=["GET"])
@require_auth
def get_account():
    """Fetch live balance, equity, margin and leverage from OANDA for each strategy account."""
    all_tokens = get_all_user_tokens(g.user_id)
    result: dict = {}
    for bot_key in ["stat_arb", "momentum", "vol_premium"]:
        row = all_tokens.get(bot_key)
        if not row or not row.get("oanda_access_token"):
            continue
        try:
            base = _oanda_base_url(row["oanda_account_type"])
            resp = _requests.get(
                f"{base}/accounts/{row['oanda_account_id']}/summary",
                headers=_oanda_headers(row["oanda_access_token"]),
                timeout=5,
            )
            if resp.status_code == 200:
                acct = resp.json().get("account", {})
                nav          = float(acct.get("NAV", 0) or 0)
                margin_used  = float(acct.get("marginUsed", 0) or 0)
                result[bot_key] = {
                    "account_id":       row["oanda_account_id"],
                    "balance":          float(acct.get("balance", 0) or 0),
                    "nav":              nav,
                    "unrealized_pl":    float(acct.get("unrealizedPL", 0) or 0),
                    "margin_used":      margin_used,
                    "margin_available": float(acct.get("marginAvailable", 0) or 0),
                    "open_trade_count": int(acct.get("openTradeCount", 0) or 0),
                    "margin_pct":       round(margin_used / nav * 100, 1) if nav > 0 else 0,
                    "currency":         acct.get("currency", "USD"),
                }
            else:
                result[bot_key] = {"error": f"HTTP {resp.status_code}"}
        except Exception as exc:
            result[bot_key] = {"error": str(exc)}
    return jsonify(result)


@app.route("/api/trades", methods=["GET"])
@require_auth
def get_trades():
    conn = _get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades ORDER BY exit_time DESC LIMIT 50")
    trades = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(trades)


@app.route("/api/stats", methods=["GET"])
@require_auth
def get_stats():
    conn = _get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            COUNT(*) as trades_today,
            COALESCE(SUM(raw_pl), 0) as daily_pnl,
            SUM(CASE WHEN raw_pl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN raw_pl < 0 THEN 1 ELSE 0 END) as losses
        FROM trades
        WHERE DATE(exit_time) = DATE('now')
    """)
    stats = dict(cursor.fetchone())
    conn.close()
    return jsonify(stats)


# ---------------------------------------------------------------------------
# Bot control routes
# ---------------------------------------------------------------------------

@app.route("/api/bot/start", methods=["POST"])
@require_auth
def start_bot():
    global bot_process, current_config, bot_owner_user_id

    if is_bot_running():
        return jsonify({"error": "Bot is already running", "pid": bot_process.pid}), 400

    creds = _resolve_credentials(g.user_id, "legacy_bot")
    if not creds:
        return jsonify({"error": "No OANDA credentials configured. Add your API token in Settings."}), 400

    account_id, access_token, account_type = creds
    cfg_path = _write_temp_cfg(account_id, access_token, account_type)

    data = request.get_json() or {}
    profile = data.get("profile")

    try:
        python_exe = sys.executable

        if profile:
            cmd = [python_exe, str(BOT_SCRIPT), "--profile", profile, "--config", cfg_path]
        else:
            instrument = data.get("instrument", "NAS100_USD")
            bar_length = data.get("bar_length", "3min")
            units = data.get("units", 1)
            threshold_k = data.get("threshold_k", 1.8)
            per_trade_sl = data.get("per_trade_sl", 20.0)
            per_trade_tp = data.get("per_trade_tp", 60.0)

            cmd = [
                python_exe, str(BOT_SCRIPT),
                "--instrument", instrument,
                "--bar-length", bar_length,
                "--units", str(units),
                "--threshold-k", str(threshold_k),
                "--per-trade-sl", str(per_trade_sl),
                "--per-trade-tp", str(per_trade_tp),
                "--use-session-filter",
                "--session-start-hour", "13",
                "--session-end-hour", "21",
                "--config", cfg_path,
            ]

        bot_process = subprocess.Popen(cmd, cwd=str(BOT_SCRIPT.parent.parent))
        current_config = data
        bot_owner_user_id = g.user_id

        print(f"Bot started (PID {bot_process.pid}) for user {g.user_email}")
        return jsonify({"status": "started", "pid": bot_process.pid, "config": data})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bot/stop", methods=["POST"])
@require_auth
def stop_bot():
    global bot_process, current_config, bot_owner_user_id

    if not is_bot_running():
        bot_process = None
        return jsonify({"error": "Bot is not running"}), 400

    try:
        bot_process.send_signal(signal.SIGINT)
        try:
            bot_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bot_process.kill()
            bot_process.wait()

        bot_process = None
        current_config = None
        bot_owner_user_id = None
        return jsonify({"status": "stopped"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bot/config", methods=["GET"])
@require_auth
def get_config():
    return jsonify(current_config or {})


@app.route("/api/health", methods=["GET"])
@require_auth
def health():
    running = is_bot_running()
    return jsonify({
        "status":      "ok",
        "bot_running": running,
        "pid":         bot_process.pid if running else None,
        "config":      current_config,
        "environment": os.environ.get("ENVIRONMENT", "staging"),
    })


# ---------------------------------------------------------------------------
# Strategy routes
# ---------------------------------------------------------------------------

VALID_STRATEGIES = {"stat_arb", "momentum", "vol_premium", "crypto"}


@app.route("/api/strategies", methods=["GET"])
@require_auth
def get_strategies():
    state = _load_strategy_state()
    return jsonify({
        "runner_running": is_runner_running(),
        "strategies": state,
    })


@app.route("/api/strategies/<name>/toggle", methods=["POST"])
@require_auth
def toggle_strategy(name):
    if name not in VALID_STRATEGIES:
        return jsonify({"error": f"Unknown strategy: {name}"}), 400

    state       = _load_strategy_state()
    new_enabled = not state.get(name, {}).get("enabled", False)
    state[name] = {"enabled": new_enabled}
    _save_strategy_state(state)

    any_enabled = any(v.get("enabled") for v in state.values())

    if new_enabled:
        try:
            _start_runner(g.user_id)
        except Exception as exc:
            # Revert the state change and surface the error to the UI
            state[name] = {"enabled": False}
            _save_strategy_state(state)
            return jsonify({
                "error":          str(exc),
                "strategy":       name,
                "enabled":        False,
                "runner_running": is_runner_running(),
                "strategies":     state,
            }), 400
    elif not any_enabled:
        _stop_runner()

    return jsonify({
        "strategy":       name,
        "enabled":        new_enabled,
        "runner_running": is_runner_running(),
        "strategies":     state,
    })


@app.route("/api/strategies/runner/stop", methods=["POST"])
@require_auth
def stop_all_strategies():
    state = _load_strategy_state()
    for name in state:
        state[name] = {"enabled": False}
    _save_strategy_state(state)
    _stop_runner()
    return jsonify({"status": "stopped"})


# ---------------------------------------------------------------------------
# Frontend static file serving (production — files built by Dockerfile)
# ---------------------------------------------------------------------------

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if _FRONTEND_DIST.exists():
        target = _FRONTEND_DIST / path
        if path and target.exists() and target.is_file():
            return send_from_directory(_FRONTEND_DIST, path)
        # Always re-fetch index.html so browsers pick up new JS filenames
        resp = send_from_directory(_FRONTEND_DIST, "index.html")
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return jsonify({"error": "Frontend not built"}), 404


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

# Run DB init + seed whether started via gunicorn or directly
init_db()
seed_users(SEED_USERS)
seed_tokens_from_env()

# Auto-restart runner if strategies were enabled before this deploy
def _autostart_runner():
    state = get_strategy_states()
    if not any(v.get("enabled") for v in state.values()):
        return
    # Find the first user who has tokens configured
    for email, _ in _USER_ENV_PREFIX.items():
        user = get_user_by_email(email)
        if not user:
            continue
        if _build_creds_map(user["id"]):
            try:
                _start_runner(user["id"])
                print(f"[autostart] runner restarted for {email}")
            except Exception as exc:
                print(f"[autostart] failed to restart runner: {exc}")
            return

_autostart_runner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print(f"API Server starting on http://0.0.0.0:{port}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
