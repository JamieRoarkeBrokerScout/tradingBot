import { useState, useEffect } from 'react';
import { X, CheckCircle, Circle } from 'lucide-react';
import { apiFetch } from '../api';

interface Props {
    onClose: () => void;
}

const BOTS = [
    { key: 'legacy_bot',  label: 'Momentum Bot',  description: 'Original momentum trader (NAS100, Gold, Silver, Copper)' },
    { key: 'stat_arb',    label: 'Stat Arb',       description: 'Pairs trading on XAU/XAG and SPX/BCO' },
    { key: 'momentum',    label: 'Momentum',        description: 'RSI + volume strategy on SPX and Gold' },
    { key: 'vol_premium', label: 'Vol Premium',     description: 'Short volatility on SPX500' },
] as const;

type BotKey = typeof BOTS[number]['key'];

interface BotTokenState {
    configured: boolean;
    oanda_account_id: string;
    oanda_account_type: string;
    oanda_access_token_set: boolean;
}

type AllTokens = Partial<Record<BotKey, BotTokenState>>;
type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

const inputClass = "w-full bg-slate-50 border border-slate-200 rounded-xl px-3 py-2.5 text-sm text-slate-900 font-mono placeholder-slate-400 focus:outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 transition-all";
const labelClass = "block text-[10px] text-slate-500 font-bold uppercase tracking-wider mb-1.5";

