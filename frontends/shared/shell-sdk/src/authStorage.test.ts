/**
 * backupAuthForImpersonation() 的契约（契约 §3.3「第二道保险」）。
 *
 * 进模拟态前必须三件事一起做，缺一条就会复发跨租户缺陷：
 *   ① 备份原租户 access token（回原租户的唯一凭证）；
 *   ② 摘掉 localStorage 里的 refresh 键（refresh 已迁 httpOnly cookie，此行删的是空键，
 *      留着它是旧模型的残留 —— 真 cookie 由后端的标记 cookie 与前端短路兜住）；
 *   ③ **清掉残留的原租户 expires_at** —— 否则整页重载后 scheduleAutoRefresh() 会判定
 *      「即将过期」并立刻发起一次 /auth/refresh，用原租户 cookie 换回原租户 token 覆盖
 *      模拟 token。这是跨租户缺陷的加速器，也是本函数存在的理由。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  JONEX_ACCESS_TOKEN_KEY,
  JONEX_IMPERSONATION_BACKUP_KEY,
  JONEX_REFRESH_TOKEN_KEY,
  JONEX_TOKEN_EXPIRES_AT_KEY,
  backupAuthForImpersonation,
  restoreAuthFromImpersonation,
} from './authStorage';

let store: Map<string, string>;

function installStorage(): void {
  store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
  };
  vi.stubGlobal('window', { localStorage: storage });
  vi.stubGlobal('localStorage', storage);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('backupAuthForImpersonation', () => {
  it('备份原租户 token、摘掉 refresh 键、并清空残留下来的 expires_at', () => {
    installStorage();
    store.set(JONEX_ACCESS_TOKEN_KEY, 'original-access');
    store.set(JONEX_REFRESH_TOKEN_KEY, 'stale-refresh');
    store.set(JONEX_TOKEN_EXPIRES_AT_KEY, String(Date.now() + 60_000));

    backupAuthForImpersonation();

    expect(JSON.parse(store.get(JONEX_IMPERSONATION_BACKUP_KEY) as string)).toEqual({
      accessToken: 'original-access',
      refreshToken: 'stale-refresh', // 备份里如实留痕（还原时一并放回）
    });
    expect(store.has(JONEX_REFRESH_TOKEN_KEY)).toBe(false);
    expect(store.has(JONEX_TOKEN_EXPIRES_AT_KEY)).toBe(false);
  });

  it('无残留 expires_at 时也不报错（幂等）', () => {
    installStorage();
    store.set(JONEX_ACCESS_TOKEN_KEY, 'original-access');

    expect(() => backupAuthForImpersonation()).not.toThrow();
    expect(store.has(JONEX_TOKEN_EXPIRES_AT_KEY)).toBe(false);
  });
});

describe('restoreAuthFromImpersonation', () => {
  it('还原 access token 并消费掉备份（幂等：无备份视为已恢复）', () => {
    installStorage();
    store.set(JONEX_ACCESS_TOKEN_KEY, 'mock-access');
    store.set(
      JONEX_IMPERSONATION_BACKUP_KEY,
      JSON.stringify({ accessToken: 'original-access', refreshToken: null }),
    );

    restoreAuthFromImpersonation();

    expect(store.get(JONEX_ACCESS_TOKEN_KEY)).toBe('original-access');
    expect(store.has(JONEX_IMPERSONATION_BACKUP_KEY)).toBe(false);
    expect(() => restoreAuthFromImpersonation()).not.toThrow(); // 二次调用幂等
  });

  it('备份损坏时不抛异常，且备份被消费（不会反复卡在同一份坏数据上）', () => {
    installStorage();
    store.set(JONEX_ACCESS_TOKEN_KEY, 'mock-access');
    store.set(JONEX_IMPERSONATION_BACKUP_KEY, '{not-json');

    expect(() => restoreAuthFromImpersonation()).not.toThrow();
    expect(store.get(JONEX_ACCESS_TOKEN_KEY)).toBe('mock-access'); // 未被破坏
    expect(store.has(JONEX_IMPERSONATION_BACKUP_KEY)).toBe(false);
  });
});
