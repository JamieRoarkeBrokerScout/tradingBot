import { useState, useEffect } from 'react';
import { getSession, type AuthSession } from './auth';
import LandingPage from './pages/LandingPage';
import LoginPage from './pages/LoginPage';
import Dashboard from './pages/Dashboard';

type View = 'landing' | 'login';

export default function App() {
    const [view, setView] = useState<View>('landing');
    const [session, setSession] = useState<AuthSession | null>(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        getSession().then(s => {
            if (s) setSession(s);
            setLoading(false);
        });
    }, []);

    if (loading) {
        return (
            <div className="min-h-screen bg-slate-950 flex items-center justify-center">
                <div className="text-slate-500 font-mono text-sm animate-pulse">Loading...</div>
            </div>
        );
    }

    if (session) return <Dashboard session={session} />;

    if (view === 'login') {
        return <LoginPage onLogin={s => setSession(s)} />;
    }

    return <LandingPage onSignInClick={() => setView('login')} />;
}
