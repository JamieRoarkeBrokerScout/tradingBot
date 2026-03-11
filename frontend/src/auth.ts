const API_BASE = '';

const TOKEN_KEY = 'auth_token';

export interface AuthSession {
    user_id: number;
    email: string;
}

export function getStoredToken(): string | null {
    return localStorage.getItem(TOKEN_KEY);
}

function authHeaders(): HeadersInit {
    const token = getStoredToken();
    return token ? { 'Authorization': `Bearer ${token}` } : {};
}

/** Check if there is a valid stored session. Returns null if not authenticated. */
export async function getSession(): Promise<AuthSession | null> {
    const token = getStoredToken();
    if (!token) return null;

    try {
        const resp = await fetch(`${API_BASE}/api/auth/session`, {
            headers: authHeaders(),
        });
        if (resp.ok) return await resp.json();
        // Token invalid/expired — clean up
        localStorage.removeItem(TOKEN_KEY);
    } catch {
        // network error
    }
    return null;
}

/** Login with email + password. Returns session on success, throws on failure. */
export async function login(email: string, password: string): Promise<AuthSession> {
    const resp = await fetch(`${API_BASE}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
    });

    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || 'Login failed');
    }

    const data = await resp.json();
    localStorage.setItem(TOKEN_KEY, data.token);
    return { user_id: data.user_id, email: data.email };
}

/** Log out and clear stored token. */
export async function logout(): Promise<void> {
    const token = getStoredToken();
    if (token) {
        await fetch(`${API_BASE}/api/auth/logout`, {
            method: 'POST',
            headers: authHeaders(),
        }).catch(() => {});
        localStorage.removeItem(TOKEN_KEY);
    }
    window.location.reload();
}

/** Returns headers to attach to every authenticated API call. */
export { authHeaders };
