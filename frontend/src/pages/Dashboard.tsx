import { useState, useEffect } from 'react';
import {
    Play, Square, History, AlertTriangle,
    BarChart3, LogOut, Settings, TrendingUp, Zap,
} from 'lucide-react';
import { logout, type AuthSession } from '../auth';
import { apiFetch } from '../api';
import TokenSettings from '../components/TokenSettings';
import TradeModal from '../components/TradeModal';
import type { Trade, Stats, DisplayTrade, StrategiesResponse, OpenTrade, AccountData, AlltimeStats } from '../types';

const STRATEGY_META = [
    {
        key: 'stat_arb' as const,
        label: 'Stat Arb',
        subtitle: 'Pairs Trading',
        description: 'Log-spread z-score on XAU/XAG and SPX/BCO. Entry at ±2σ, exit at 0.5σ. 1.5% NAV per leg.',
        color: 'violet',
    },
    {
        key: 'momentum' as const,
        label: 'Momentum',
        subtitle: 'RSI + Volume',
        description: 'RSI extremes with 1.8× volume confirmation and 200MA trend filter on SPX & Gold (H1).',
        color: 'blue',
    },
    {
        key: 'vol_premium' as const,
        label: 'Vol Premium',
        subtitle: 'Short Volatility',
        description: 'Fades IV spikes on SPX when IV/RV ratio ≥ 1.15. Hard kill at 2.0× or VIX > 30.',
        color: 'amber',
    },
    {
        key: 'crypto' as const,
        label: 'Crypto',
        subtitle: 'BTC, ETH & SOL',
        description: 'RSI momentum + 50MA trend filter on BTC/ETH/SOL (H1). Runs 24/7. 2% NAV per trade.',
        color: 'emerald',
    },
    {
        key: 'daily_target' as const,
        label: 'Daily Target',
        subtitle: '+2% / Day',
        description: 'RSI + 20MA on M15 across EUR, GBP, NAS100, XAU, SPX. Stops at +2% daily P&L, halts at -3% loss.',
        color: 'rose',
    },
] as const;

type StrategyKey = typeof STRATEGY_META[number]['key'];

const STRATEGY_COLORS: Record<string, { badge: string; toggle: string; glow: string }> = {
    violet: {
        badge:  'bg-violet-100 text-violet-700 border-violet-200',
        toggle: 'bg-violet-600 hover:bg-violet-700',
        glow:   'shadow-violet-100',
    },
    blue: {
        badge:  'bg-blue-100 text-blue-700 border-blue-200',
        toggle: 'bg-blue-600 hover:bg-blue-700',
        glow:   'shadow-blue-100',
    },
    amber: {
        badge:  'bg-amber-100 text-amber-700 border-amber-200',
        toggle: 'bg-amber-500 hover:bg-amber-600',
        glow:   'shadow-amber-100',
    },
    emerald: {
        badge:  'bg-emerald-100 text-emerald-700 border-emerald-200',
        toggle: 'bg-emerald-600 hover:bg-emerald-700',
        glow:   'shadow-emerald-100',
    },
    rose: {
        badge:  'bg-rose-100 text-rose-700 border-rose-200',
        toggle: 'bg-rose-600 hover:bg-rose-700',
        glow:   'shadow-rose-100',
    },
};

const PROFILES = [
    { key: 'nas_a',  label: 'NASDAQ-100', instrument: 'NAS100_USD', color: 'blue'   },
    { key: 'xau_a',  label: 'Gold',        instrument: 'XAU_USD',    color: 'amber'  },
    { key: 'xag_a',  label: 'Silver',      instrument: 'XAG_USD',    color: 'slate'  },
    { key: 'xcu_a',  label: 'Copper',      instrument: 'XCU_USD',    color: 'orange' },
] as const;

type ProfileKey = typeof PROFILES[number]['key'];

const PROFILE_STYLES: Record<string, { active: string; idle: string }> = {
    blue:   { active: 'bg-blue-600 text-white border-blue-600',   idle: 'bg-white text-blue-600 border-blue-200 hover:border-blue-400 hover:bg-blue-50' },
    amber:  { active: 'bg-amber-500 text-white border-amber-500', idle: 'bg-white text-amber-600 border-amber-200 hover:border-amber-400 hover:bg-amber-50' },
    slate:  { active: 'bg-slate-600 text-white border-slate-600', idle: 'bg-white text-slate-600 border-slate-200 hover:border-slate-400 hover:bg-slate-50' },
    orange: { active: 'bg-orange-500 text-white border-orange-500', idle: 'bg-white text-orange-600 border-orange-200 hover:border-orange-400 hover:bg-orange-50' },
};