export default function TokenSettings({ onClose }: Props) {
    const [activeBot, setActiveBot] = useState<BotKey>('legacy_bot');
    const [allTokens, setAllTokens] = useState<AllTokens>({});
    const [formState, setFormState] = useState<Record<BotKey, { accountId: string; accessToken: string; accountType: string }>>({
        legacy_bot:  { accountId: '', accessToken: '', accountType: 'practice' },
        stat_arb:    { accountId: '', accessToken: '', accountType: 'practice' },
        momentum:    { accountId: '', accessToken: '', accountType: 'practice' },
        vol_premium: { accountId: '', accessToken: '', accountType: 'practice' },
    });
    const [saveStatus, setSaveStatus] = useState<Record<BotKey, SaveStatus>>({
        legacy_bot: 'idle', stat_arb: 'idle', momentum: 'idle', vol_premium: 'idle',
    });

    useEffect(() => {
        apiFetch('/api/user/tokens')
            .then(r => r.json())
            .then((data: AllTokens) => {
                setAllTokens(data);
                // Pre-fill account ID and type if already configured
                setFormState(prev => {
                    const next = { ...prev };
                    for (const bot of BOTS) {
                        const tok = data[bot.key];
                        if (tok?.configured) {
                            next[bot.key] = {
                                ...next[bot.key],
                                accountId:   tok.oanda_account_id  ?? '',
                                accountType: tok.oanda_account_type ?? 'practice',
                            };
                        }
                    }
                    return next;
                });
            })
            .catch(() => {});
    }, []);

    const setField = (field: 'accountId' | 'accessToken' | 'accountType', value: string) => {
        setFormState(prev => ({ ...prev, [activeBot]: { ...prev[activeBot], [field]: value } }));
    };

    const save = async () => {
        setSaveStatus(prev => ({ ...prev, [activeBot]: 'saving' }));
        const form = formState[activeBot];
        try {
            const resp = await apiFetch('/api/user/tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    bot_key:            activeBot,
                    oanda_account_id:   form.accountId,
                    oanda_access_token: form.accessToken,
                    oanda_account_type: form.accountType,
                }),
            });
            if (resp.ok) {
                setSaveStatus(prev => ({ ...prev, [activeBot]: 'saved' }));
                setAllTokens(prev => ({
                    ...prev,
                    [activeBot]: {
                        configured:             true,
                        oanda_account_id:       form.accountId,
                        oanda_account_type:     form.accountType,
                        oanda_access_token_set: true,
                    },
                }));
                setTimeout(() => setSaveStatus(prev => ({ ...prev, [activeBot]: 'idle' })), 2000);
            } else {
                setSaveStatus(prev => ({ ...prev, [activeBot]: 'error' }));
            }
        } catch {
            setSaveStatus(prev => ({ ...prev, [activeBot]: 'error' }));
        }
    };

    const form    = formState[activeBot];
    const status  = saveStatus[activeBot];
    const tokInfo = allTokens[activeBot];

    const saveLabel: Record<SaveStatus, string> = {
        idle:   'Save Credentials',
        saving: 'Saving…',
        saved:  'Saved!',
        error:  'Error — try again',
    };

    return (
        <div className="fixed inset-0 z-[200] flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm">
            <div className="bg-white border border-slate-200 w-full max-w-xl rounded-2xl shadow-xl overflow-hidden flex flex-col">

                {/* Header */}
                <div className="p-6 border-b border-slate-100 flex justify-between items-start">
                    <div>
                        <h2 className="text-lg font-bold text-slate-900">OANDA API Settings</h2>
                        <p className="text-slate-500 text-xs mt-0.5">
                            Each bot can use a separate OANDA account. Credentials are stored securely in the database.
                        </p>
                    </div>
                    <button onClick={onClose} className="p-2 hover:bg-slate-100 rounded-xl text-slate-400 hover:text-slate-700 transition-all">
                        <X size={20} />
                    </button>
                </div>

                {/* Bot tabs */}
                <div className="flex border-b border-slate-100 px-6 gap-1 pt-3 overflow-x-auto">
                    {BOTS.map(bot => {
                        const configured = allTokens[bot.key]?.configured ?? false;
                        const isActive   = activeBot === bot.key;
                        return (
                            <button
                                key={bot.key}
                                onClick={() => setActiveBot(bot.key)}
                                className={`flex items-center gap-1.5 px-3 py-2 text-xs font-bold rounded-t-lg whitespace-nowrap transition-all border-b-2 -mb-px ${
                                    isActive
                                        ? 'border-blue-500 text-blue-600 bg-blue-50/50'
                                        : 'border-transparent text-slate-500 hover:text-slate-700'
                                }`}
                            >
                                {configured
                                    ? <CheckCircle size={11} className="text-emerald-500 shrink-0" />
                                    : <Circle size={11} className="text-slate-300 shrink-0" />
                                }
                                {bot.label}
                            </button>
                        );
                    })}
                </div>

                {/* Form */}
                <div className="p-6 space-y-4 flex-1">
                    <p className="text-[11px] text-slate-400">
                        {BOTS.find(b => b.key === activeBot)?.description}
                    </p>

                    {tokInfo?.configured && (
                        <p className="text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-xl px-3 py-2">
                            Credentials configured. Enter a new access token below to update.
                        </p>
                    )}

                    <div>
                        <label className={labelClass}>Account ID</label>
                        <input
                            value={form.accountId}
                            onChange={e => setField('accountId', e.target.value)}
                            placeholder="101-002-xxxxxxxx-001"
                            className={inputClass}
                        />
                    </div>
                    <div>
                        <label className={labelClass}>Access Token</label>
                        <input
                            type="password"
                            value={form.accessToken}
                            onChange={e => setField('accessToken', e.target.value)}
                            placeholder={tokInfo?.configured ? '••••••••••••••••' : 'Enter your OANDA access token'}
                            className={inputClass}
                        />
                    </div>
                    <div>
                        <label className={labelClass}>Account Type</label>
                        <select
                            value={form.accountType}
                            onChange={e => setField('accountType', e.target.value)}
                            className={inputClass}
                        >
                            <option value="practice">Practice</option>
                            <option value="live">Live</option>
                        </select>
                    </div>
                </div>

                {/* Footer */}
                <div className="p-6 border-t border-slate-100 flex gap-3">
                    <button
                        onClick={save}
                        disabled={status === 'saving'}
                        className={`flex-1 font-bold py-2.5 rounded-xl transition-all text-sm uppercase tracking-wider ${
                            status === 'saved'
                                ? 'bg-emerald-500 text-white'
                                : status === 'error'
                                    ? 'bg-rose-500 text-white'
                                    : 'bg-slate-900 hover:bg-slate-700 disabled:bg-slate-200 disabled:text-slate-400 text-white'
                        }`}
                    >
                        {saveLabel[status]}
                    </button>
                    <button onClick={onClose} className="px-4 bg-slate-100 hover:bg-slate-200 text-slate-600 font-bold rounded-xl transition-all text-sm">
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
}
