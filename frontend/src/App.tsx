import { useState, useEffect } from 'react';
import {
    Activity,
    Play,
    Square,
    Settings2,
    TrendingUp,
    TrendingDown,
    History,
    AlertTriangle,
    BarChart3,
    Info,
    Scale,
    Clock,
    X,
    Cpu,
    Target,
} from 'lucide-react';

// API Base URL - FIXED TO PORT 5000
const API_BASE = 'http://localhost:5000';

interface Trade {
    id: number;
    entry_time: string;
    exit_time: string;
    instrument: string;
    direction: number;
    entry_price: number;
    exit_price: number;
    exit_reason: string;
    raw_pl: number;
    pl_R: number;
    entry_units?: number;
}

interface Stats {
    daily_pnl: number;
    trades_today: number;
    wins: number;
    losses: number;
}

type DisplayTrade = Trade & {
    type: string;
    profit: number;
    date: string;
    duration: string;
    reason: string;
    size: string;
    stock: string;
};

const App = () => {
    // State
    const [globalStats, setGlobalStats] = useState<Stats>({ daily_pnl: 0, trades_today: 0, wins: 0, losses: 0 });
    const [trades, setTrades] = useState<Trade[]>([]);
    const [selectedTrade, setSelectedTrade] = useState<DisplayTrade | null>(null);
    const [botRunning, setBotRunning] = useState(false);
    const [currentProfile, setCurrentProfile] = useState<string>('nas_a');
    const [currentInstrument, setCurrentInstrument] = useState<string>('NAS100_USD');
    const [isGlobalKillSwitchActive, setIsGlobalKillSwitchActive] = useState(false);

    // Profile mapping
    const profileNames: Record<string, string> = {
        'nas_a': 'NASDAQ-100',
        'xau_a': 'Gold',
        'xag_a': 'Silver',
        'xcu_a': 'Copper'
    };

    // Fetch Functions
    const fetchStats = async () => {
        try {
            const response = await fetch(`${API_BASE}/api/stats`);
            const data = await response.json();
            setGlobalStats(data);
        } catch (error) {
            console.error('Failed to fetch stats:', error);
        }
    };

    const fetchTrades = async () => {
        try {
            const response = await fetch(`${API_BASE}/api/trades`);
            const data = await response.json();
            setTrades(data);
        } catch (error) {
            console.error('Failed to fetch trades:', error);
        }
    };

    const checkHealth = async () => {
        try {
            const response = await fetch(`${API_BASE}/api/health`);
            const data = await response.json();
            setBotRunning(data.bot_running);
            if (data.config?.profile) {
                setCurrentProfile(data.config.profile);
            }
            if (data.config?.instrument) {
                setCurrentInstrument(data.config.instrument);
            }
        } catch (error) {
            setBotRunning(false);
        }
    };

    const toggleBot = async () => {
        if (botRunning) {
            // Stop bot
            try {
                await fetch(`${API_BASE}/api/bot/stop`, { method: 'POST' });
                setTimeout(checkHealth, 1000);
            } catch (error) {
                console.error('Failed to stop bot:', error);
            }
        } else {
            // Start bot with current profile
            try {
                await fetch(`${API_BASE}/api/bot/start`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile: currentProfile })
                });
                setTimeout(checkHealth, 1000);
            } catch (error) {
                console.error('Failed to start bot:', error);
            }
        }
    };

    const killBot = async () => {
        if (botRunning) {
            try {
                await fetch(`${API_BASE}/api/bot/stop`, { method: 'POST' });
                setIsGlobalKillSwitchActive(true);
                setTimeout(() => {
                    checkHealth();
                    setIsGlobalKillSwitchActive(false);
                }, 1000);
            } catch (error) {
                console.error('Failed to kill bot:', error);
            }
        }
    };

    // Initial load and polling
    useEffect(() => {
        const loadData = async () => {
            await Promise.all([fetchStats(), fetchTrades(), checkHealth()]);
        };
        loadData();

        const interval = setInterval(() => {
            fetchStats();
            fetchTrades();
            checkHealth();
        }, 5000);

        return () => clearInterval(interval);
    }, []);

    const totalPL = globalStats.daily_pnl;
    const winRate = globalStats.trades_today > 0 
        ? Math.round((globalStats.wins / globalStats.trades_today) * 100) 
        : 0;

    // Convert trades for display
    const pastTrades: DisplayTrade[] = trades.slice(0, 10).map(trade => ({
        ...trade,
        type: trade.direction > 0 ? 'Long' : 'Short',
        profit: trade.raw_pl,
        date: new Date(trade.exit_time).toLocaleDateString(),
        duration: Math.floor((new Date(trade.exit_time).getTime() - new Date(trade.entry_time).getTime()) / 60000) + 'm',
        reason: trade.exit_reason,
        size: `${Math.abs(trade.entry_units || 0)} units`,
        stock: trade.instrument
    }));

    return (
        <div className="min-h-screen bg-slate-950 text-slate-200 font-sans p-4 md:p-8">
            {/* Header */}
            <header className="max-w-7xl mx-auto flex flex-col md:flex-row justify-between items-center gap-6 mb-10">
                <div>
                    <h1 className="text-3xl font-bold bg-gradient-to-r from-emerald-400 to-blue-500 bg-clip-text text-transparent italic tracking-tight">
                        MOMENTUM ENGINE
                    </h1>
                    <p className="text-slate-500 text-[10px] font-mono uppercase tracking-[0.3em]">
                        Live Trading System v4.2 • OANDA Practice
                    </p>
                </div>

                <div className="flex items-center gap-6">
                    <div className="text-right">
                        <p className="text-[10px] text-slate-500 font-bold uppercase tracking-wider">Daily P/L</p>
                        <p className={`text-2xl font-mono font-bold ${totalPL >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                            ${totalPL.toFixed(2)}
                        </p>
                    </div>
                    <button
                        onClick={killBot}
                        disabled={!botRunning}
                        className={`px-6 py-3 rounded-lg font-black tracking-tighter transition-all flex items-center gap-2 ${
                            !botRunning ? 'bg-slate-800 text-slate-500 cursor-not-allowed' : 'bg-rose-600 hover:bg-rose-500 text-white shadow-lg shadow-rose-900/30'
                        }`}
                    >
                        <AlertTriangle size={18} />
                        {isGlobalKillSwitchActive ? 'STOPPING...' : 'KILL SWITCH'}
                    </button>
                </div>
            </header>

            <main className="max-w-7xl mx-auto grid grid-cols-1 lg:grid-cols-12 gap-8">
                {/* Main Bot Card + Stats */}
                <div className="lg:col-span-8 space-y-8">
                    {/* Single Momentum Bot Card */}
                    <section>
                        <h2 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.2em] mb-4 flex items-center gap-2">
                            <BarChart3 size={14} /> Active Strategy
                        </h2>
                        
                        <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 hover:border-slate-600 transition-all">
                            <div className="flex justify-between items-start mb-6">
                                <div>
                                    <h3 className="text-2xl font-bold text-white mb-2">
                                        Momentum Bot
                                    </h3>
                                    <div className="flex items-center gap-2">
                                        <span className="text-[10px] font-mono bg-blue-500/10 text-blue-400 px-2 py-0.5 rounded border border-blue-500/20">
                                            {currentInstrument}
                                        </span>
                                        <span className={`text-[10px] font-mono px-2 py-0.5 rounded ${
                                            botRunning 
                                                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' 
                                                : 'bg-slate-800 text-slate-500 border border-slate-700'
                                        }`}>
                                            {botRunning ? 'ACTIVE' : 'IDLE'}
                                        </span>
                                    </div>
                                </div>
                                <button
                                    onClick={toggleBot}
                                    className={`p-3 rounded-lg transition-all ${
                                        botRunning
                                            ? 'bg-rose-500/10 text-rose-500 hover:bg-rose-600 hover:text-white'
                                            : 'bg-emerald-500/10 text-emerald-500 hover:bg-emerald-600 hover:text-white'
                                    }`}
                                >
                                    {botRunning ? <Square size={24} fill="currentColor" /> : <Play size={24} fill="currentColor" />}
                                </button>
                            </div>

                            <div className="grid grid-cols-3 gap-4">
                                <div>
                                    <p className="text-[9px] text-slate-500 font-bold uppercase tracking-tighter mb-2">Total P/L</p>
                                    <p className={`text-2xl font-mono font-bold ${totalPL >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                        ${totalPL.toFixed(2)}
                                    </p>
                                </div>
                                <div>
                                    <p className="text-[9px] text-slate-500 font-bold uppercase tracking-tighter mb-2">Win Rate</p>
                                    <p className="text-2xl font-mono font-bold text-slate-300">{winRate}%</p>
                                </div>
                                <div>
                                    <p className="text-[9px] text-slate-500 font-bold uppercase tracking-tighter mb-2">Trades</p>
                                    <p className="text-2xl font-mono font-bold text-white">{globalStats.trades_today}</p>
                                </div>
                            </div>

                            {botRunning && (
                                <div className="mt-6 pt-6 border-t border-slate-800">
                                    <div className="flex items-center justify-between text-xs">
                                        <span className="text-slate-500">Current Profile</span>
                                        <span className="text-blue-400 font-mono font-bold">
                                            {profileNames[currentProfile] || currentProfile}
                                        </span>
                                    </div>
                                </div>
                            )}
                        </div>
                    </section>

                    {/* Performance Stats */}
                    <section className="grid grid-cols-3 gap-4">
                        <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
                            <p className="text-[9px] text-slate-500 font-bold uppercase tracking-tighter mb-2">Trades Today</p>
                            <p className="text-3xl font-mono font-bold text-white">{globalStats.trades_today}</p>
                        </div>
                        <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
                            <p className="text-[9px] text-slate-500 font-bold uppercase tracking-tighter mb-2">Wins / Losses</p>
                            <p className="text-3xl font-mono font-bold text-blue-400">
                                {globalStats.wins} / {globalStats.losses}
                            </p>
                        </div>
                        <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
                            <p className="text-[9px] text-slate-500 font-bold uppercase tracking-tighter mb-2">Success Rate</p>
                            <p className="text-3xl font-mono font-bold text-emerald-400">
                                {winRate}%
                            </p>
                        </div>
                    </section>
                </div>

                {/* Trade History Sidebar */}
                <div className="lg:col-span-4">
                    <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 h-full flex flex-col">
                        <h2 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.2em] mb-6 flex items-center gap-2">
                            <History size={14} /> Trade Archive
                        </h2>
                        <div className="space-y-4 overflow-y-auto pr-2 flex-1">
                            {pastTrades.length === 0 ? (
                                <p className="text-slate-500 text-center py-8 text-sm">No trades yet</p>
                            ) : (
                                pastTrades.map(trade => (
                                    <div
                                        key={trade.id}
                                        onClick={() => setSelectedTrade(trade)}
                                        className="p-4 bg-slate-950 border border-slate-800 rounded-xl cursor-pointer hover:border-slate-500 transition-all group"
                                    >
                                        <div className="flex justify-between mb-2">
                                            <div className="flex items-center gap-2">
                                                <span className="text-xs font-bold text-white group-hover:text-blue-400">{trade.stock}</span>
                                                <span className={`text-[8px] font-black px-1.5 py-0.5 rounded ${
                                                    trade.profit >= 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'
                                                }`}>
                                                    {trade.profit >= 0 ? 'WIN' : 'LOSS'}
                                                </span>
                                            </div>
                                            <span className={`text-xs font-mono font-bold ${
                                                trade.profit >= 0 ? 'text-emerald-400' : 'text-rose-400'
                                            }`}>
                                                {trade.profit > 0 ? '+' : ''}{trade.profit.toFixed(2)}
                                            </span>
                                        </div>
                                        <div className="flex justify-between items-center text-[10px] text-slate-500 font-mono">
                                            <span>{trade.reason}</span>
                                            <span>{trade.date}</span>
                                        </div>
                                    </div>
                                ))
                            )}
                        </div>
                        <div className="mt-auto pt-6 border-t border-slate-800">
                            <div className="flex items-center justify-between text-xs text-slate-500 mb-2">
                                <span>System Status</span>
                                <span className={`font-mono ${botRunning ? 'text-emerald-400' : 'text-slate-500'}`}>
                                    {botRunning ? 'ACTIVE' : 'IDLE'}
                                </span>
                            </div>
                            <div className="flex items-center justify-between text-xs text-slate-500">
                                <span>API Connection</span>
                                <span className="text-blue-400 font-mono">OANDA</span>
                            </div>
                        </div>
                    </div>
                </div>
            </main>

            {/* Trade Detail Modal */}
            {selectedTrade && (
                <div className="fixed inset-0 z-[120] flex items-center justify-center p-4 bg-slate-950/90 backdrop-blur-xl overflow-y-auto">
                    <div className="bg-slate-900 border border-slate-800 w-full max-w-2xl rounded-3xl shadow-[0_0_50px_rgba(0,0,0,0.5)] overflow-hidden">
                        <div className={`p-8 border-b border-slate-800 flex justify-between items-start ${
                            selectedTrade.profit >= 0 ? 'bg-emerald-600/5' : 'bg-rose-600/5'
                        }`}>
                            <div>
                                <div className="flex items-center gap-4 mb-2">
                                    <h3 className="text-3xl font-black text-white">{selectedTrade.stock}</h3>
                                    <span className={`px-3 py-1 rounded-lg text-[10px] font-black tracking-widest uppercase ${
                                        selectedTrade.type === 'Long' ? 'bg-emerald-500 text-slate-950' : 'bg-rose-500 text-slate-950'
                                    }`}>
                                        {selectedTrade.type}
                                    </span>
                                </div>
                                <div className="flex items-center gap-4 text-slate-400 text-xs">
                                    <span className="flex items-center gap-1.5"><Cpu size={14} /> ID: {selectedTrade.id}</span>
                                    <span className="flex items-center gap-1.5"><Clock size={14} /> {selectedTrade.duration}</span>
                                </div>
                            </div>
                            <button onClick={() => setSelectedTrade(null)} className="p-2 hover:bg-slate-800 rounded-xl text-slate-500 hover:text-white transition-all">
                                <X size={24} />
                            </button>
                        </div>

                        <div className="p-8 space-y-10">
                            <div className="flex flex-col items-center justify-center">
                                <p className="text-[10px] text-slate-500 uppercase font-black tracking-[0.2em] mb-2">Realized P/L</p>
                                <p className={`text-7xl font-mono font-black tracking-tighter ${
                                    selectedTrade.profit >= 0 ? 'text-emerald-400' : 'text-rose-400'
                                }`}>
                                    ${selectedTrade.profit.toFixed(2)}
                                </p>
                                <div className="mt-4 flex items-center gap-3">
                                    <span className="text-[10px] font-bold text-slate-400 bg-slate-800 px-4 py-1.5 rounded-full border border-slate-700">
                                        {selectedTrade.reason}
                                    </span>
                                    <span className="text-[10px] font-bold text-slate-500 uppercase">
                                        R: {selectedTrade.pl_R?.toFixed(2)}
                                    </span>
                                </div>
                            </div>

                            <div className="grid grid-cols-2 md:grid-cols-4 gap-6 bg-slate-950/50 p-6 rounded-2xl border border-slate-800/50">
                                <div className="space-y-1">
                                    <p className="text-[9px] text-slate-500 uppercase font-bold flex items-center gap-1">
                                        <Target size={12} /> Entry
                                    </p>
                                    <p className="text-base font-mono font-bold text-white">${selectedTrade.entry_price.toFixed(2)}</p>
                                </div>
                                <div className="space-y-1">
                                    <p className="text-[9px] text-slate-500 uppercase font-bold flex items-center gap-1">
                                        <Info size={12} /> Exit
                                    </p>
                                    <p className="text-base font-mono font-bold text-white">${selectedTrade.exit_price.toFixed(2)}</p>
                                </div>
                                <div className="space-y-1">
                                    <p className="text-[9px] text-slate-500 uppercase font-bold flex items-center gap-1">
                                        <Clock size={12} /> Duration
                                    </p>
                                    <p className="text-base font-mono font-bold text-white">{selectedTrade.duration}</p>
                                </div>
                                <div className="space-y-1">
                                    <p className="text-[9px] text-slate-500 uppercase font-bold flex items-center gap-1">
                                        <Scale size={12} /> Size
                                    </p>
                                    <p className="text-base font-mono font-bold text-white">{selectedTrade.size}</p>
                                </div>
                            </div>
                        </div>

                        <div className="p-8 bg-slate-950/50 border-t border-slate-800">
                            <button
                                onClick={() => setSelectedTrade(null)}
                                className="w-full bg-slate-800 hover:bg-slate-700 text-slate-400 font-bold py-4 rounded-2xl transition-all uppercase tracking-widest text-xs"
                            >
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default App;
