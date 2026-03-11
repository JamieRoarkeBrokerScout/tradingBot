import { getStoredToken } from './auth';

// Always use relative paths — Flask serves both API and frontend in production.
// Local dev: Vite proxies /api/* to localhost:5000 (see vite.config.ts).
const API_BASE = '';

export { API_BASE };

export function apiFetch(path: string, options?: RequestInit): Promise<Response> {
    const token = getStoredToken();
    const authHeader: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
    return fetch(`${API_BASE}${path}`, {
        ...options,
        headers: { ...authHeader, ...(options?.headers ?? {}) },
    });
}
