import { getStoredToken } from './auth';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:5000';

export { API_BASE };

export function apiFetch(path: string, options?: RequestInit): Promise<Response> {
    const token = getStoredToken();
    const authHeader: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
    return fetch(`${API_BASE}${path}`, {
        ...options,
        headers: { ...authHeader, ...(options?.headers ?? {}) },
    });
}
