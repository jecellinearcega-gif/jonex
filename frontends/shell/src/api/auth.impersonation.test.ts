/**
 * 模拟租户会话在 shell/api 层的生命周期契约（契约 §3.3 / §3.4，BDD S1 / S3）。
 *
 * 覆盖三处：
 *   ① switchTenant() —— 进模拟态时**拿掉续期加速器**（停原定时器 + 清 expires_at），
 *      并启动模拟态到期看护；此后原租户的续期请求一个都不许发出（S1）。
 *   ② autoExitImpersonation() —— 到期自动退出的**降级顺序**是硬约束：本地还原优先，
 *      审计失败不阻塞它；连本地还原都失败才退回登出（绝不留在错身份上）。
 *   ③ clearTokens() —— 登出/会话失效时连模拟态看护一起停。
 *
 * 做法沿用 `auth.loginConfig.test.ts`：用**真实的 axios 实例**（`apiClient.raw`），
 * 只把 adapter（传输层）与全局 fetch 换掉。于是请求/响应拦截器、信封校验、`unwrap`
 * 解包、以及 shell-sdk 的真实 `isImpersonating()` / `restoreAuthFromImpersonation()`
 * 全部走生产代码 —— 被替换的只有「网线」与「整页重载」这两个不可在 node 里发生的副作用。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import {
  JONEX_ACCESS_TOKEN_KEY,
  JONEX_IMPERSONATION_BACKUP_KEY,
  JONEX_TOKEN_EXPIRES_AT_KEY,
  JONEX_USER_KEY,
  readAccessToken,
  readTokenExpiresAt,
} from '@jonex/shell-sdk';
import { apiClient, autoExitImpersonation, clearTokens, switchTenant } from './auth';
import {
  getImpersonationRemainingMs,
  startImpersonationExpiryWatch,
  stopImpersonationExpiryWatch,
} from '../utils/impersonationSession';

/** 固定时间基准（整秒，便于用整数 exp 造 token） */
const T0 = 1_700_000_000_000;

