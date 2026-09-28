/**
 * `fetchUiConfig` —— 外壳 UI 开关的取值（顶栏「任务中心」按钮显隐）。
 *
 * 与 `auth.loginConfig.test.ts` 同一套手法：用**真实的 axios 实例**，只把 adapter
 * （传输层）换掉 —— 于是请求/响应拦截器、信封校验、`unwrap` 解包全走生产代码，
 * 被替换的只有「网线」。
 *
 * ⚠ 兜底方向是**不隐藏**（与登录页那边的「拿不到配置就隐藏 SSO 入口」相反）：
 * 外壳不该因为一个开关读不到就少/多一个入口，而且「没配开关的环境莫名少入口」
 * 比「多一个快捷按钮」更难被发现。①–③ 就是在钉这个方向。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import { apiClient } from './auth';
import { fetchUiConfig } from './uiConfig';

/** 后端新口的路径（与 platform_crud.py 的 @router.get 一致） */
const UI_CONFIG_URL = '/platform/frontend/ui-config';

/** 内存版 localStorage：拦截器要读 token 与 locale，node 环境没有原生实现 */
function installLocalStorage(): void {
  const map = new Map<string, string>();
  const store = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
  // ⚠ 必须同时注入 window：shell-sdk 的 getStorage() 以 typeof window === 'undefined'
  // 判空（authStorage.ts:10-13），只 stub globalThis.localStorage 会让读写静默落空。
  vi.stubGlobal('localStorage', store);
  vi.stubGlobal('window', { localStorage: store });
}

/** 构造与真实 adapter 同形的 axios 响应壳 */
function makeResponse(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return {
    data,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: {},
    config,
  } as AxiosResponse;
}

type AdapterSlot = { adapter: AxiosAdapter | undefined };

function adapterSlot(): AdapterSlot {
  return apiClient.raw.defaults as unknown as AdapterSlot;
}

/** 记录本次请求的 URL，用来钉「前端确实打的是新口、不是 login-config」 */
let requestedUrls: string[] = [];

function stubAdapter(adapter: AxiosAdapter): void {
  adapterSlot().adapter = adapter;
}

const originalAdapter = apiClient.raw.defaults.adapter;

describe('fetchUiConfig · 外壳 UI 开关取值', () => {
  beforeEach(() => {
    installLocalStorage();
    requestedUrls = [];
  });

  afterEach(() => {
    adapterSlot().adapter = originalAdapter as AxiosAdapter;
    vi.unstubAllGlobals();
  });

  /** 包一层：记录 URL 后交给行为桩 */
  function withUrl(fn: AxiosAdapter): AxiosAdapter {
    return (config) => {
      requestedUrls.push(String(config.url ?? ''));
      return fn(config);
    };
  }

  // ④ 正对照（放前面读起来顺）：配了 HIDE_TASK_CENTER=true ⇒ 前端拿到 true ⇒ 顶栏不渲染。
  //    没有这条，「①–③ 恒为 false」可能只是因为整条链路压根没跑通（断言恒真的假绿）。
  it('后端下发 hide_task_center=true ⇒ hideTaskCenter 为 true（顶栏隐藏）', async () => {
    stubAdapter(
      withUrl(async (config) =>
        makeResponse(config, { success: true, code: 0, data: { hide_task_center: true } }, 200),
      ),
    );

    const cfg = await fetchUiConfig();

    expect(cfg.hideTaskCenter).toBe(true);
    // 钉住打的是新口：若误用 /auth/login-config（登录页专用、不认这个键），
    // 隐藏会在配了开关的环境里静默失效。
    expect(requestedUrls).toEqual([expect.stringContaining(UI_CONFIG_URL)]);
    expect(requestedUrls[0]).not.toContain('login-config');
  });

  it('后端下发 hide_task_center=false ⇒ 不隐藏（未配开关的环境就是这个形态）', async () => {
    stubAdapter(async (config) =>
      makeResponse(config, { success: true, code: 0, data: { hide_task_center: false } }, 200),
    );

    expect((await fetchUiConfig()).hideTaskCenter).toBe(false);
  });

  // ① 滚动发布组合：新前端 + 旧后端，200 且信封合法，但 data 里没有这个键
  it('① data 缺 hide_task_center ⇒ 回落「不隐藏」（不静默藏入口）', async () => {
    stubAdapter(async (config) => makeResponse(config, { success: true, code: 0, data: {} }, 200));

    expect((await fetchUiConfig()).hideTaskCenter).toBe(false);
  });

  it('② data 里给的是字符串 "true" ⇒ 仍按不隐藏（只认严格布尔，不替后端转义）', async () => {
    stubAdapter(async (config) =>
      makeResponse(config, { success: true, code: 0, data: { hide_task_center: 'true' } }, 200),
    );

    expect((await fetchUiConfig()).hideTaskCenter).toBe(false);
  });

  // ③ 传输失败：5xx（网关/上游挂掉时的真实形态）
  it('③ 5xx 传输失败 ⇒ 兜底「不隐藏」，且不抛异常', async () => {
    stubAdapter(async (config) => {
      const response = makeResponse(config, '<html>502 Bad Gateway</html>', 502);
      throw new AxiosError(
        'Request failed with status code 502',
        AxiosError.ERR_BAD_RESPONSE,
        config,
        undefined,
        response as never,
      );
    });

    await expect(fetchUiConfig()).resolves.toEqual({ hideTaskCenter: false });
  });

  it('③b HTTP 200 但响应体不是 JSON ⇒ 兜底「不隐藏」（被网关拦成登录页的形态）', async () => {
    stubAdapter(async (config) => makeResponse(config, '<html>please login</html>', 200));

    await expect(fetchUiConfig()).resolves.toEqual({ hideTaskCenter: false });
  });
});
