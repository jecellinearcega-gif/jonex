import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  JONEX_ACCESS_TOKEN_KEY,
  JONEX_IMPERSONATION_BACKUP_KEY,
  JONEX_TOKEN_EXPIRES_AT_KEY,
  JONEX_USER_KEY,
} from './authStorage';

function encodeSegment(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function makeToken(payload: Record<string, unknown>): string {
  return `${encodeSegment({ alg: 'none' })}.${encodeSegment(payload)}.sig`;
}

const IMPERSONATION_TOKEN = makeToken({ sub: 'admin', tenant_id: 'tenant_b', impersonated: true });
const NORMAL_TOKEN = makeToken({ sub: 'admin', tenant_id: 'tenant_a' });

interface Harness {
  store: Map<string, string>;
  events: string[];
}

function installGlobals(): Harness {
  const store = new Map<string, string>();
  const events: string[] = [];
  const storage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
  class FakeCustomEvent {
    constructor(public type: string) {}
  }
  const fakeWindow: Record<string, unknown> = {
    localStorage: storage,
    sessionStorage: storage,
    dispatchEvent: (event: FakeCustomEvent) => {
      events.push(event.type);
      return true;
    },
  };
  fakeWindow.top = fakeWindow;
  fakeWindow.parent = fakeWindow;
  vi.stubGlobal('window', fakeWindow);
  vi.stubGlobal('localStorage', storage);
  vi.stubGlobal('sessionStorage', storage);
  vi.stubGlobal('CustomEvent', FakeCustomEvent);
  return { store, events };
}

let harness: Harness;

beforeEach(() => {
  vi.resetModules();
  harness = installGlobals();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** 模拟态：access token 是模拟 token，原租户凭证备份在 JONEX_IMPERSONATION_BACKUP_KEY */
function seedImpersonationSession(): void {
  harness.store.set(JONEX_ACCESS_TOKEN_KEY, IMPERSONATION_TOKEN);
  harness.store.set(
    JONEX_IMPERSONATION_BACKUP_KEY,
    JSON.stringify({ accessToken: NORMAL_TOKEN, refreshToken: null }),
  );
  harness.store.set(JONEX_USER_KEY, JSON.stringify({ id: 'admin', tenantId: 'tenant_b' }));
  harness.store.set(JONEX_TOKEN_EXPIRES_AT_KEY, String(Date.now() + 60_000));
}

describe('emitSessionExpired —— 模拟态改道', () => {
  it('不发 jonex:token-expired，改发 jonex:impersonation-expired', async () => {
    seedImpersonationSession();
    const { emitSessionExpired } = await import('./sessionExpired');

    emitSessionExpired();

    expect(harness.events).toEqual(['jonex:impersonation-expired']);
  });

  it('不清 storage：备份与模拟 token 必须原样保留（回原租户的唯一凭证）', async () => {
    seedImpersonationSession();
    const { emitSessionExpired } = await import('./sessionExpired');

    emitSessionExpired();

    expect(harness.store.get(JONEX_IMPERSONATION_BACKUP_KEY)).toBe(
      JSON.stringify({ accessToken: NORMAL_TOKEN, refreshToken: null }),
    );
    expect(harness.store.get(JONEX_ACCESS_TOKEN_KEY)).toBe(IMPERSONATION_TOKEN);
    expect(harness.store.get(JONEX_USER_KEY)).not.toBeUndefined();
  });

  it('调试开关 jonex_disable_401_redirect=1 时两条路都静默跳过', async () => {
    seedImpersonationSession();
    harness.store.set('jonex_disable_401_redirect', '1');
    const { emitSessionExpired } = await import('./sessionExpired');

    emitSessionExpired();

    expect(harness.events).toEqual([]);
  });
});

describe('emitSessionExpired —— 非模拟态路径一个字节都不变', () => {
  it('清 storage 并只发 jonex:token-expired', async () => {
    harness.store.set(JONEX_ACCESS_TOKEN_KEY, NORMAL_TOKEN);
    harness.store.set(JONEX_USER_KEY, JSON.stringify({ id: 'admin', tenantId: 'tenant_a' }));
    harness.store.set(JONEX_TOKEN_EXPIRES_AT_KEY, String(Date.now() + 60_000));
    harness.store.set('locale', 'zh');
    const { emitSessionExpired } = await import('./sessionExpired');

    emitSessionExpired();

    expect(harness.events).toEqual(['jonex:token-expired']);
    expect(harness.store.get(JONEX_ACCESS_TOKEN_KEY)).toBeUndefined();
    expect(harness.store.get(JONEX_TOKEN_EXPIRES_AT_KEY)).toBeUndefined();
    expect(harness.store.get('locale')).toBe('zh');
  });
});
