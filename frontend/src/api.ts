import { getStoredToken } from './auth';

// Runtime detection: use same-origin in production, localhost:5000 in local dev.
const API_BASE = window.location.hostname === 'localhost'
    ? 'http://localhost:5000'
    : '';

export { API_BASE };

export function apiFetch(path: string, options?: RequestInit): Promise<Response> {
    const token = getStoredToken();
    const authHeader: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
    return fetch(`${API_BASE}${path}`, {
        ...options,
        headers: { ...authHeader, ...(options?.headers ?? {}) },
    });
}
