from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlite3
import subprocess
import signal
import sys
from pathlib import Path

app = Flask(__name__)
CORS(app)

DB_PATH = Path(__file__).parent.parent / "database" / "trades.db"
BOT_SCRIPT = Path(__file__).parent.parent / "src" / "momentum_trader_live.py"

# Global variable to track bot process
bot_process = None
current_config = None

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def is_bot_running():
    """Check if bot process is alive"""
    global bot_process
    if bot_process is None:
        return False
    return bot_process.poll() is None

@app.route('/api/trades', methods=['GET'])
def get_trades():
    """Get all trades"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades ORDER BY exit_time DESC LIMIT 50")
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(trades)

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get daily statistics"""
    conn = get_db()
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

@app.route('/api/bot/start', methods=['POST'])
def start_bot():
    """Start the trading bot with profile or custom parameters"""
    global bot_process, current_config
    
    if is_bot_running():
        return jsonify({"error": "Bot is already running", "pid": bot_process.pid}), 400
    
    data = request.get_json() or {}
    
    try:
        python_exe = sys.executable
        
        # Check if using a profile or custom settings
        profile = data.get('profile')
        
        if profile:
            # Use profile mode
            cmd = [
                python_exe, str(BOT_SCRIPT),
                '--profile', profile
            ]
            print(f"🚀 Starting bot with profile: {profile}")
        else:
            # Use custom parameters mode
            instrument = data.get('instrument', 'NAS100_USD')
            bar_length = data.get('bar_length', '3min')
            units = data.get('units', 1)
            threshold_k = data.get('threshold_k', 1.8)
            per_trade_sl = data.get('per_trade_sl', 20.0)
            per_trade_tp = data.get('per_trade_tp', 60.0)
            
            cmd = [
                python_exe, str(BOT_SCRIPT),
                '--instrument', instrument,
                '--bar-length', bar_length,
                '--units', str(units),
                '--threshold-k', str(threshold_k),
                '--per-trade-sl', str(per_trade_sl),
                '--per-trade-tp', str(per_trade_tp),
                '--use-session-filter',
                '--session-start-hour', '13',
                '--session-end-hour', '21',
            ]
            print(f"🚀 Starting bot with custom settings")
            print(f"   Instrument: {instrument}")
            print(f"   Bar Length: {bar_length}")
            print(f"   Units: {units}")
            print(f"   SL: {per_trade_sl}, TP: {per_trade_tp}")
        
        bot_process = subprocess.Popen(
            cmd,
            cwd=str(BOT_SCRIPT.parent.parent),
        )
        
        current_config = data
        
        print(f"✅ Bot started with PID {bot_process.pid}")
        
        return jsonify({
            "status": "started",
            "pid": bot_process.pid,
            "config": data
        })
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/bot/stop', methods=['POST'])
def stop_bot():
    """Stop the trading bot"""
    global bot_process, current_config
    
    if not is_bot_running():
        bot_process = None
        return jsonify({"error": "Bot is not running"}), 400
    
    try:
        print(f"🛑 Stopping bot (PID {bot_process.pid})...")
        bot_process.send_signal(signal.SIGINT)
        
        try:
            bot_process.wait(timeout=10)
            print(f"✅ Bot stopped gracefully")
        except subprocess.TimeoutExpired:
            print(f"⚠️  Bot didn't stop gracefully, force killing...")
            bot_process.kill()
            bot_process.wait()
            print(f"✅ Bot force stopped")
        
        bot_process = None
        current_config = None
        return jsonify({"status": "stopped"})
    except Exception as e:
        print(f"❌ Error stopping bot: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/bot/config', methods=['GET'])
def get_config():
    """Get current bot configuration"""
    global current_config
    return jsonify(current_config or {})

@app.route('/api/health', methods=['GET'])
def health():
    """Health check - returns bot status"""
    running = is_bot_running()
    result = {
        "status": "ok",
        "bot_running": running,
        "pid": bot_process.pid if running else None,
        "config": current_config
    }
    return jsonify(result)

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 API Server starting on http://localhost:5000")
    print("📊 Dashboard: http://localhost:3000")
    print("=" * 60)
    
    # Auto-start bot with nas_a profile
    print("\n🤖 Auto-starting trading bot...")
    try:
        python_exe = sys.executable
        
        default_config = {
            'profile': 'nas_a'
        }
        
        cmd = [
            python_exe, str(BOT_SCRIPT),
            '--profile', 'nas_a'
        ]
        
        bot_process = subprocess.Popen(
            cmd,
            cwd=str(BOT_SCRIPT.parent.parent),
        )
        
        current_config = default_config
        print(f"✅ Bot auto-started with profile nas_a (PID {bot_process.pid})\n")
    except Exception as e:
        print(f"❌ Failed to auto-start bot: {e}\n")
    
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)