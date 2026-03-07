import { Activity, TrendingUp, Shield } from 'lucide-react';

interface Props {
    onSignInClick: () => void;
}

export default function LandingPage({ onSignInClick }: Props) {
    return (
        <div className="min-h-screen flex flex-col" style={{ background: 'linear-gradient(135deg, #0f4c35 0%, #0d3d6b 50%, #1a1a4e 100%)' }}>
            <nav className="flex items-center justify-between px-8 py-5 border-b border-white/10 bg-black/20 backdrop-blur-md sticky top-0 z-10">
                <div>
                    <span className="text-xl font-bold text-white italic tracking-tight">
                        MOMENTUM ENGINE
                    </span>
                    <p className="text-white/40 text-[9px] font-mono uppercase tracking-[0.3em]">Live Trading System</p>
                </div>
                <button
                    onClick={onSignInClick}
                    className="px-5 py-2 text-sm bg-emerald-500 hover:bg-emerald-400 text-white rounded-lg font-semibold transition-colors shadow-lg shadow-emerald-900/50"
                >
                    Sign in
                </button>
            </nav>

            <main className="flex-1 flex flex-col items-center justify-center px-8 py-24 text-center">
                <div className="max-w-3xl mx-auto space-y-8">
                    <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-emerald-500/20 border border-emerald-400/30 text-emerald-300 text-xs font-mono uppercase tracking-widest">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                        Live Trading Active
                    </div>

                    <h1 className="text-5xl md:text-6xl font-black tracking-tight leading-[1.1] text-white">
                        Algorithmic trading,{' '}
                        <span className="text-emerald-400">
                            automated.
                        </span>
                    </h1>

                    <p className="text-white/60 text-lg max-w-xl mx-auto leading-relaxed">
                        A momentum-based trading engine for OANDA markets. Monitor performance,
                        control your bot, and manage multiple strategies — all in one dashboard.
                    </p>

                    <div className="pt-2">
                        <button
                            onClick={onSignInClick}
                            className="px-8 py-3.5 bg-emerald-500 hover:bg-emerald-400 text-white font-bold rounded-xl transition-all shadow-xl shadow-emerald-900/50 text-sm uppercase tracking-wider"
                        >
                            Sign in to Dashboard
                        </button>
                    </div>
                </div>

                <div className="mt-24 grid grid-cols-1 md:grid-cols-3 gap-5 max-w-4xl w-full">
                    <div className="bg-white/10 backdrop-blur-sm border border-white/15 rounded-2xl p-6 text-left hover:bg-white/15 transition-colors">
                        <div className="w-10 h-10 bg-emerald-500/30 border border-emerald-400/40 rounded-xl flex items-center justify-center mb-4">
                            <Activity size={18} className="text-emerald-400" />
                        </div>
                        <h3 className="font-bold text-white mb-2">Live Monitoring</h3>
                        <p className="text-white/50 text-sm leading-relaxed">
                            Real-time P&L tracking, win rate, and trade history updated every 5 seconds.
                        </p>
                    </div>
                    <div className="bg-white/10 backdrop-blur-sm border border-white/15 rounded-2xl p-6 text-left hover:bg-white/15 transition-colors">
                        <div className="w-10 h-10 bg-blue-500/30 border border-blue-400/40 rounded-xl flex items-center justify-center mb-4">
                            <TrendingUp size={18} className="text-blue-300" />
                        </div>
                        <h3 className="font-bold text-white mb-2">Multi-Instrument</h3>
                        <p className="text-white/50 text-sm leading-relaxed">
                            Trade NAS100, Gold, Silver, and Copper with pre-configured momentum profiles.
                        </p>
                    </div>
                    <div className="bg-white/10 backdrop-blur-sm border border-white/15 rounded-2xl p-6 text-left hover:bg-white/15 transition-colors">
                        <div className="w-10 h-10 bg-violet-500/30 border border-violet-400/40 rounded-xl flex items-center justify-center mb-4">
                            <Shield size={18} className="text-violet-300" />
                        </div>
                        <h3 className="font-bold text-white mb-2">Risk Controls</h3>
                        <p className="text-white/50 text-sm leading-relaxed">
                            Per-trade SL/TP, daily loss caps, session filters, and an instant kill switch.
                        </p>
                    </div>
                </div>
            </main>

            <footer className="px-8 py-5 border-t border-white/10 text-center text-white/30 text-xs">
                Momentum Engine — Private Access Only
            </footer>
        </div>
    );
}
