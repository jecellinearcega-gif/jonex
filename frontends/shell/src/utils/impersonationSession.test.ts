import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { JONEX_ACCESS_TOKEN_KEY } from '@jonex/shell-sdk';

/** 固定时间基准（整秒，便于用整数 exp 造 token） */
const T0 = 1_700_000_000_000;

function encodeSegment(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function makeToken(payload: Record<string, unknown>): string {
  return `${encodeSegment({ alg: 'none' })}.${encodeSegment(payload)}.sig`;
}

/** 造一个 exp = T0 + seconds 的 token */
function tokenExpiringIn(seconds: number, impersonated = true): string {
  return makeToken({ sub: 'admin', tenant_id: 'tenant_b', exp: T0 / 1000 + seconds, impersonated });
}

let store: Map<string, string>;

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  vi.setSystemTime(T0);
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function loadModule() {
  return import('./impersonationSession');
}

describe('startImpersonationExpiryWatch —— 排定时器的前提', () => {
  it('非模拟态：不排任何定时器，不回调', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, tokenExpiringIn(120, false));
    const onWarn = vi.fn();
    const onExpired = vi.fn();
    const { startImpersonationExpiryWatch } = await loadModule();

    startImpersonationExpiryWatch({ onWarn, onExpired });
    vi.advanceTimersByTime(300_000);

    expect(onWarn).not.toHaveBeenCalled();
    expect(onExpired).not.toHaveBeenCalled();
  });

  it('无 access token：不排任何定时器', async () => {
    const onExpired = vi.fn();
    const { startImpersonationExpiryWatch } = await loadModule();

    startImpersonationExpiryWatch({ onExpired });
    vi.advanceTimersByTime(300_000);

    expect(onExpired).not.toHaveBeenCalled();
  });

  it('模拟 token 解不出 exp：不排定时器（判不准就不动）', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, makeToken({ impersonated: true }));
    const onExpired = vi.fn();
    const { startImpersonationExpiryWatch } = await loadModule();

    startImpersonationExpiryWatch({ onExpired });
    vi.advanceTimersByTime(300_000);

    expect(onExpired).not.toHaveBeenCalled();
  });

  it('幂等：重复调用不会叠加定时器 / 不会重复提示', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, tokenExpiringIn(120));
    const onWarn = vi.fn();
    const { startImpersonationExpiryWatch } = await loadModule();

    startImpersonationExpiryWatch({ onWarn, onExpired: vi.fn() });
    startImpersonationExpiryWatch({ onWarn, onExpired: vi.fn() });
    vi.advanceTimersByTime(61_000);

    expect(onWarn).toHaveBeenCalledTimes(1);
  });
});

describe('到期前 60 秒提示（S2）', () => {
  it('未到 60 秒窗口不提示；进入窗口后提示恰好一次，会话剩余期间不重复', async () => {
    const expiresIn = 300; // 5 分钟后到期
    store.set(JONEX_ACCESS_TOKEN_KEY, tokenExpiringIn(expiresIn));
    const onWarn = vi.fn();
    const { startImpersonationExpiryWatch } = await loadModule();
    startImpersonationExpiryWatch({ onWarn, onExpired: vi.fn() });

    // 4 分钟时：距到期还有 60 秒 —— 边界上就应提示
    vi.advanceTimersByTime(239_000);
    expect(onWarn).not.toHaveBeenCalled();

    vi.advanceTimersByTime(2_000);
    expect(onWarn).toHaveBeenCalledTimes(1);

    // 余下 59 秒每秒 tick，仍只有一次
    vi.advanceTimersByTime(58_000);
    expect(onWarn).toHaveBeenCalledTimes(1);

    const { getImpersonationRemainingMs } = await loadModule();
    expect(getImpersonationRemainingMs()).toBeLessThanOrEqual(60_000);
  });

  it('横幅订阅者能拿到剩余毫秒并随 tick 递减', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, tokenExpiringIn(120));
    const seen: (number | null)[] = [];
    const { startImpersonationExpiryWatch, subscribeImpersonationRemaining } = await loadModule();

    subscribeImpersonationRemaining((ms) => seen.push(ms));
    startImpersonationExpiryWatch({ onExpired: vi.fn() });

    expect(seen[seen.length - 1]).toBe(120_000);
    vi.advanceTimersByTime(1_000);
    expect(seen[seen.length - 1]).toBe(119_000);
  });
});

describe('到期自动退出模拟（S3）', () => {
  it('exp 到达时回调 onExpired 恰好一次，之后不再重复', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, tokenExpiringIn(90));
    const onExpired = vi.fn();
    const { startImpersonationExpiryWatch } = await loadModule();
    startImpersonationExpiryWatch({ onExpired });

    vi.advanceTimersByTime(89_000);
    expect(onExpired).not.toHaveBeenCalled();

    vi.advanceTimersByTime(2_000);
    expect(onExpired).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(300_000);
    expect(onExpired).toHaveBeenCalledTimes(1);
  });

  it('onExpired 抛异常不会把定时器循环打崩（不产生未处理拒绝）', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, tokenExpiringIn(10));
    const onExpired = vi.fn(() => {
      throw new Error('boom');
    });
    const { startImpersonationExpiryWatch } = await loadModule();
    startImpersonationExpiryWatch({ onExpired });

    expect(() => vi.advanceTimersByTime(20_000)).not.toThrow();
    expect(onExpired).toHaveBeenCalledTimes(1);
  });
});

describe('formatImpersonationRemaining', () => {
  it('向上取整到秒并输出 mm:ss，负数夹到 00:00', async () => {
    const { formatImpersonationRemaining } = await loadModule();
    expect(formatImpersonationRemaining(60_000)).toBe('01:00');
    expect(formatImpersonationRemaining(59_001)).toBe('01:00');
    expect(formatImpersonationRemaining(59_000)).toBe('00:59');
    expect(formatImpersonationRemaining(1)).toBe('00:01');
    expect(formatImpersonationRemaining(0)).toBe('00:00');
    expect(formatImpersonationRemaining(-5_000)).toBe('00:00');
  });
});
