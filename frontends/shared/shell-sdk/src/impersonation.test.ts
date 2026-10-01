import { afterEach, describe, expect, it, vi } from 'vitest';
import { isImpersonating, readAccessTokenExpiresAtMs } from './impersonation';
import { JONEX_ACCESS_TOKEN_KEY } from './authStorage';

/** base64url 段编码（与服务端 JWT 段同形，无 padding） */
function encodeSegment(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function makeToken(payload: Record<string, unknown>): string {
  return `${encodeSegment({ alg: 'none', typ: 'JWT' })}.${encodeSegment(payload)}.signature`;
}

function installStorage(accessToken?: string | null): Map<string, string> {
  const store = new Map<string, string>();
  if (accessToken) store.set(JONEX_ACCESS_TOKEN_KEY, accessToken);
  vi.stubGlobal('window', {
    localStorage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
    },
  });
  return store;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('isImpersonating', () => {
  it('payload.impersonated === true 时判为模拟态', () => {
    installStorage(makeToken({ sub: 'u1', tenant_id: 'tenant_b', impersonated: true }));
    expect(isImpersonating()).toBe(true);
  });

  it('普通登录 token（无 impersonated）判为非模拟态', () => {
    installStorage(makeToken({ sub: 'u1', tenant_id: 'tenant_a', exp: 4102444800 }));
    expect(isImpersonating()).toBe(false);
  });

  it('impersonated 为字符串 "true" 时判为非模拟态（严格 === true）', () => {
    installStorage(makeToken({ impersonated: 'true' }));
    expect(isImpersonating()).toBe(false);
  });

  it('本地无 access token 时判为非模拟态', () => {
    installStorage(null);
    expect(isImpersonating()).toBe(false);
  });

  it('畸形 token 判为非模拟态且不抛异常', () => {
    installStorage('not-a-jwt');
    expect(isImpersonating()).toBe(false);
    installStorage('a.@@@invalid-base64@@@.c');
    expect(isImpersonating()).toBe(false);
  });

  it('payload 非对象时不抛异常', () => {
    installStorage(makeToken('just-a-string' as unknown as Record<string, unknown>));
    expect(isImpersonating()).toBe(false);
  });
});

describe('readAccessTokenExpiresAtMs', () => {
  it('解出 exp 并换算为毫秒', () => {
    installStorage(makeToken({ exp: 1_800_000_000 }));
    expect(readAccessTokenExpiresAtMs()).toBe(1_800_000_000_000);
  });

  it('无 exp / 无 token / 畸形 token 一律返回 null', () => {
    installStorage(makeToken({ sub: 'u1' }));
    expect(readAccessTokenExpiresAtMs()).toBeNull();
    installStorage(null);
    expect(readAccessTokenExpiresAtMs()).toBeNull();
    installStorage('broken');
    expect(readAccessTokenExpiresAtMs()).toBeNull();
  });
});
