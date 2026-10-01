import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { JONEX_ACCESS_TOKEN_KEY, JONEX_TOKEN_EXPIRES_AT_KEY } from './authStorage';

function encodeSegment(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function makeToken(payload: Record<string, unknown>): string {
  return `${encodeSegment({ alg: 'none' })}.${encodeSegment(payload)}.sig`;
}

const IMPERSONATION_TOKEN = makeToken({ sub: 'admin', tenant_id: 'tenant_b', impersonated: true });
const NORMAL_TOKEN = makeToken({ sub: 'admin', tenant_id: 'tenant_a' });

let store: Map<string, string>;

beforeEach(() => {
  vi.resetModules();
  store = new Map<string, string>();
  const storage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
  vi.stubGlobal('window', { localStorage: storage });
  vi.stubGlobal('localStorage', storage);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stubFetchOk(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    json: async () => ({ success: true, data: { access_token: 'refreshed-token', expires_in: 3600 } }),
  }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('callRefresh —— 模拟态硬不变量', () => {
  it('模拟态短路：不发任何 /auth/refresh 请求，且不改写 access token', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, IMPERSONATION_TOKEN);
    const fetchMock = stubFetchOk();
    const { refreshTokens } = await import('./autoRefresh');

    await expect(refreshTokens()).resolves.toBe(false);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(store.get(JONEX_ACCESS_TOKEN_KEY)).toBe(IMPERSONATION_TOKEN);
  });

  it('模拟态短路早于单飞登记：后续调用同样一次请求都不发', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, IMPERSONATION_TOKEN);
    const fetchMock = stubFetchOk();
    const { refreshTokens } = await import('./autoRefresh');

    const results = await Promise.all([refreshTokens(), refreshTokens(), refreshTokens()]);

    expect(results).toEqual([false, false, false]);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('callRefresh —— 非模拟态行为不变', () => {
  it('非模拟态照常发 refresh 并写入新 token', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, NORMAL_TOKEN);
    const fetchMock = stubFetchOk();
    const { refreshTokens } = await import('./autoRefresh');

    await expect(refreshTokens()).resolves.toBe(true);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/auth/refresh');
    expect(store.get(JONEX_ACCESS_TOKEN_KEY)).toBe('refreshed-token');
  });

  it('无 access token 时也照常发 refresh（SSO 首屏：只带 httpOnly cookie）', async () => {
    const fetchMock = stubFetchOk();
    const { refreshTokens } = await import('./autoRefresh');

    await expect(refreshTokens()).resolves.toBe(true);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('scheduleAutoRefresh —— 模拟态不排定时器（expires_at 已被清空）', () => {
  it('expires_at 为 null 时不排定时器，也不发请求', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, IMPERSONATION_TOKEN);
    const fetchMock = stubFetchOk();
    const { scheduleAutoRefresh, stopAutoRefresh } = await import('./autoRefresh');

    scheduleAutoRefresh();
    stopAutoRefresh();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(store.get(JONEX_TOKEN_EXPIRES_AT_KEY)).toBeUndefined();
  });
});
