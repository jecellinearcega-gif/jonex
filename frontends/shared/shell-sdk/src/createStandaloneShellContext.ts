import type { ShellContext, ShellUser } from './types';
import { readAccessToken, readCachedUser, clearAuthStorage } from './authStorage';
import { finishLogout } from './sessionExpired';
import { revokeServerSession } from './logout';

interface StandaloneOptions {
  appId: string;
  basePath: string;
  token?: string | null;
  user?: ShellUser | null;
  locale?: string;
  loginUrl?: string;
}

const listeners = new Map<string, Set<(payload?: unknown) => void>>();

export function createStandaloneShellContext(options: StandaloneOptions): ShellContext {
  const appId = options.appId;
  const basePath = options.basePath;

  return {
    appId,
    basePath,
    mode: 'standalone',
    token: options.token ?? readAccessToken(),
    user: options.user ?? readCachedUser<ShellUser>(),
    locale: options.locale ?? localStorage.getItem('locale') ?? 'zh',
    theme: {},

    navigate: (to: string, opts?: { replace?: boolean }) => {
      const dest = `${basePath}${to}`.replace(/\/+/g, '/');
      if (opts?.replace) {
        window.location.replace(dest);
      } else {
        window.location.href = dest;
      }
    },

    logout: async () => {
      // 与 shell 宿主同口径：先吊销服务端会话与 httpOnly refresh cookie，再清本地并跳转；
      // SSO 会话返回 sso_logout_url 时由 finishLogout 走顶层导航到 Keycloak 结束会话
      const ssoLogoutUrl = await revokeServerSession();
      clearAuthStorage({ keepLocale: true });
      finishLogout(ssoLogoutUrl, { loginUrl: options.loginUrl || '/login', appId });
    },

    getToken: () => options.token ?? readAccessToken(),
    getCurrentUser: () => options.user ?? readCachedUser<ShellUser>(),

    emitEvent: (name: string, payload?: unknown) => {
      const handlers = listeners.get(name);
      if (handlers) {
        handlers.forEach((fn) => {
          try {
            fn(payload);
          } catch {
            /* swallow */
          }
        });
      }
    },

    onEvent: (name: string, handler: (payload?: unknown) => void) => {
      if (!listeners.has(name)) {
        listeners.set(name, new Set());
      }
      listeners.get(name)!.add(handler);
      return () => {
        listeners.get(name)?.delete(handler);
      };
    },

    reportError: (error: unknown, extra?: Record<string, unknown>) => {
      console.error('[shell-sdk] Sub-app error:', error, extra);
    },

    reportMetric: (name: string, value: number, tags?: Record<string, string>) => {
      console.log('[shell-sdk] Metric:', name, value, tags);
    },
  };
}
