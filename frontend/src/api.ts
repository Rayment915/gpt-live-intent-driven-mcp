import { message, object, type CatalogStatus, type JsonObject, type StudioConfig, type Transport } from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { credentials: 'same-origin', signal: AbortSignal.timeout(60000), ...init });
  } catch (error) {
    throw new Error(`后端连接失败：${message(error)}。请确认 Python 服务已启动。`);
  }
  const text = await response.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`后端返回非 JSON 内容（HTTP ${response.status}）。请检查 /api 路由与开发代理。`);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error(`后端 JSON 响应必须为对象（HTTP ${response.status}）`);
  if (!response.ok) {
    const body = object(data);
    if (response.status === 401 && !path.startsWith('/api/auth')) {
      window.dispatchEvent(new Event('studio:login-required'));
    }
    throw new Error(`HTTP ${response.status} · ${typeof body.detail === 'string' ? body.detail : typeof body.error === 'string' ? body.error : JSON.stringify(body.detail ?? body.error ?? body)}`);
  }
  return data as T;
}

export const getConfig = (signal?: AbortSignal) => request<StudioConfig>('/api/config', { signal });

export interface AuthInfo {
  required: boolean;
  authenticated: boolean;
  csrf: string;
  username: string | null;
}

export const getAuth = () => request<AuthInfo>('/api/auth', { signal: AbortSignal.timeout(15000) });
export const login = (username: string, password: string, csrf: string) =>
  post<AuthInfo>('/api/auth/login', { username, password }, csrf);
export const logout = (csrf: string) => post<{ authenticated: boolean }>('/api/auth/logout', {}, csrf);

function post<T>(path: string, body: unknown, csrf: string) {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Studio-Csrf': csrf }, body: JSON.stringify(body) });
}

export function createSession(session: JsonObject, transport: Transport, csrf: string, sdp?: string) {
  return post<{ id: string; session: JsonObject; sdp?: string }>('/api/sessions', { session, transport, ...(sdp ? { sdp } : {}) }, csrf);
}

export const sendCommand = (id: string, event: JsonObject, csrf: string, experimental = false) =>
  post<JsonObject>(`/api/sessions/${encodeURIComponent(id)}/commands`, { event, ...(experimental ? { experimental: true } : {}) }, csrf);

export const refreshCatalog = (servers: string[], csrf: string) =>
  post<{ catalogs: CatalogStatus[] }>('/api/mcp/catalog/refresh', { servers }, csrf);

export function eventsUrl(id: string): string {
  const url = new URL(`/api/sessions/${encodeURIComponent(id)}/events`, location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.href;
}