function encodeSegment(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function makeToken(payload: Record<string, unknown>): string {
  return `${encodeSegment({ alg: 'none' })}.${encodeSegment(payload)}.sig`;
}

/** 原租户（正常态）token 与切租户后拿到的模拟 token */
const ORIGINAL_TOKEN = makeToken({ sub: 'admin', tenant_id: 'tenant_a', exp: T0 / 1000 + 1800 });
const MOCK_TOKEN = makeToken({
  sub: 'admin',
  tenant_id: 'tenant_b',
  exp: T0 / 1000 + 300, // 5 分钟后到期
  impersonated: true,
});

const ME = { user_id: 'admin', username: 'admin', tenant_id: 'tenant_b' };

// ---- 环境桩 ----------------------------------------------------------------

/** 原生 fetch 的最小可用替身（只用到 ok + json）；vitest 1.x 的 vi.fn<T> 收的是**参数元组** */
type FetchResult = { ok: boolean; json: () => Promise<unknown> };
type FetchMock = ReturnType<typeof vi.fn<[string], Promise<FetchResult>>>;

let store: Map<string, string>;
let location: { href: string; origin: string; reload: ReturnType<typeof vi.fn>; assign: ReturnType<typeof vi.fn> };
let fetchMock: FetchMock;
/** 实际发出的 fetch 请求 URL（按先后顺序） */
let fetchCalls: string[];
/** 实际打到 adapter 的请求（`<method> <url>`，按先后顺序） */
let httpCalls: string[];

function makeStorage(map: Map<string, string>) {
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
}

function installGlobals(): void {
  store = new Map<string, string>();
  httpCalls = [];
  fetchCalls = [];
  location = {
    href: 'http://localhost/apps/core-business/knowledge-search',
    origin: 'http://localhost',
    reload: vi.fn(),
    assign: vi.fn(),
  };
  const storage = makeStorage(store);
  vi.stubGlobal('localStorage', storage);
  vi.stubGlobal('sessionStorage', makeStorage(new Map<string, string>()));
  vi.stubGlobal('window', {
    localStorage: storage,
    sessionStorage: makeStorage(new Map<string, string>()),
    location,
    addEventListener: () => {},
    removeEventListener: () => {},
  });
  // 续期（shell-sdk callRefresh）与登出吊销（shell-sdk revokeServerSession）都走原生 fetch。
  // 默认一律拒绝：需要用到哪条就由用例显式声明 —— 没声明却发出去的请求会当场暴露。
  fetchMock = vi.fn<[string], Promise<FetchResult>>(async (url) => {
    fetchCalls.push(url);
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
}

// ---- axios adapter 桩 ------------------------------------------------------

interface Route {
  /** 逐字符相等（'/auth/impersonate' 与 '/auth/impersonate/end' 必须区分开） */
  url: string;
  method: string;
  /** 成功信封的 data */
  data?: unknown;
  /** 给了就按该状态码 reject 一个 AxiosError */
  failWithStatus?: number;
}

const originalAdapter = apiClient.raw.defaults.adapter;

function installAdapter(routes: Route[]): void {
  const adapter: AxiosAdapter = async (config) => {
    const url = config.url ?? '';
    const method = (config.method ?? 'get').toLowerCase();
    httpCalls.push(`${method} ${url}`);
    const route = routes.find((r) => r.url === url && r.method === method);
    if (!route) throw new Error(`unexpected request: ${method} ${url}`);
    const response = makeResponse(
      config,
      route.failWithStatus
        ? { success: false, code: 5000, message: 'boom' }
        : { success: true, code: 0, data: route.data ?? null },
      route.failWithStatus ?? 200,
    );
    if (route.failWithStatus) {
      throw new AxiosError(
        'Request failed',
        AxiosError.ERR_BAD_RESPONSE,
        config,
        undefined,
        response as never,
      );
    }
    return response;
  };
  (apiClient.raw.defaults as unknown as { adapter: AxiosAdapter }).adapter = adapter;
}

function makeResponse(
  config: InternalAxiosRequestConfig,
  data: unknown,
  status = 200,
): AxiosResponse {
  return { data, status, statusText: status === 200 ? 'OK' : 'Error', headers: {}, config } as AxiosResponse;
}

/** 让登出吊销（原生 fetch）成功返回，且不是 SSO 会话（sso_logout_url 为 null） */
function allowLogoutRevoke(): void {
  fetchMock.mockImplementation(async (url) => {
    fetchCalls.push(url);
    return { ok: true, json: async () => ({ success: true, code: 0, data: { sso_logout_url: null } }) };
  });
}

// ---- 用例 ------------------------------------------------------------------

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  installGlobals();
  // impersonationSession 是模块级单例（定时器 / 订阅 / 剩余值），跨用例必须显式复位
  stopImpersonationExpiryWatch();
});

afterEach(() => {
  stopImpersonationExpiryWatch();
  (apiClient.raw.defaults as unknown as { adapter: AxiosAdapter }).adapter = originalAdapter as AxiosAdapter;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('switchTenant —— 进模拟态必须拿掉续期加速器（S1 / 契约 §3.3）', () => {
  it('清掉残留 expires_at、停掉原续期定时器：此后原租户的 refresh 一个都不发', async () => {
    // 登录态：原租户 token + 1 秒后就该触发续期的 expires_at（原缺陷的加速器）
    store.set(JONEX_ACCESS_TOKEN_KEY, ORIGINAL_TOKEN);
    store.set(JONEX_TOKEN_EXPIRES_AT_KEY, String(Date.now() + 61_000));
    installAdapter([
      { url: '/auth/impersonate', method: 'post', data: { token: MOCK_TOKEN, target_tenant_id: 'tenant_b' } },
      { url: '/auth/me', method: 'get', data: ME },
    ]);
    const { scheduleAutoRefresh } = await import('@jonex/shell-sdk');
    scheduleAutoRefresh(); // 武装登录态留下的续期定时器
    expect(vi.getTimerCount()).toBeGreaterThanOrEqual(1); // 正对照：确实有定时器可以被停掉

    await switchTenant('tenant_b');

    // 【判别式一】加速器拿掉：残留的原租户过期时间戳清零
    // （不清的话 reload 后 scheduleAutoRefresh() 会判定「即将过期」并立刻发起那次致命 refresh）
    expect(readTokenExpiresAt()).toBeNull();
    // 【判别式二】原续期定时器被停掉 —— 此刻只剩模拟态看护那一个 1 秒间隔的定时器
    expect(vi.getTimerCount()).toBe(1);
    // 模拟 token 已就位，原租户 token 完整备份（回原租户的唯一凭证）
    expect(readAccessToken()).toBe(MOCK_TOKEN);
    expect(JSON.parse(store.get(JONEX_IMPERSONATION_BACKUP_KEY) as string)).toEqual({
      accessToken: ORIGINAL_TOKEN,
      refreshToken: null,
    });
    // 用户缓存已刷新 + 整页重载（重载后由 AppShellLayout 重启看护）
    expect(JSON.parse(store.get(JONEX_USER_KEY) as string).tenantId).toBe('tenant_b');
    expect(location.reload).toHaveBeenCalledTimes(1);

    // 越过原定时器的触发点，refresh 一次都没发。
    // ⚠️ 这条**不是**判别式：模拟态短路（shell-sdk autoRefresh.test.ts 覆盖）本来就会挡住它，
    //    定时器即使存活也发不出请求。这里只作 S1 的行为兜底，判别靠上面两条。
    await vi.advanceTimersByTimeAsync(120_000);
    expect(fetchCalls).not.toContain('/api/v1/auth/refresh');
  });

  it('启动模拟态到期看护（以模拟 token 的 exp 为基准）', async () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, ORIGINAL_TOKEN);
    installAdapter([
      { url: '/auth/impersonate', method: 'post', data: { token: MOCK_TOKEN, target_tenant_id: 'tenant_b' } },
      { url: '/auth/me', method: 'get', data: ME },
    ]);

    expect(getImpersonationRemainingMs()).toBeNull(); // 前置：看护未启动
    await switchTenant('tenant_b');

    expect(getImpersonationRemainingMs()).toBe(300_000);
  });
});

describe('autoExitImpersonation —— 到期自动退出（S3 / 契约 §3.4）', () => {
  beforeEach(() => {
    // 模拟态：模拟 token 在外、原租户 token 备份在内
    store.set(JONEX_ACCESS_TOKEN_KEY, MOCK_TOKEN);
    store.set(
      JONEX_IMPERSONATION_BACKUP_KEY,
      JSON.stringify({ accessToken: ORIGINAL_TOKEN, refreshToken: null }),
    );
    store.set(JONEX_USER_KEY, JSON.stringify({ id: 'admin', tenantId: 'tenant_b' }));
  });

  it('审计 → 本地还原 → 刷新用户缓存 → 整页重载，回到原租户', async () => {
    installAdapter([
      { url: '/auth/impersonate/end', method: 'post', data: null },
      { url: '/auth/me', method: 'get', data: { ...ME, tenant_id: 'tenant_a' } },
    ]);

    await autoExitImpersonation();

    expect(httpCalls).toEqual(['post /auth/impersonate/end', 'get /auth/me']); // 审计先于刷新用户
    expect(location.reload).toHaveBeenCalledTimes(1);
    expect(readAccessToken()).toBe(ORIGINAL_TOKEN); // 已回到原租户
    expect(store.get(JONEX_IMPERSONATION_BACKUP_KEY)).toBeUndefined(); // 备份已消费
    expect(JSON.parse(store.get(JONEX_USER_KEY) as string).tenantId).toBe('tenant_a');
  });

  it('审计失败不阻塞本地还原（本地还原优先），仍回到原租户而非登出', async () => {
    installAdapter([
      { url: '/auth/impersonate/end', method: 'post', failWithStatus: 500 },
      { url: '/auth/me', method: 'get', data: { ...ME, tenant_id: 'tenant_a' } },
    ]);

    await autoExitImpersonation();

    expect(readAccessToken()).toBe(ORIGINAL_TOKEN); // 本地还原照样发生
    expect(location.reload).toHaveBeenCalledTimes(1);
    expect(location.assign).not.toHaveBeenCalled(); // 不是登出
    expect(fetchCalls).not.toContain('/api/v1/auth/logout');
  });

  it('本地还原也失败（备份缺失）⇒ 退回登出，绝不留在错身份上', async () => {
    store.delete(JONEX_IMPERSONATION_BACKUP_KEY); // 唯一凭证已丢，退不出模拟
    installAdapter([{ url: '/auth/impersonate/end', method: 'post', data: null }]);
    allowLogoutRevoke();

    await autoExitImpersonation();

    expect(location.reload).not.toHaveBeenCalled(); // 不得以模拟身份重载
    expect(fetchCalls).toContain('/api/v1/auth/logout'); // 走了服务端吊销
    expect(location.href).toContain('/login'); // 落到登录页
    expect(readAccessToken()).toBeNull(); // 已清干净
  });
});

describe('clearTokens —— 登出/会话失效时连模拟态看护一起停', () => {
  it('停掉看护定时器并清空剩余值', () => {
    store.set(JONEX_ACCESS_TOKEN_KEY, MOCK_TOKEN);
    startImpersonationExpiryWatch({ onExpired: vi.fn() });
    expect(getImpersonationRemainingMs()).toBe(300_000);

    clearTokens();

    expect(getImpersonationRemainingMs()).toBeNull();
  });
});
