import { getStoredToken } from './auth';

// In production (Railway) Flask serves the frontend, so API calls are same-origin.
// Set VITE_API_BASE in your local frontend/.env for dev (e.g. http://localhost:5000).
const API_BASE = import.meta.env.VITE_API_BASE ?? '';

export { API_BASE };

export function apiFetch(path: string, options?: RequestInit): Promise<Response> {
    const token = getStoredToken();
    const authHeader: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
    return fetch(`${API_BASE}${path}`, {
        ...options,
        headers: { ...authHeader, ...(options?.headers ?? {}) },
    });
}