function toDisplayTrade(t: Trade): DisplayTrade {
    return {
        ...t,
        type: t.direction > 0 ? 'Long' : 'Short',
        profit: t.raw_pl,
        date: new Date(t.exit_time).toLocaleDateString(),
        duration: Math.floor(
            (new Date(t.exit_time).getTime() - new Date(t.entry_time).getTime()) / 60000
        ) + 'm',
        reason: t.exit_reason,
        size: `${Math.abs(t.entry_units || 0)} units`,
        stock: t.instrument,
    };
}

export default function Dashboard({ session }: { session: AuthSession }) {
    const [stats, setStats] = useState<Stats>({ daily_pnl: 0, trades_today: 0, wins: 0, losses: 0 });
    const [trades, setTrades] = useState<Trade[]>([]);
    const [selectedTrade, setSelectedTrade] = useState<DisplayTrade | null>(null);
    const [botRunning, setBotRunning] = useState(false);
    const [currentProfile, setCurrentProfile] = useState<ProfileKey>('nas_a');
    const [isKillActive, setIsKillActive] = useState(false);
    const [showSettings, setShowSettings] = useState(false);
    const [strategies, setStrategies] = useState<StrategiesResponse>({
        runner_running: false,
        strategies: {
            stat_arb:      { enabled: false },
            momentum:      { enabled: false },
            vol_premium:   { enabled: false },
            crypto:        { enabled: false },
            daily_target:  { enabled: false },
        },
    });
    const [togglingStrategy, setTogglingStrategy] = useState<StrategyKey | null>(null);
    const [strategyError, setStrategyError] = useState<string | null>(null);
    const [openTrades, setOpenTrades] = useState<OpenTrade[]>([]);
    const [accountData, setAccountData] = useState<AccountData>({});
    const [closingTrade, setClosingTrade] = useState<string | null>(null);
    const [environment, setEnvironment] = useState<string>('staging');

    const fetchStats = async () => {
        try { const r = await apiFetch('/api/stats'); if (r.ok) setStats(await r.json()); } catch {}
    };
    const fetchTrades = async () => {
        try { const r = await apiFetch('/api/trades'); if (r.ok) setTrades(await r.json()); } catch {}
    };
    const checkHealth = async () => {
        try {
            const r = await apiFetch('/api/health');
            if (r.ok) {
                const d = await r.json();
                setBotRunning(d.bot_running);
                if (d.config?.profile) setCurrentProfile(d.config.profile as ProfileKey);
                if (d.environment) setEnvironment(d.environment);
            }
        } catch { setBotRunning(false); }
    };
    const fetchOpenTrades = async () => {
        try { const r = await apiFetch('/api/open_trades'); if (r.ok) setOpenTrades(await r.json()); } catch {}
    };
    const fetchAccount = async () => {
        try { const r = await apiFetch('/api/account'); if (r.ok) setAccountData(await r.json()); } catch {}
    };
    const closeTrade = async (tradeKey: string) => {
        setClosingTrade(tradeKey);
        try {
            const r = await apiFetch(`/api/open_trades/${encodeURIComponent(tradeKey)}/close`, { method: 'POST' });
            if (r.ok) {
                await fetchOpenTrades();
                await fetchAccount();
            } else {
                const d = await r.json().catch(() => ({}));
                setStrategyError(d.error ?? `Failed to close trade (${r.status})`);
            }
        } catch {
            setStrategyError('Network error — could not close trade');
        }
        setClosingTrade(null);
    };
    const fetchStrategies = async () => {
        try {
            const r = await apiFetch('/api/strategies');
            if (r.ok) setStrategies(await r.json());
        } catch {}
    };
    const toggleStrategy = async (key: StrategyKey) => {
        setTogglingStrategy(key);
        setStrategyError(null);
        try {
            const r = await apiFetch(`/api/strategies/${key}/toggle`, { method: 'POST' });
            const data = await r.json();
            if (!r.ok) {
                setStrategyError(data.error ?? 'Failed to toggle strategy');
            }
            // Always refresh — the response includes the current state
            if (data.strategies) {
                setStrategies({ runner_running: data.runner_running ?? false, strategies: data.strategies });
            } else {
                await fetchStrategies();
            }
        } catch {
            setStrategyError('Network error — could not reach the server');
            await fetchStrategies();
        }
        setTogglingStrategy(null);
    };

    const toggleBot = async () => {
        if (botRunning) {
            await apiFetch('/api/bot/stop', { method: 'POST' });
        } else {
            await apiFetch('/api/bot/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profile: currentProfile }),
            });
        }
        setTimeout(checkHealth, 1000);
    };

    const killBot = async () => {
        if (!botRunning) return;
        await apiFetch('/api/bot/stop', { method: 'POST' });
        setIsKillActive(true);
        setTimeout(() => { checkHealth(); setIsKillActive(false); }, 1000);
    };

    useEffect(() => {
        Promise.all([fetchStats(), fetchTrades(), checkHealth(), fetchStrategies(), fetchOpenTrades(), fetchAccount()]);
        const id = setInterval(() => { fetchStats(); fetchTrades(); checkHealth(); fetchStrategies(); fetchOpenTrades(); fetchAccount(); }, 10000);
        return () => clearInterval(id);
    }, []);

    const totalPL = stats.daily_pnl;
    const winRate = stats.trades_today > 0
        ? Math.round((stats.wins / stats.trades_today) * 100) : 0;
    const at: AlltimeStats = stats.alltime ?? {
        total_trades: 0, total_pl: 0, total_wins: 0, total_losses: 0,
        win_rate: 0, avg_win: 0, avg_loss: 0, profit_factor: null,
    };
    const pastTrades = trades.map(toDisplayTrade);
    const activeProfile = PROFILES.find(p => p.key === currentProfile) ?? PROFILES[0];

    return (
        <div className="min-h-screen bg-slate-100 text-slate-900 font-sans">
            {showSettings && <TokenSettings onClose={() => setShowSettings(false)} />}
            {selectedTrade && <TradeModal trade={selectedTrade} onClose={() => setSelectedTrade(null)} />}

            {/* Header */}
            <header className="bg-white border-b border-slate-200 px-4 md:px-8 py-4">
                <div className="max-w-7xl mx-auto flex flex-col md:flex-row justify-between items-center gap-4">
                    <div>
                        <div className="flex items-center gap-2">
                            <h1 className="text-2xl font-bold bg-gradient-to-r from-emerald-500 to-blue-600 bg-clip-text text-transparent italic tracking-tight">
                                MOMENTUM ENGINE
                            </h1>
                            {environment === 'production' ? (
                                <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-rose-100 text-rose-700 border border-rose-300 animate-pulse">
                                    LIVE
                                </span>
                            ) : (
                                <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-amber-100 text-amber-700 border border-amber-300">
                                    STAGING
                                </span>
                            )}
                        </div>
                        <p className="text-slate-400 text-[10px] font-mono uppercase tracking-[0.3em]">
                            {environment === 'production' ? 'Live Trading System • OANDA Live' : 'Staging • OANDA Practice'}
                        </p>
                    </div>

                    <div className="flex items-center gap-3">
                        <div className="text-right mr-2">
                            <p className="text-[10px] text-slate-400 font-bold uppercase tracking-wider">Daily P/L</p>
                            <p className={`text-2xl font-mono font-bold ${totalPL >= 0 ? 'text-emerald-600' : 'text-rose-500'}`}>
                                ${totalPL.toFixed(2)}
                            </p>
                        </div>
                        <button
                            onClick={() => setShowSettings(true)}
                            className="p-2.5 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-500 hover:text-slate-800 transition-all border border-slate-200"
                            title="Settings"
                        >
                            <Settings size={18} />
                        </button>
                        <button
                            onClick={logout}
                            className="p-2.5 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-500 hover:text-slate-800 transition-all border border-slate-200"
                            title="Sign out"
                        >
                            <LogOut size={18} />
                        </button>
                        <button
                            onClick={killBot}
                            disabled={!botRunning}
                            className={`px-5 py-2.5 rounded-lg font-black tracking-tighter transition-all flex items-center gap-2 text-sm ${
                                !botRunning
                                    ? 'bg-slate-100 text-slate-400 cursor-not-allowed border border-slate-200'
                                    : 'bg-rose-500 hover:bg-rose-600 text-white shadow-md shadow-rose-200'
                            }`}
                        >
                            <AlertTriangle size={16} />
                            {isKillActive ? 'STOPPING...' : 'KILL SWITCH'}
                        </button>
                    </div>
                </div>
            </header>

            <main className="max-w-7xl mx-auto p-4 md:p-8 grid grid-cols-1 lg:grid-cols-12 gap-6">

                {/* Left column */}
                <div className="lg:col-span-8 space-y-6">

                    {/* Instrument selector */}
                    <section>
                        <p className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-3 flex items-center gap-2">
                            <TrendingUp size={12} /> Select Instrument
                        </p>
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                            {PROFILES.map(p => {
                                const isSelected = currentProfile === p.key;
                                const styles = PROFILE_STYLES[p.color];
                                return (
                                    <button
                                        key={p.key}
                                        onClick={() => !botRunning && setCurrentProfile(p.key)}
                                        disabled={botRunning && !isSelected}
                                        className={`px-4 py-3 rounded-xl border-2 font-bold text-sm transition-all ${
                                            isSelected
                                                ? styles.active
                                                : botRunning
                                                    ? 'bg-slate-100 text-slate-300 border-slate-200 cursor-not-allowed'
                                                    : styles.idle
                                        }`}
                                    >
                                        <span className="block text-xs font-black tracking-wider">{p.label}</span>
                                        <span className="block text-[10px] font-mono opacity-70 mt-0.5">{p.instrument}</span>
                                    </button>
                                );
                            })}
                        </div>
                        {botRunning && (
                            <p className="text-xs text-slate-400 mt-2">Stop the bot to switch instruments.</p>
                        )}
                    </section>

                    {/* Bot card */}
                    <section>
                        <p className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-3 flex items-center gap-2">
                            <BarChart3 size={12} /> Active Strategy
                        </p>
                        <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
                            <div className="flex justify-between items-start mb-6">
                                <div>
                                    <h3 className="text-xl font-bold text-slate-900 mb-1">Momentum Bot</h3>
                                    <div className="flex items-center gap-2">
                                        <span className="text-[10px] font-mono bg-blue-50 text-blue-600 px-2 py-0.5 rounded-full border border-blue-100">
                                            {activeProfile.instrument}
                                        </span>
                                        <span className={`text-[10px] font-mono px-2 py-0.5 rounded-full border ${
                                            botRunning
                                                ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
                                                : 'bg-slate-100 text-slate-400 border-slate-200'
                                        }`}>
                                            {botRunning ? '● ACTIVE' : '○ IDLE'}
                                        </span>
                                    </div>
                                </div>
                                <button
                                    onClick={toggleBot}
                                    className={`px-5 py-2.5 rounded-xl font-bold text-sm transition-all flex items-center gap-2 ${
                                        botRunning
                                            ? 'bg-rose-50 text-rose-600 hover:bg-rose-500 hover:text-white border border-rose-200'
                                            : 'bg-emerald-500 text-white hover:bg-emerald-600 shadow-md shadow-emerald-100'
                                    }`}
                                >
                                    {botRunning
                                        ? <><Square size={16} fill="currentColor" /> Stop</>
                                        : <><Play size={16} fill="currentColor" /> Start</>}
                                </button>
                            </div>

                            <div className="grid grid-cols-3 gap-4">
                                <div className="bg-slate-50 rounded-xl p-4">
                                    <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-1">Total P/L</p>
                                    <p className={`text-2xl font-mono font-bold ${totalPL >= 0 ? 'text-emerald-600' : 'text-rose-500'}`}>
                                        ${totalPL.toFixed(2)}
                                    </p>
                                </div>
                                <div className="bg-slate-50 rounded-xl p-4">
                                    <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-1">Win Rate</p>
                                    <p className="text-2xl font-mono font-bold text-slate-700">{winRate}%</p>
                                </div>
                                <div className="bg-slate-50 rounded-xl p-4">
                                    <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-1">Trades</p>
                                    <p className="text-2xl font-mono font-bold text-slate-700">{stats.trades_today}</p>
                                </div>
                            </div>
                        </div>
                    </section>

                    {/* Strategy Engine */}
                    <section>
                        <p className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-3 flex items-center gap-2">
                            <Zap size={12} /> Strategy Engine
                            {strategies.runner_running && (
                                <span className="ml-auto text-[9px] font-mono bg-emerald-50 text-emerald-600 border border-emerald-200 px-2 py-0.5 rounded-full">
                                    ● RUNNER ACTIVE
                                </span>
                            )}
                        </p>
                        {strategyError && (
                            <div className="mb-3 flex items-start justify-between gap-2 bg-rose-50 border border-rose-200 rounded-xl px-3 py-2.5">
                                <p className="text-xs text-rose-600">{strategyError}</p>
                                <button onClick={() => setStrategyError(null)} className="text-rose-400 hover:text-rose-600 text-xs font-bold shrink-0">✕</button>
                            </div>
                        )}
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                            {STRATEGY_META.map(s => {
                                const isEnabled  = strategies.strategies[s.key]?.enabled ?? false;
                                const isToggling = togglingStrategy === s.key;
                                const colors     = STRATEGY_COLORS[s.color];
                                return (
                                    <div
                                        key={s.key}
                                        className={`bg-white border rounded-2xl p-4 shadow-sm flex flex-col gap-3 transition-all ${
                                            isEnabled ? `border-slate-200 shadow-md ${colors.glow}` : 'border-slate-200'
                                        }`}
                                    >
                                        <div className="flex items-start justify-between gap-2">
                                            <div>
                                                <p className="text-sm font-black text-slate-900">{s.label}</p>
                                                <p className="text-[10px] font-mono text-slate-400">{s.subtitle}</p>
                                            </div>
                                            <span className={`text-[9px] font-black px-2 py-0.5 rounded-full border whitespace-nowrap ${
                                                isEnabled
                                                    ? colors.badge
                                                    : 'bg-slate-100 text-slate-400 border-slate-200'
                                            }`}>
                                                {isEnabled ? '● ON' : '○ OFF'}
                                            </span>
                                        </div>

                                        <p className="text-[11px] text-slate-500 leading-relaxed flex-1">
                                            {s.description}
                                        </p>

                                        <button
                                            onClick={() => toggleStrategy(s.key)}
                                            disabled={isToggling}
                                            className={`w-full py-2 rounded-xl text-xs font-bold uppercase tracking-wider transition-all ${
                                                isToggling
                                                    ? 'bg-slate-100 text-slate-400 cursor-wait'
                                                    : isEnabled
                                                        ? 'bg-rose-50 text-rose-600 hover:bg-rose-500 hover:text-white border border-rose-200'
                                                        : `${colors.toggle} text-white shadow-sm`
                                            }`}
                                        >
                                            {isToggling ? 'Updating...' : isEnabled ? 'Disable' : 'Enable'}
                                        </button>
                                    </div>
                                );
                            })}
                        </div>
                    </section>

                    {/* Account Summary */}
                    {Object.keys(accountData).length > 0 && (
                        <section>
                            <p className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-3 flex items-center gap-2">
                                <BarChart3 size={12} /> Account Summary
                            </p>
                            <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
                                {(['stat_arb', 'momentum', 'vol_premium', 'crypto'] as const).map(key => {
                                    const acct = accountData[key];
                                    if (!acct || acct.error) return null;
                                    const label = key === 'stat_arb' ? 'Stat Arb' : key === 'momentum' ? 'Momentum' : key === 'vol_premium' ? 'Vol Premium' : 'Crypto';
                                    const plColor = acct.unrealized_pl >= 0 ? 'text-emerald-600' : 'text-rose-500';
                                    return (
                                        <div key={key} className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
                                            <p className="text-[9px] font-black text-slate-400 uppercase tracking-wider mb-3">{label}</p>
                                            <div className="space-y-2">
                                                <div className="flex justify-between items-center">
                                                    <span className="text-[10px] text-slate-500">Balance</span>
                                                    <span className="font-mono font-bold text-slate-800 text-sm">${acct.balance.toFixed(2)}</span>
                                                </div>
                                                <div className="flex justify-between items-center">
                                                    <span className="text-[10px] text-slate-500">Equity (NAV)</span>
                                                    <span className="font-mono font-bold text-slate-800 text-sm">${acct.nav.toFixed(2)}</span>
                                                </div>
                                                <div className="flex justify-between items-center">
                                                    <span className="text-[10px] text-slate-500">Unrealised P&L</span>
                                                    <span className={`font-mono font-bold text-sm ${plColor}`}>
                                                        {acct.unrealized_pl >= 0 ? '+' : ''}${acct.unrealized_pl.toFixed(2)}
                                                    </span>
                                                </div>
                                                <div className="flex justify-between items-center pt-1 border-t border-slate-100">
                                                    <span className="text-[10px] text-slate-500">Margin Used</span>
                                                    <span className="font-mono text-slate-600 text-xs">${acct.margin_used.toFixed(2)} ({acct.margin_pct}%)</span>
                                                </div>
                                                <div className="flex justify-between items-center">
                                                    <span className="text-[10px] text-slate-500">Positions</span>
                                                    <span className="font-mono text-slate-600 text-xs">{acct.open_trade_count}</span>
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        </section>
                    )}

                    {/* Open Positions */}
                    <section>
                        <p className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-3 flex items-center gap-2">
                            <TrendingUp size={12} /> Open Positions
                            <span className="ml-auto text-[9px] font-mono bg-slate-100 text-slate-500 border border-slate-200 px-2 py-0.5 rounded-full">
                                {openTrades.length} open
                            </span>
                        </p>
                        <div className="bg-white border border-slate-200 rounded-2xl shadow-sm overflow-hidden">
                            {openTrades.length === 0 ? (
                                <p className="text-slate-400 text-center py-6 text-sm">No open positions</p>
                            ) : (
                                <table className="w-full text-xs">
                                    <thead>
                                        <tr className="border-b border-slate-100 bg-slate-50 text-[10px] text-slate-400 font-bold uppercase tracking-wider">
                                            <th className="text-left px-4 py-2.5">Instrument</th>
                                            <th className="text-left px-4 py-2.5">Strategy</th>
                                            <th className="text-left px-4 py-2.5">Side</th>
                                            <th className="text-right px-4 py-2.5">Leverage</th>
                                            <th className="text-right px-4 py-2.5">Entry</th>
                                            <th className="text-right px-4 py-2.5">SL / TP</th>
                                            <th className="text-right px-4 py-2.5">Current</th>
                                            <th className="text-right px-4 py-2.5">P&L</th>
                                            <th className="text-right px-4 py-2.5">Opened</th>
                                            <th className="px-4 py-2.5"></th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {openTrades.map(t => {
                                            const pl = t.unrealized_pl;
                                            const isClosing = closingTrade === t.trade_key;
                                            const nav = accountData[t.strategy]?.nav || accountData[t.strategy]?.balance || 0;
                                            const notional = t.entry_price * t.units;
                                            const leverage = nav > 0 ? notional / nav : null;
                                            const fmt = (p: number) => p >= 100 ? p.toFixed(2) : p >= 1 ? p.toFixed(4) : p.toFixed(6);
                                            return (
                                                <tr key={t.id} className="border-b border-slate-50 last:border-0 hover:bg-slate-50 transition-colors">
                                                    <td className="px-4 py-2.5 font-mono font-bold text-slate-800">{t.instrument}</td>
                                                    <td className="px-4 py-2.5 text-slate-500">{t.strategy}</td>
                                                    <td className="px-4 py-2.5">
                                                        <span className={`px-1.5 py-0.5 rounded-full text-[9px] font-black ${
                                                            t.direction > 0
                                                                ? 'bg-emerald-100 text-emerald-700'
                                                                : 'bg-rose-100 text-rose-600'
                                                        }`}>
                                                            {t.direction > 0 ? 'LONG' : 'SHORT'}
                                                        </span>
                                                    </td>
                                                    <td className="px-4 py-2.5 text-right font-mono text-slate-700">
                                                        {leverage != null ? `${leverage.toFixed(1)}×` : '—'}
                                                    </td>
                                                    <td className="px-4 py-2.5 text-right font-mono text-slate-500">{fmt(t.entry_price)}</td>
                                                    <td className="px-4 py-2.5 text-right font-mono text-[10px]">
                                                        {t.stop_price != null
                                                            ? <span className="text-rose-500">{fmt(t.stop_price)}</span>
                                                            : <span className="text-slate-300">—</span>}
                                                        <span className="text-slate-300 mx-1">/</span>
                                                        {t.tp_price != null
                                                            ? <span className="text-emerald-600">{fmt(t.tp_price)}</span>
                                                            : <span className="text-slate-300">—</span>}
                                                    </td>
                                                    <td className="px-4 py-2.5 text-right font-mono text-slate-700">
                                                        {t.current_price != null ? fmt(t.current_price) : '—'}
                                                    </td>
                                                    <td className={`px-4 py-2.5 text-right font-mono font-bold ${
                                                        pl == null ? 'text-slate-400' : pl >= 0 ? 'text-emerald-600' : 'text-rose-500'
                                                    }`}>
                                                        {pl == null ? '—' : `${pl >= 0 ? '+' : ''}$${pl.toFixed(2)}`}
                                                    </td>
                                                    <td className="px-4 py-2.5 text-right text-slate-400 font-mono">
                                                        {new Date(t.entry_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                                                    </td>
                                                    <td className="px-4 py-2.5 text-right">
                                                        <button
                                                            onClick={() => closeTrade(t.trade_key)}
                                                            disabled={isClosing}
                                                            className={`px-2.5 py-1 rounded-lg text-[9px] font-black uppercase tracking-wider transition-all ${
                                                                isClosing
                                                                    ? 'bg-slate-100 text-slate-400 cursor-wait'
                                                                    : 'bg-rose-50 text-rose-600 hover:bg-rose-500 hover:text-white border border-rose-200'
                                                            }`}
                                                        >
                                                            {isClosing ? '...' : 'Close'}
                                                        </button>
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            )}
                        </div>
                    </section>

                    {/* Stats row — today */}
                    <section className="grid grid-cols-4 gap-4">
                        <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm">
                            <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-2">Open Positions</p>
                            <p className={`text-3xl font-mono font-bold ${openTrades.length > 0 ? 'text-blue-600' : 'text-slate-900'}`}>{openTrades.length}</p>
                        </div>
                        <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm">
                            <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-2">Today's P/L</p>
                            <p className={`text-3xl font-mono font-bold ${stats.daily_pnl >= 0 ? 'text-emerald-600' : 'text-rose-500'}`}>
                                {stats.daily_pnl >= 0 ? '+' : ''}${stats.daily_pnl.toFixed(2)}
                            </p>
                        </div>
                        <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm">
                            <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-2">Today W / L</p>
                            <p className="text-3xl font-mono font-bold text-blue-600">{stats.wins} / {stats.losses}</p>
                        </div>
                        <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm">
                            <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-2">Today Win Rate</p>
                            <p className="text-3xl font-mono font-bold text-emerald-600">{winRate}%</p>
                        </div>
                    </section>

                    {/* All-time performance */}
                    <section>
                        <p className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-3 flex items-center gap-2">
                            <BarChart3 size={12} /> All-Time Performance
                        </p>
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
                                <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-1">Total P/L</p>
                                <p className={`text-2xl font-mono font-bold ${at.total_pl >= 0 ? 'text-emerald-600' : 'text-rose-500'}`}>
                                    {at.total_pl >= 0 ? '+' : ''}${at.total_pl.toFixed(2)}
                                </p>
                            </div>
                            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
                                <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-1">Win Rate</p>
                                <p className="text-2xl font-mono font-bold text-slate-800">{at.win_rate}%</p>
                                <p className="text-[10px] text-slate-400 font-mono mt-0.5">{at.total_wins}W / {at.total_losses}L of {at.total_trades}</p>
                            </div>
                            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
                                <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-1">Avg Win / Loss</p>
                                <p className="text-2xl font-mono font-bold text-slate-800">
                                    <span className="text-emerald-600">${at.avg_win.toFixed(2)}</span>
                                    <span className="text-slate-300 text-lg"> / </span>
                                    <span className="text-rose-500">${Math.abs(at.avg_loss).toFixed(2)}</span>
                                </p>
                            </div>
                            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
                                <p className="text-[9px] text-slate-400 font-bold uppercase tracking-tighter mb-1">Profit Factor</p>
                                <p className={`text-2xl font-mono font-bold ${
                                    at.profit_factor == null ? 'text-slate-400' :
                                    at.profit_factor >= 1.5 ? 'text-emerald-600' :
                                    at.profit_factor >= 1.0 ? 'text-amber-500' : 'text-rose-500'
                                }`}>
                                    {at.profit_factor != null ? at.profit_factor.toFixed(2) : '—'}
                                </p>
                                <p className="text-[10px] text-slate-400 font-mono mt-0.5">gross profit / loss</p>
                            </div>
                        </div>
                        {/* By strategy */}
                        {stats.by_strategy && Object.keys(stats.by_strategy).length > 0 && (
                            <div className="mt-3 bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-sm">
                                <table className="w-full text-xs">
                                    <thead>
                                        <tr className="border-b border-slate-100 bg-slate-50 text-[10px] text-slate-400 font-bold uppercase tracking-wider">
                                            <th className="text-left px-4 py-2">Strategy</th>
                                            <th className="text-right px-4 py-2">Trades</th>
                                            <th className="text-right px-4 py-2">W / L</th>
                                            <th className="text-right px-4 py-2">Win %</th>
                                            <th className="text-right px-4 py-2">Total P/L</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {Object.entries(stats.by_strategy).map(([name, s]) => {
                                            const wr = s.trades > 0 ? Math.round(s.wins / s.trades * 100) : 0;
                                            return (
                                                <tr key={name} className="border-b border-slate-50 last:border-0">
                                                    <td className="px-4 py-2 font-bold text-slate-700 capitalize">{name.replace('_', ' ')}</td>
                                                    <td className="px-4 py-2 text-right font-mono text-slate-600">{s.trades}</td>
                                                    <td className="px-4 py-2 text-right font-mono text-slate-600">{s.wins} / {s.losses}</td>
                                                    <td className="px-4 py-2 text-right font-mono font-bold text-slate-700">{wr}%</td>
                                                    <td className={`px-4 py-2 text-right font-mono font-bold ${s.pl >= 0 ? 'text-emerald-600' : 'text-rose-500'}`}>
                                                        {s.pl >= 0 ? '+' : ''}${s.pl.toFixed(2)}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>
                </div>

                {/* Right — Trade history */}
                <div className="lg:col-span-4">
                    <div className="bg-white border border-slate-200 rounded-2xl p-6 h-full flex flex-col shadow-sm">
                        <h2 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-5 flex items-center gap-2">
                            <History size={12} /> Trade Archive
                        </h2>

                        <div className="space-y-3 overflow-y-auto pr-1 flex-1">
                            {pastTrades.length === 0 ? (
                                <p className="text-slate-400 text-center py-8 text-sm">No trades yet</p>
                            ) : pastTrades.map(trade => (
                                <div
                                    key={trade.id}
                                    onClick={() => setSelectedTrade(trade)}
                                    className="p-3.5 bg-slate-50 border border-slate-100 rounded-xl cursor-pointer hover:border-slate-300 hover:shadow-sm transition-all group"
                                >
                                    <div className="flex justify-between mb-1.5">
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs font-bold text-slate-700 group-hover:text-blue-600">{trade.stock}</span>
                                            <span className={`text-[8px] font-black px-1.5 py-0.5 rounded-full ${
                                                trade.profit >= 0
                                                    ? 'bg-emerald-100 text-emerald-700'
                                                    : 'bg-rose-100 text-rose-600'
                                            }`}>
                                                {trade.profit >= 0 ? 'WIN' : 'LOSS'}
                                            </span>
                                        </div>
                                        <span className={`text-xs font-mono font-bold ${trade.profit >= 0 ? 'text-emerald-600' : 'text-rose-500'}`}>
                                            {trade.profit > 0 ? '+' : ''}{trade.profit.toFixed(2)}
                                        </span>
                                    </div>
                                    <div className="flex justify-between items-center text-[10px] text-slate-400 font-mono">
                                        <span>{trade.reason}</span>
                                        <span>{trade.date}</span>
                                    </div>
                                </div>
                            ))}
                        </div>

                        <div className="mt-auto pt-4 border-t border-slate-100 space-y-2">
                            <div className="flex items-center justify-between text-xs text-slate-400">
                                <span>System Status</span>
                                <span className={`font-mono font-semibold ${botRunning ? 'text-emerald-600' : 'text-slate-400'}`}>
                                    {botRunning ? 'ACTIVE' : 'IDLE'}
                                </span>
                            </div>
                            <div className="flex items-center justify-between text-xs text-slate-400">
                                <span>Signed in as</span>
                                <span className="text-blue-600 font-mono truncate max-w-[140px]">{session.email}</span>
                            </div>
                        </div>
                    </div>
                </div>
            </main>
        </div>
    );
}
