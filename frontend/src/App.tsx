import { useState, useEffect } from 'react'
import { Activity, DollarSign, TrendingUp, Settings, RefreshCw, X } from 'lucide-react'

interface Trade {
    id: number
    entry_time: string
    exit_time: string
    instrument: string
    direction: number
    entry_price: number
    exit_price: number
    exit_reason: string
    raw_pl: number
    pl_R: number
}

interface Stats {
    daily_pnl: number
    trades_today: number
    wins: number
    losses: number
}

interface BotConfig {
    profile?: string
    instrument?: string
    bar_length?: string
    units?: number
    threshold_k?: number
    per_trade_sl?: number
    per_trade_tp?: number
}

function App() {
    const [trades, setTrades] = useState<Trade[]>([])
    const [stats, setStats] = useState<Stats>({ daily_pnl: 0, trades_today: 0, wins: 0, losses: 0 })
    const [botRunning, setBotRunning] = useState(false)
    const [loading, setLoading] = useState(true)
    const [showSettings, setShowSettings] = useState(false)
    const [useProfile, setUseProfile] = useState(true)

    // Separate pending config (what user is editing) from current config (what bot is running)
    const [pendingConfig, setPendingConfig] = useState<BotConfig>({
        profile: 'nas_a'
    })
    const [currentConfig, setCurrentConfig] = useState<BotConfig>({
        profile: 'nas_a'
    })

    // Profile definitions
    const profiles = [
        {
            value: 'nas_a',
            label: 'NASDAQ-100',
            description: 'NAS100_USD • 3min bars • 1 unit • SL: 20 / TP: 60'
        },
        {
            value: 'xau_a',
            label: 'Gold',
            description: 'XAU_USD • 3min bars • 10 units • SL: 5 / TP: 15'
        },
        {
            value: 'xag_a',
            label: 'Silver',
            description: 'XAG_USD • 1min bars • 400 units • SL: 0.4 / TP: 0.8'
        },
        {
            value: 'xcu_a',
            label: 'Copper',
            description: 'XCU_USD • 3min bars • 10 units • SL: 150 / TP: 450'
        },
    ]

    // Instrument presets for custom mode
    const instruments = [
        { value: 'NAS100_USD', label: 'NASDAQ-100', defaultUnits: 1, defaultSL: 20, defaultTP: 60, defaultBar: '3min' },
        { value: 'XAU_USD', label: 'Gold', defaultUnits: 10, defaultSL: 5, defaultTP: 15, defaultBar: '3min' },
        { value: 'XAG_USD', label: 'Silver', defaultUnits: 400, defaultSL: 0.4, defaultTP: 0.8, defaultBar: '1min' },
        { value: 'XCU_USD', label: 'Copper', defaultUnits: 10, defaultSL: 150, defaultTP: 450, defaultBar: '3min' },
    ]

    const fetchStats = async () => {
        try {
            const response = await fetch('http://localhost:5000/api/stats')
            const data = await response.json()
            setStats(data)
        } catch (error) {
            console.error('Failed to fetch stats:', error)
        }
    }

    const fetchTrades = async () => {
        try {
            const response = await fetch('http://localhost:5000/api/trades')
            const data = await response.json()
            setTrades(data)
        } catch (error) {
            console.error('Failed to fetch trades:', error)
        }
    }

    const checkHealth = async () => {
        try {
            const response = await fetch('http://localhost:5000/api/health')
            const data = await response.json()
            setBotRunning(data.bot_running)

            // Only update current config (what's running), not pending config (what user is editing)
            if (data.config) {
                setCurrentConfig(data.config)
                // Only update pending if settings panel is closed
                if (!showSettings) {
                    setPendingConfig(data.config)
                    setUseProfile(!!data.config.profile)
                }
            }
        } catch (error) {
            setBotRunning(false)
        }
    }

    const startBot = async () => {
        try {
            await fetch('http://localhost:5000/api/bot/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(pendingConfig)
            })
            setTimeout(checkHealth, 1000)
            setShowSettings(false)
        } catch (error) {
            console.error('Failed to start bot:', error)
        }
    }

    const stopBot = async () => {
        try {
            await fetch('http://localhost:5000/api/bot/stop', { method: 'POST' })
            setTimeout(checkHealth, 1000)
        } catch (error) {
            console.error('Failed to stop bot:', error)
        }
    }

    const handleProfileChange = (profile: string) => {
        setPendingConfig({ profile })
    }

    const handleInstrumentChange = (instrument: string) => {
        const preset = instruments.find(i => i.value === instrument)
        if (preset) {
            setPendingConfig({
                instrument: preset.value,
                bar_length: preset.defaultBar,
                units: preset.defaultUnits,
                threshold_k: pendingConfig.threshold_k || 1.8,
                per_trade_sl: preset.defaultSL,
                per_trade_tp: preset.defaultTP
            })
        }
    }

    const switchMode = (toProfile: boolean) => {
        setUseProfile(toProfile)
        if (toProfile) {
            setPendingConfig({ profile: 'nas_a' })
        } else {
            setPendingConfig({
                instrument: 'NAS100_USD',
                bar_length: '3min',
                units: 1,
                threshold_k: 1.8,
                per_trade_sl: 20.0,
                per_trade_tp: 60.0
            })
        }
    }

    // When opening settings, load current pending config
    const handleOpenSettings = () => {
        if (!showSettings) {
            // Copy current config to pending when opening
            setPendingConfig(currentConfig)
            setUseProfile(!!currentConfig.profile)
        }
        setShowSettings(!showSettings)
    }

    useEffect(() => {
        const loadData = async () => {
            setLoading(true)
            await Promise.all([fetchStats(), fetchTrades(), checkHealth()])
            setLoading(false)
        }
        loadData()
    }, [])

    useEffect(() => {
        const interval = setInterval(() => {
            fetchStats()
            fetchTrades()
            checkHealth()
        }, 5000)
        return () => clearInterval(interval)
    }, [showSettings]) // Re-subscribe when showSettings changes

    const winRate = stats.trades_today > 0
        ? ((stats.wins / stats.trades_today) * 100).toFixed(1)
        : '0.0'

    const currentProfileLabel = profiles.find(p => p.value === currentConfig.profile)?.label || currentConfig.instrument

    return (
        <div className="min-h-screen bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 text-white p-6">
            {/* Header */}
            <div className="flex justify-between items-center mb-8">
                <div>
                    <h1 className="text-3xl font-bold text-white">Trading Bot Dashboard</h1>
                    <p className="text-slate-400 text-sm mt-1">Real-time momentum trading</p>
                </div>
                <div className="flex gap-3">
                    {botRunning ? (
                        <button
                            onClick={stopBot}
                            className="flex items-center gap-2 px-5 py-2.5 bg-red-600 rounded-lg hover:bg-red-700 transition-colors shadow-lg shadow-red-900/50"
                        >
                            <Activity className="w-5 h-5" />
                            <span className="font-medium">Stop Bot</span>
                        </button>
                    ) : (
                        <button
                            onClick={handleOpenSettings}
                            className="flex items-center gap-2 px-5 py-2.5 bg-emerald-600 rounded-lg hover:bg-emerald-700 transition-colors shadow-lg shadow-emerald-900/50"
                        >
                            <Activity className="w-5 h-5" />
                            <span className="font-medium">Start Bot</span>
                        </button>
                    )}
                    <button
                        onClick={() => { fetchStats(); fetchTrades(); checkHealth(); }}
                        className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 border border-slate-700 rounded-lg hover:bg-slate-700 transition-colors"
                    >
                        <RefreshCw className="w-4 h-4" />
                    </button>
                    <button
                        onClick={handleOpenSettings}
                        className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 border border-slate-700 rounded-lg hover:bg-slate-700 transition-colors"
                    >
                        <Settings className="w-4 h-4" />
                    </button>
                </div>
            </div>

            {/* Settings Panel */}
            {showSettings && (
                <div className="bg-slate-900 border border-slate-700 rounded-xl p-6 mb-8 shadow-2xl">
                    <div className="flex justify-between items-center mb-6">
                        <h2 className="text-xl font-semibold">Bot Configuration</h2>
                        <button onClick={() => setShowSettings(false)} className="text-slate-400 hover:text-white">
                            <X className="w-5 h-5" />
                        </button>
                    </div>

                    {/* Mode Toggle */}
                    <div className="flex gap-2 mb-6 bg-slate-800 p-1 rounded-lg">
                        <button
                            onClick={() => switchMode(true)}
                            className={`flex-1 px-4 py-2 rounded-md transition-colors font-medium ${useProfile
                                ? 'bg-blue-600 text-white'
                                : 'text-slate-400 hover:text-white'
                                }`}
                        >
                            Use Profile
                        </button>
                        <button
                            onClick={() => switchMode(false)}
                            className={`flex-1 px-4 py-2 rounded-md transition-colors font-medium ${!useProfile
                                ? 'bg-blue-600 text-white'
                                : 'text-slate-400 hover:text-white'
                                }`}
                        >
                            Custom Settings
                        </button>
                    </div>

                    {useProfile ? (
                        /* Profile Mode */
                        <div>
                            <label className="block text-sm font-medium text-slate-300 mb-3">Select Profile</label>
                            <div className="grid grid-cols-1 gap-3">
                                {profiles.map(profile => (
                                    <button
                                        key={profile.value}
                                        onClick={() => handleProfileChange(profile.value)}
                                        className={`p-4 rounded-lg border-2 transition-all text-left ${pendingConfig.profile === profile.value
                                            ? 'border-blue-500 bg-blue-500/10'
                                            : 'border-slate-700 bg-slate-800 hover:border-slate-600'
                                            }`}
                                    >
                                        <div className="font-semibold text-white mb-1">{profile.label}</div>
                                        <div className="text-sm text-slate-400">{profile.description}</div>
                                    </button>
                                ))}
                            </div>
                        </div>
                    ) : (
                        /* Custom Settings Mode */
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className="block text-sm font-medium text-slate-300 mb-2">Instrument</label>
                                <select
                                    value={pendingConfig.instrument}
                                    onChange={(e) => handleInstrumentChange(e.target.value)}
                                    className="w-full bg-slate-800 border border-slate-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                                >
                                    {instruments.map(inst => (
                                        <option key={inst.value} value={inst.value}>{inst.label}</option>
                                    ))}
                                </select>
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-slate-300 mb-2">Bar Length</label>
                                <select
                                    value={pendingConfig.bar_length}
                                    onChange={(e) => setPendingConfig({ ...pendingConfig, bar_length: e.target.value })}
                                    className="w-full bg-slate-800 border border-slate-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                                >
                                    <option value="1min">1 Minute</option>
                                    <option value="3min">3 Minutes</option>
                                    <option value="5min">5 Minutes</option>
                                    <option value="15min">15 Minutes</option>
                                </select>
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-slate-300 mb-2">Units</label>
                                <input
                                    type="number"
                                    value={pendingConfig.units}
                                    onChange={(e) => setPendingConfig({ ...pendingConfig, units: Number(e.target.value) })}
                                    className="w-full bg-slate-800 border border-slate-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-slate-300 mb-2">Threshold K</label>
                                <input
                                    type="number"
                                    step="0.1"
                                    value={pendingConfig.threshold_k}
                                    onChange={(e) => setPendingConfig({ ...pendingConfig, threshold_k: Number(e.target.value) })}
                                    className="w-full bg-slate-800 border border-slate-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-slate-300 mb-2">Stop Loss</label>
                                <input
                                    type="number"
                                    step="0.1"
                                    value={pendingConfig.per_trade_sl}
                                    onChange={(e) => setPendingConfig({ ...pendingConfig, per_trade_sl: Number(e.target.value) })}
                                    className="w-full bg-slate-800 border border-slate-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-slate-300 mb-2">Take Profit</label>
                                <input
                                    type="number"
                                    step="0.1"
                                    value={pendingConfig.per_trade_tp}
                                    onChange={(e) => setPendingConfig({ ...pendingConfig, per_trade_tp: Number(e.target.value) })}
                                    className="w-full bg-slate-800 border border-slate-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                                />
                            </div>
                        </div>
                    )}

                    <div className="mt-6 flex gap-3 items-center">
                        <button
                            onClick={startBot}
                            disabled={botRunning}
                            className="px-6 py-2.5 bg-emerald-600 rounded-lg hover:bg-emerald-700 disabled:bg-slate-700 disabled:cursor-not-allowed transition-colors font-medium shadow-lg"
                        >
                            {botRunning ? 'Bot is Running' : 'Start Bot with These Settings'}
                        </button>
                        {botRunning && (
                            <p className="text-sm text-slate-400">Stop bot to change settings</p>
                        )}
                    </div>
                </div>
            )}

            {/* Status Cards */}
            <div className="grid grid-cols-4 gap-4 mb-8">
                {/* Status Card */}
                <div className="bg-slate-900 border border-slate-700 rounded-xl p-6 shadow-xl">
                    <div className="flex items-center justify-between mb-3">
                        <span className="text-slate-400 text-sm font-medium">Status</span>
                        <div className={`p-2 rounded-lg ${botRunning ? 'bg-emerald-500/20' : 'bg-red-500/20'}`}>
                            <Activity className={`w-5 h-5 ${botRunning ? 'text-emerald-400' : 'text-red-400'}`} />
                        </div>
                    </div>
                    <div className={`text-2xl font-bold ${botRunning ? 'text-emerald-400' : 'text-red-400'}`}>
                        {loading ? '...' : botRunning ? 'Running' : 'Stopped'}
                    </div>
                    {botRunning && (
                        <div className="text-xs text-slate-400 mt-2 font-medium">{currentProfileLabel}</div>
                    )}
                </div>

                {/* Daily P&L Card */}
                <div className="bg-slate-900 border border-slate-700 rounded-xl p-6 shadow-xl">
                    <div className="flex items-center justify-between mb-3">
                        <span className="text-slate-400 text-sm font-medium">Daily P&L</span>
                        <div className={`p-2 rounded-lg ${stats.daily_pnl >= 0 ? 'bg-emerald-500/20' : 'bg-red-500/20'}`}>
                            <DollarSign className={`w-5 h-5 ${stats.daily_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`} />
                        </div>
                    </div>
                    <div className={`text-2xl font-bold ${stats.daily_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        ${stats.daily_pnl.toFixed(2)}
                    </div>
                </div>

                {/* Trades Today Card */}
                <div className="bg-slate-900 border border-slate-700 rounded-xl p-6 shadow-xl">
                    <div className="flex items-center justify-between mb-3">
                        <span className="text-slate-400 text-sm font-medium">Trades Today</span>
                        <div className="p-2 rounded-lg bg-blue-500/20">
                            <TrendingUp className="w-5 h-5 text-blue-400" />
                        </div>
                    </div>
                    <div className="text-2xl font-bold text-white">{stats.trades_today}</div>
                </div>

                {/* Win Rate Card */}
                <div className="bg-slate-900 border border-slate-700 rounded-xl p-6 shadow-xl">
                    <div className="flex items-center justify-between mb-3">
                        <span className="text-slate-400 text-sm font-medium">Win Rate</span>
                        <div className="p-2 rounded-lg bg-purple-500/20">
                            <Activity className="w-5 h-5 text-purple-400" />
                        </div>
                    </div>
                    <div className="text-2xl font-bold text-purple-400">{winRate}%</div>
                    <div className="text-xs text-slate-400 mt-2">{stats.wins}W / {stats.losses}L</div>
                </div>
            </div>

            {/* Trades Table */}
            <div className="bg-slate-900 border border-slate-700 rounded-xl p-6 shadow-xl">
                <h2 className="text-xl font-semibold mb-4">Recent Trades</h2>
                {trades.length === 0 ? (
                    <p className="text-slate-400 text-center py-8">No trades yet</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full">
                            <thead>
                                <tr className="text-left text-slate-400 border-b border-slate-800">
                                    <th className="pb-3 font-medium">Time</th>
                                    <th className="pb-3 font-medium">Instrument</th>
                                    <th className="pb-3 font-medium">Direction</th>
                                    <th className="pb-3 font-medium">Entry</th>
                                    <th className="pb-3 font-medium">Exit</th>
                                    <th className="pb-3 font-medium">P&L</th>
                                    <th className="pb-3 font-medium">R-Multiple</th>
                                    <th className="pb-3 font-medium">Reason</th>
                                </tr>
                            </thead>
                            <tbody>
                                {trades.map((trade) => (
                                    <tr key={trade.id} className="border-b border-slate-800 hover:bg-slate-800/50 transition-colors">
                                        <td className="py-4 text-sm text-slate-300">{new Date(trade.exit_time).toLocaleString()}</td>
                                        <td className="py-4 text-white font-medium">{trade.instrument}</td>
                                        <td className="py-4">
                                            <span className={`px-3 py-1 rounded-full text-xs font-semibold ${trade.direction > 0
                                                ? 'bg-emerald-500/20 text-emerald-400'
                                                : 'bg-red-500/20 text-red-400'
                                                }`}>
                                                {trade.direction > 0 ? 'LONG' : 'SHORT'}
                                            </span>
                                        </td>
                                        <td className="py-4 text-slate-300">{trade.entry_price.toFixed(2)}</td>
                                        <td className="py-4 text-slate-300">{trade.exit_price.toFixed(2)}</td>
                                        <td className={`py-4 font-bold ${trade.raw_pl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                            ${trade.raw_pl.toFixed(2)}
                                        </td>
                                        <td className={`py-4 font-semibold ${trade.pl_R >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                            {trade.pl_R.toFixed(2)}R
                                        </td>
                                        <td className="py-4 text-sm text-slate-400">{trade.exit_reason}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>
        </div>
    )
}

export default App