import type { ShellUser } from './types';
import {
  readAccessToken,
  writeAccessToken,
  writeRefreshToken,
  writeTokenExpiresAt,
  writeCachedUser,
} from './authStorage';
import { hasTicketCallback, getTicketCallback, stripBlockedAuthQuery, stripCallbackQuery } from './authRedirect';
import { emitSessionExpired, redirectToLogin } from './sessionExpired';
import { refreshTokens, scheduleAutoRefresh } from './autoRefresh';

export interface AuthBootstrapOptions {
  appId: string;
  loginUrl: string;
  authMeUrl?: string;
  exchangeTicketUrl?: string;
  currentUrl?: string;
  fetchImpl?: typeof fetch;
}

export interface AuthBootstrapResult {
  authenticated: boolean;
  token: string | null;
  user: ShellUser | null;
  source: 'ticket' | 'local-token' | 'none';
}

interface ApiEnvelope<T> {
  success: boolean;
  code?: number;
  message?: string;
  data?: T;
}

interface AuthenticatedPayload {
  status?: 'authenticated';
  access_token?: string;
  accessToken?: string;
  refresh_token?: string;
  refreshToken?: string;
  expires_in?: number;
  user?: Record<string, unknown>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function unwrapEnvelope<T>(payload: unknown): T | null {
  if (!isRecord(payload)) return null;
  if ('success' in payload && 'data' in payload) {
    const envelope = payload as unknown as ApiEnvelope<T>;
    return envelope.success && envelope.data ? envelope.data : null;
  }
  return payload as T;
}

function normalizeUser(raw: Record<string, unknown>): ShellUser | null {
  const id = raw.user_id || raw.id;
  const username = raw.username;
  if (!id || !username) return null;

  const displayName = raw.display_name || raw.displayName || username;
  const tenantId = raw.tenant_id || raw.tenantId;
  const tenantName = raw.tenant_name || raw.tenantName;
  const roles = Array.isArray(raw.roles) ? raw.roles.map(String) : raw.role ? [String(raw.role)] : [];
  // 幂等：同时兜底 snake_case（后端原始）与 camelCase（已归一化对象二次进入）
  const isPlatformAdmin =
    raw.is_platform_admin === true || (raw as Record<string, unknown>).isPlatformAdmin === true;
  const isTenantAdmin =
    raw.is_tenant_admin === true || (raw as Record<string, unknown>).isTenantAdmin === true;
  const permissions: string[] = Array.isArray(raw.permissions) ? raw.permissions.map(String) : [];

  return {
    id: String(id),
    username: String(username),
    displayName: String(displayName),
    tenantId: tenantId ? String(tenantId) : undefined,
    tenantName: tenantName ? String(tenantName) : undefined,
    roles,
    isPlatformAdmin,
    isTenantAdmin,
    permissions,
  };
}

export async function bootstrapStandaloneAuth(options: AuthBootstrapOptions): Promise<AuthBootstrapResult> {
  const fetchFn = options.fetchImpl || window.fetch.bind(window);
  const currentUrl = options.currentUrl || window.location.href;
  const url = new URL(currentUrl);

  // 1. 清理历史 URL token 参数
  const originalUrl = url.toString();
  stripBlockedAuthQuery(url);
  if (url.toString() !== originalUrl) {
    window.history.replaceState({}, '', url.toString());
  }

  // 2. 如果有 ticket/code，尝试交换 token
  if (hasTicketCallback(url) && options.exchangeTicketUrl) {
    try {
      const cb = getTicketCallback(url);
      const resp = await fetchFn(options.exchangeTicketUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          appId: options.appId,
          ticket: cb.ticket,
          code: cb.code,
          state: cb.state,
          redirectUri: window.location.origin + window.location.pathname,
        }),
      });

      if (resp.ok) {
        const payload = await resp.json();
        const data = unwrapEnvelope<AuthenticatedPayload>(payload);
        const accessToken = data?.access_token || data?.accessToken;
        const user = data?.user ? normalizeUser(data.user) : null;
        if (accessToken) {
          writeAccessToken(accessToken);
          const refreshToken = data?.refresh_token || data?.refreshToken;
          if (refreshToken) {
            writeRefreshToken(refreshToken);
          }
          if (typeof data?.expires_in === 'number' && data.expires_in > 0) {
            writeTokenExpiresAt(Date.now() + data.expires_in * 1000);
          }
          if (user) writeCachedUser(user);
          // 清理 URL 中的 ticket/code/state
          stripCallbackQuery(url);
          window.history.replaceState({}, '', url.toString());
          // ticket 交换成功同样启动定时自动刷新（滑动续期）
          scheduleAutoRefresh();
          return { authenticated: true, token: accessToken, user: user || null, source: 'ticket' };
        }
      }
    } catch (err) {
      console.error('[authBootstrap] Ticket exchange failed:', err);
    }
  }

  // URL 中有 ticket/code 但交换失败或未配置 exchange endpoint
  // 清理 URL 参数避免残留
  if (hasTicketCallback(url)) {
    stripCallbackQuery(url);
    window.history.replaceState({}, '', url.toString());
  }

  // 3. 取本域 token：本域没有时，先尝试用 httpOnly refresh cookie 换一次。
  //    SSO 回调只下发 refresh cookie、不下发 access token，宿主 shell 靠 RequireAuth 里的
  //    refreshTokens() 接住（frontends/shell/src/App.tsx），standalone 这条启动路径原先直接
  //    跳登录、把登录态丢了。须在 redirectToLogin 之前：/auth/refresh 无 cookie 时快速 401
  //    （RefreshTokenRevokedError），换不到再走第 4 步跳登录。
  let token = readAccessToken();
  if (!token && (await refreshTokens())) {
    token = readAccessToken();
    // 换到了新的 access：启动定时自动刷新（滑动续期），与 ticket 交换成功路径一致
    if (token) scheduleAutoRefresh();
  }
  if (token) {
    if (options.authMeUrl) {
      try {
        const resp = await fetchFn(options.authMeUrl, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (resp.ok) {
          const payload = await resp.json();
          const data = unwrapEnvelope<Record<string, unknown>>(payload);
          const rawUser = data && isRecord(data.user) ? data.user : data;
          const user = rawUser ? normalizeUser(rawUser) : null;
          if (user) {
            writeCachedUser(user);
            return { authenticated: true, token, user, source: 'local-token' };
          }
        }
        // 401 或其他错误 → 发会话失效信号（清 token + 事件），跳转由监听者统一执行
        if (resp.status === 401 || resp.status === 403) {
          emitSessionExpired();
          return { authenticated: false, token: null, user: null, source: 'none' };
        }
      } catch {
        // 网络错误，信任本地 token
        return { authenticated: true, token, user: null, source: 'local-token' };
      }
    }
    // 没有配置 authMeUrl，信任本地 token
    return { authenticated: true, token, user: null, source: 'local-token' };
  }

  // 4. 无任何登录态 → 跳转登录页（首次未登录引导，走统一跳转）
  redirectToLogin({ loginUrl: options.loginUrl, appId: options.appId });
  return { authenticated: false, token: null, user: null, source: 'none' };
}
