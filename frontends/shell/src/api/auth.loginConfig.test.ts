/**
 * 验收 #6 兜底实测：`GET /auth/login-config` 拿不到可用配置时，登录页 SSO 入口
 * 必须**隐藏**（`enabled === false`），不得静默显示。
 *
 * 覆盖三例 + 一条正对照：
 *   ① 传输失败·5xx         ② 传输失败·非 JSON
 *   ③ HTTP 200 但 data 无 `enabled`   ④ 正对照：200 且 data.enabled=true ⇒ 显示
 *
 * 做法：用**真实的 axios 实例**（`apiClient.raw`），只把 adapter（传输层）换掉。
 * 于是请求/响应拦截器、信封校验、`unwrap` 解包、`fetchLoginConfig` 的字段解析
 * 全部走生产代码 —— 被替换的只有「网线」。正对照用来排除「断言恒真」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import { apiClient, fetchLoginConfig } from './auth';

/** 内存版 localStorage：拦截器要读 token 与 locale，node 环境没有原生实现 */
function installLocalStorage(): void {
  const map = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  });
  vi.stubGlobal('window', { addEventListener: () => {}, removeEventListener: () => {} });
}

/** 构造与真实 adapter 同形的 axios 响应壳 */
function makeResponse(
  config: InternalAxiosRequestConfig,
  data: unknown,
  status = 200,
): AxiosResponse {
  return {
    data,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: {},
    config,
    // axios 的响应头是 RawAxiosResponseHeaders 交集体，空对象足矣，故整体断言
  } as AxiosResponse;
}

/** adapter 槽位在 axios 里接受「名字符串 | 函数 | 数组」，测试只塞函数，统一收窄 */
type AdapterSlot = { adapter: AxiosAdapter | undefined };

function adapterSlot(): AdapterSlot {
  return apiClient.raw.defaults as unknown as AdapterSlot;
}

/** 把 adapter 换成固定行为的桩，返回受控响应 */
function stubAdapter(adapter: AxiosAdapter): void {
  adapterSlot().adapter = adapter;
}

const originalAdapter = apiClient.raw.defaults.adapter;

describe('验收 #6 · fetchLoginConfig 兜底实测', () => {
  beforeEach(() => {
    installLocalStorage();
  });

  afterEach(() => {
    adapterSlot().adapter = originalAdapter as AxiosAdapter;
    vi.unstubAllGlobals();
  });

  // ① 传输失败：5xx（网关/上游挂掉时的真实形态：状态码非 2xx + 非 JSON 的错误页）
  it('① 5xx 传输失败 ⇒ enabled 回落 false（入口隐藏）', async () => {
    stubAdapter(async (config) => {
      const response = makeResponse(config, '<html>502 Bad Gateway</html>', 502);
      // 与真实 adapter 的 settle() 同形：非 2xx 一律 reject 一个 AxiosError 并挂上 response
      throw new AxiosError(
        'Request failed with status code 502',
        AxiosError.ERR_BAD_RESPONSE,
        config,
        undefined,
        response as never,
      );
    });

    const cfg = await fetchLoginConfig();

    expect(cfg.enabled).toBe(false);
    // 兜底不得把租户框也一起藏掉（登录页必须仍然可用）
    expect(cfg.hideTenantInput).toBe(false);
    expect(cfg.defaultTenantId).toBe('');
  });

  // ② 传输失败：HTTP 200 但响应体不是 JSON（典型：被 Nginx/登录门户拦截返回 HTML）
  it('② 非 JSON 响应 ⇒ enabled 回落 false（入口隐藏）', async () => {
    stubAdapter(async (config) => makeResponse(config, '<html>please login</html>', 200));

    const cfg = await fetchLoginConfig();

    expect(cfg.enabled).toBe(false);
  });

  // ③ 滚动发布组合：新前端 + 旧后端，200 且信封合法，但 data 里没有 enabled 字段
  it('③ HTTP 200 但 data 缺 enabled ⇒ enabled 回落 false（不静默显示）', async () => {
    stubAdapter(async (config) =>
      makeResponse(
        config,
        { success: true, code: 0, data: { default_tenant_id: '', hide_tenant_input: false } },
        200,
      ),
    );

    const cfg = await fetchLoginConfig();

    expect(cfg.enabled).toBe(false);
    expect(cfg.provider).toBeUndefined();
  });

  // ④ 正对照：四键齐全且 enabled=true ⇒ 入口显示。
  //    没有这条，「①–③ 恒为 false」可能只是因为整条链路压根没跑通（断言恒真的假绿）。
  it('④ 正对照：200 且 data.enabled=true ⇒ enabled 为 true（入口显示）', async () => {
    stubAdapter(async (config) =>
      makeResponse(
        config,
        {
          success: true,
          code: 0,
          data: {
            default_tenant_id: 'tenant_jonex_demo',
            hide_tenant_input: true,
            enabled: true,
            provider: 'keycloak',
          },
        },
        200,
      ),
    );

    const cfg = await fetchLoginConfig();

    expect(cfg.enabled).toBe(true);
    expect(cfg.provider).toBe('keycloak');
    expect(cfg.defaultTenantId).toBe('tenant_jonex_demo');
    expect(cfg.hideTenantInput).toBe(true);
  });
});

