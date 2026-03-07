import json
import os
import sys
import signal
import sqlite3
import subprocess
import tempfile
from functools import wraps
from pathlib import Path

# Ensure project root is on sys.path however the server is invoked
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import bcrypt
from dotenv import load_dotenv
from flask import Flask, jsonify, request, g
from flask_cors import CORS

# Load .env from project root
load_dotenv(_project_root / ".env")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
CORS(app, supports_credentials=True, origins="*")

DB_PATH              = _project_root / "database" / "trades.db"
BOT_SCRIPT           = _project_root / "bot" / "momentum_trader_live.py"
STRATEGY_STATE_FILE  = _project_root / "strategy_state.json"
STRATEGY_RUNNER      = _project_root / "strategies" / "runner.py"

_DEFAULT_STRATEGY_STATE = {
    "stat_arb":    {"enabled": False},
    "momentum":    {"enabled": False},
    "vol_premium": {"enabled": False},
}

# Global strategy runner process
strategy_runner_process = None

from database.database import (
    init_db, seed_users,
    get_user_by_email, get_session, create_session, delete_session,
    get_user_token, upsert_user_token,
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
    row = get_user_token(g.user_id)
    if not row:
        return jsonify({"configured": False})
    return jsonify({
        "configured": True,
        "oanda_account_id": row["oanda_account_id"],
        "oanda_account_type": row["oanda_account_type"],
        "oanda_access_token_set": bool(row["oanda_access_token"]),
    })


@app.route("/api/user/tokens", methods=["POST"])
@require_auth
def save_tokens():
    data = request.get_json() or {}
    account_id = (data.get("oanda_account_id") or "").strip()
    access_token = (data.get("oanda_access_token") or "").strip()
    account_type = (data.get("oanda_account_type") or "practice").strip()

    if not account_id or not access_token:
        return jsonify({"error": "oanda_account_id and oanda_access_token are required"}), 400

    upsert_user_token(g.user_id, account_id, access_token, account_type)
    return jsonify({"status": "saved"})


# ---------------------------------------------------------------------------
# Credential resolution for bot
# ---------------------------------------------------------------------------

def _resolve_credentials(user_id: int):
    row = get_user_token(user_id)
    if row:
        return row["oanda_account_id"], row["oanda_access_token"], row["oanda_account_type"]

    # Fall back to .env
    account_id = os.environ.get("OANDA_ACCOUNT_ID")
    access_token = os.environ.get("OANDA_ACCESS_TOKEN")
    account_type = os.environ.get("OANDA_ACCOUNT_TYPE", "practice")
    if account_id and access_token:
        return account_id, access_token, account_type

    return None


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
    try:
        if STRATEGY_STATE_FILE.exists():
            return json.loads(STRATEGY_STATE_FILE.read_text())
    except Exception:
        pass
    return dict(_DEFAULT_STRATEGY_STATE)


def _save_strategy_state(state: dict) -> None:
    STRATEGY_STATE_FILE.write_text(json.dumps(state, indent=2))


def _start_runner(user_id: int) -> None:
    global strategy_runner_process
    if is_runner_running():
        return
    creds = _resolve_credentials(user_id)
    if not creds:
        raise ValueError("No OANDA credentials configured. Add your API token in Settings.")
    account_id, access_token, account_type = creds
    cfg_path = _write_temp_cfg(account_id, access_token, account_type)
    cmd = [
        sys.executable, str(STRATEGY_RUNNER),
        "--config", cfg_path,
        "--state",  str(STRATEGY_STATE_FILE),
    ]
    strategy_runner_process = subprocess.Popen(
        cmd,
        cwd=str(_project_root),
        env={**__import__("os").environ, "PYTHONPATH": str(_project_root)},
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

    creds = _resolve_credentials(g.user_id)
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
        "status": "ok",
        "bot_running": running,
        "pid": bot_process.pid if running else None,
        "config": current_config,
    })


# ---------------------------------------------------------------------------
# Strategy routes
# ---------------------------------------------------------------------------

VALID_STRATEGIES = {"stat_arb", "momentum", "vol_premium"}


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
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    seed_users(SEED_USERS)
    print("=" * 60)
    print("API Server starting on http://localhost:5000")
    print("Dashboard: http://localhost:3000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
