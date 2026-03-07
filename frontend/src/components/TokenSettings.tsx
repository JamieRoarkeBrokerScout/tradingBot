import { useState, useEffect } from 'react';
import { X } from 'lucide-react';
import { apiFetch } from '../api';

interface Props {
    onClose: () => void;
}

export default function TokenSettings({ onClose }: Props) {
    const [accountId, setAccountId] = useState('');
    const [accessToken, setAccessToken] = useState('');
    const [accountType, setAccountType] = useState('practice');
    const [status, setStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
    const [configured, setConfigured] = useState(false);

    useEffect(() => {
        apiFetch('/api/user/tokens')
            .then(r => r.json())
            .then(data => {
                if (data.configured) {
                    setConfigured(true);
                    setAccountId(data.oanda_account_id);
                    setAccountType(data.oanda_account_type);
                }
            })
            .catch(() => {});
    }, []);

    const save = async () => {
        setStatus('saving');
        try {
            const resp = await apiFetch('/api/user/tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    oanda_account_id: accountId,
                    oanda_access_token: accessToken,
                    oanda_account_type: accountType,
                }),
            });
            setStatus(resp.ok ? 'saved' : 'error');
            if (resp.ok) setTimeout(() => setStatus('idle'), 2000);
        } catch {
            setStatus('error');
        }
    };

    const saveLabel = { idle: 'Save Credentials', saving: 'Saving...', saved: 'Saved!', error: 'Error — try again' }[status];

    const inputClass = "w-full bg-slate-50 border border-slate-200 rounded-xl px-3 py-2.5 text-sm text-slate-900 font-mono placeholder-slate-400 focus:outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 transition-all";
    const labelClass = "block text-[10px] text-slate-500 font-bold uppercase tracking-wider mb-1.5";

    return (
        <div className="fixed inset-0 z-[200] flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm">
            <div className="bg-white border border-slate-200 w-full max-w-lg rounded-2xl shadow-xl overflow-hidden">
                <div className="p-6 border-b border-slate-100 flex justify-between items-center">
                    <div>
                        <h2 className="text-lg font-bold text-slate-900">OANDA API Settings</h2>
                        <p className="text-slate-500 text-xs mt-0.5">Credentials are stored securely in the database.</p>
                    </div>
                    <button onClick={onClose} className="p-2 hover:bg-slate-100 rounded-xl text-slate-400 hover:text-slate-700 transition-all">
                        <X size={20} />
                    </button>
                </div>

                <div className="p-6 space-y-4">
                    {configured && (
                        <p className="text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-xl px-3 py-2">
                            Credentials configured. Enter a new access token to update.
                        </p>
                    )}
                    <div>
                        <label className={labelClass}>Account ID</label>
                        <input value={accountId} onChange={e => setAccountId(e.target.value)} placeholder="101-002-xxxxxxxx-001" className={inputClass} />
                    </div>
                    <div>
                        <label className={labelClass}>Access Token</label>
                        <input
                            type="password"
                            value={accessToken}
                            onChange={e => setAccessToken(e.target.value)}
                            placeholder={configured ? '••••••••••••••••' : 'Enter your OANDA access token'}
                            className={inputClass}
                        />
                    </div>
                    <div>
                        <label className={labelClass}>Account Type</label>
                        <select value={accountType} onChange={e => setAccountType(e.target.value)} className={inputClass}>
                            <option value="practice">Practice</option>
                            <option value="live">Live</option>
                        </select>
                    </div>
                </div>

                <div className="p-6 border-t border-slate-100 flex gap-3">
                    <button
                        onClick={save}
                        disabled={status === 'saving'}
                        className="flex-1 bg-emerald-500 hover:bg-emerald-600 disabled:bg-slate-200 disabled:text-slate-400 text-white font-bold py-2.5 rounded-xl transition-all text-sm uppercase tracking-wider"
                    >
                        {saveLabel}
                    </button>
                    <button onClick={onClose} className="px-4 bg-slate-100 hover:bg-slate-200 text-slate-600 font-bold rounded-xl transition-all text-sm">
                        Cancel
                    </button>
                </div>
            </div>
        </div>
    );
}
