import { useState, FormEvent } from 'react';
import { login, register, type AuthSession } from '../auth';

interface Props {
    onLogin: (session: AuthSession) => void;
}

export default function LoginPage({ onLogin }: Props) {
    const [mode, setMode] = useState<'signin' | 'register'>('signin');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);

    const isRegister = mode === 'register';

    const handleSubmit = async (e: FormEvent) => {
        e.preventDefault();
        setError('');

        if (isRegister && password !== confirmPassword) {
            setError('Passwords do not match');
            return;
        }

        setLoading(true);
        try {
            const session = isRegister
                ? await register(email.trim(), password)
                : await login(email.trim(), password);
            onLogin(session);
        } catch (err) {
            setError(err instanceof Error ? err.message : isRegister ? 'Registration failed' : 'Login failed');
        } finally {
            setLoading(false);
        }
    };

    const switchMode = () => {
        setMode(isRegister ? 'signin' : 'register');
        setError('');
        setPassword('');
        setConfirmPassword('');
    };

    return (
        <div className="min-h-screen flex flex-col" style={{ background: 'linear-gradient(135deg, #0f4c35 0%, #0d3d6b 50%, #1a1a4e 100%)' }}>
            <nav className="flex items-center px-8 py-5 border-b border-white/10 bg-black/20 backdrop-blur-md">
                <div>
                    <span className="text-xl font-bold text-white italic tracking-tight">
                        MOMENTUM ENGINE
                    </span>
                    <p className="text-white/40 text-[9px] font-mono uppercase tracking-[0.3em]">Live Trading System</p>
                </div>
            </nav>

            <main className="flex-1 flex items-center justify-center px-4">
                <div className="w-full max-w-sm">
                    <div className="mb-8 text-center">
                        <h1 className="text-2xl font-black text-white mb-1">
                            {isRegister ? 'Create account' : 'Welcome back'}
                        </h1>
                        <p className="text-white/50 text-sm">
                            {isRegister ? 'Set up your trading dashboard' : 'Sign in to your trading dashboard'}
                        </p>
                    </div>

                    <form onSubmit={handleSubmit} className="bg-white/10 backdrop-blur-md border border-white/20 rounded-2xl p-6 shadow-2xl space-y-4">
                        <div>
                            <label className="block text-[10px] text-white/60 font-bold uppercase tracking-wider mb-1.5">Email</label>
                            <input
                                type="email"
                                value={email}
                                onChange={e => setEmail(e.target.value)}
                                required
                                autoComplete="email"
                                placeholder="you@example.com"
                                className="w-full bg-white/10 border border-white/20 rounded-xl px-3 py-2.5 text-sm text-white placeholder-white/30 focus:outline-none focus:border-emerald-400/60 focus:bg-white/15 transition-all"
                            />
                        </div>
                        <div>
                            <label className="block text-[10px] text-white/60 font-bold uppercase tracking-wider mb-1.5">Password</label>
                            <input
                                type="password"
                                value={password}
                                onChange={e => setPassword(e.target.value)}
                                required
                                autoComplete={isRegister ? 'new-password' : 'current-password'}
                                placeholder="••••••••"
                                className="w-full bg-white/10 border border-white/20 rounded-xl px-3 py-2.5 text-sm text-white placeholder-white/30 focus:outline-none focus:border-emerald-400/60 focus:bg-white/15 transition-all"
                            />
                        </div>

                        {isRegister && (
                            <div>
                                <label className="block text-[10px] text-white/60 font-bold uppercase tracking-wider mb-1.5">Confirm Password</label>
                                <input
                                    type="password"
                                    value={confirmPassword}
                                    onChange={e => setConfirmPassword(e.target.value)}
                                    required
                                    autoComplete="new-password"
                                    placeholder="••••••••"
                                    className="w-full bg-white/10 border border-white/20 rounded-xl px-3 py-2.5 text-sm text-white placeholder-white/30 focus:outline-none focus:border-emerald-400/60 focus:bg-white/15 transition-all"
                                />
                            </div>
                        )}

                        {error && (
                            <p className="text-rose-300 text-xs bg-rose-500/20 border border-rose-400/30 rounded-lg px-3 py-2">
                                {error}
                            </p>
                        )}

                        <button
                            type="submit"
                            disabled={loading}
                            className="w-full bg-emerald-500 hover:bg-emerald-400 disabled:bg-white/10 disabled:text-white/30 text-white font-bold py-3 rounded-xl transition-all text-sm uppercase tracking-wider shadow-lg shadow-emerald-900/50"
                        >
                            {loading
                                ? (isRegister ? 'Creating account...' : 'Signing in...')
                                : (isRegister ? 'Create account' : 'Sign in')}
                        </button>

                        <div className="text-center pt-1">
                            <button
                                type="button"
                                onClick={switchMode}
                                className="text-white/40 hover:text-white/70 text-xs transition-colors"
                            >
                                {isRegister
                                    ? 'Already have an account? Sign in'
                                    : "Don't have an account? Create one"}
                            </button>
                        </div>
                    </form>
                </div>
            </main>

            <footer className="px-8 py-4 text-center text-white/25 text-xs">
                Momentum Engine — Private Access
            </footer>
        </div>
    );
}
