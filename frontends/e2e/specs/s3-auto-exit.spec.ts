/**
 * S3 到期自动退出模拟回原租户（主流程）
 * 契约：docs/auth/tenant-impersonation-refresh-isolation-plan.md §4 S3、§3.4（exp 那一行）
 *
 * Given 管理员已切换到 tenant_jonex_sso
 * When  模拟 token 到期
 * Then  记录一条 impersonate_expired 审计
 * And   管理员回到 tenant_jonex_demo 会话
 * And   后续请求的 Bearer 属于 tenant_jonex_demo
 *
 * 【前置】后端 IMPERSONATE_EXPIRE_MINUTES=1（用 E2E_IMPERSONATE_EXPIRE_MINUTES=1 对齐），
 * 否则要真等 30 分钟。运行方式见 README.md「两轮跑法」。
 */
import { expect, test } from '@playwright/test';
import {
  cfg,
  decodeJwtPayload,
  expectImpersonationActive,
  expectTokenTenant,
  installEventRecorder,
  login,
  navigatedToLogin,
  readEvents,
  readStorage,
  recordNavigations,
  recordNetwork,
  remainingSeconds,
  sleep,
  switchTenantViaUi,
  waitForAutoExit,
  EVENTS,
} from '../lib/harness';

/** 容忍不同包装层级：把响应体里所有 action 字段抠出来 */
function collectActions(node: unknown, out: string[] = []): string[] {
  if (Array.isArray(node)) {
    for (const item of node) collectActions(item, out);
    return out;
  }
  if (node && typeof node === 'object') {
    const rec = node as Record<string, unknown>;
    if (typeof rec.action === 'string') out.push(rec.action);
    for (const v of Object.values(rec)) collectActions(v, out);
  }
  return out;
}

test.describe('S3 到期自动退出', () => {
  test('到期后回到原租户、写 impersonate_expired 审计、后续 Bearer 属原租户', async ({ page }) => {
    const c = cfg();
    test.setTimeout(300_000);

    await login(page);
    const net = recordNetwork(page);
    const navs = recordNavigations(page);
    await installEventRecorder(page, [EVENTS.tokenExpired, EVENTS.impersonationExpired]);
    await switchTenantViaUi(page, c.targetTenant);
    await expectImpersonationActive(page, c.targetTenant);

    const payload = decodeJwtPayload((await readStorage(page)).accessToken as string)!;
    const remaining = remainingSeconds(payload);
    expect(
      remaining,
      `模拟 token 剩余 ${remaining}s ⇒ E2E_IMPERSONATE_EXPIRE_MINUTES 与后端 IMPERSONATE_EXPIRE_MINUTES 不一致，或 token 已过期`,
    ).toBeLessThanOrEqual(c.impersonateExpireMinutes * 60 + 10);
    expect(remaining, 'token 已过期 ⇒ 无法观测「到期那一刻」，本用例结论无效').toBeGreaterThan(15);

    // ---------- 等到到期（+12s 让定时器与收尾都跑完） ----------
    await sleep((remaining + 12) * 1000);
    await waitForAutoExit(page);

    // ---------- 回到原租户会话 ----------
    await expectTokenTenant(page, c.tenant, false, '自动退出后');
    const afterExit = await readStorage(page);
    expect(decodeJwtPayload(afterExit.accessToken as string)?.tenant_id, '已回到原租户').toBe(c.tenant);
    expect(navigatedToLogin(navs), `自动退出不得跳登录页（§3.4 闭环自洽性）；跳转链=${JSON.stringify(navs)}`).toBe(
      false,
    );
    expect(await readEvents(page), '自动退出不得发 jonex:token-expired').not.toContain(EVENTS.tokenExpired);

    // ---------- impersonate_expired 审计 ----------
    // 经平台审计查询接口核验；若接口/包装层级不同，报错信息里带出实际响应体，便于人工判断
    const found = await queryAudit(page, afterExit.accessToken as string);
    expect(
      found.actions.includes(c.auditAction),
      `未在 /api/v1/platform/audit-logs 找到 ${c.auditAction} 审计。\n命中端点=${found.url}\n状态=${found.status}\n响应摘要=${found.summary}`,
    ).toBe(true);

    // ---------- 后续请求的 Bearer 属于原租户 ----------
    const bearerBefore = net.bearer.length;
    await page.reload(); // 触发一次应用启动，产生带 Bearer 的真实业务请求
    await page.waitForLoadState('load');
    await expect
      .poll(() => net.bearer.length, {
        timeout: 20_000,
        message: '退出后应用没有发出任何带 Bearer 的请求 ⇒ 无法验证「后续 Bearer 属原租户」（反空洞失败）',
      })
      .toBeGreaterThan(bearerBefore);

    const after = net.bearer.slice(bearerBefore);
    const decoded = after.map((b) => decodeJwtPayload(b.token)).filter(Boolean) as Record<string, unknown>[];
    expect(decoded.length, '退出后的 Bearer 都应可解码').toBeGreaterThan(0);
    expect(
      decoded.some((p) => p.tenant_id === c.tenant && !p.impersonated),
      `退出后的请求必须带原租户 token；实际=${JSON.stringify(decoded.map((p) => ({ t: p.tenant_id, imp: !!p.impersonated })))}`,
    ).toBe(true);
    expect(
      decoded.some((p) => p.impersonated === true),
      '退出后的请求不得再带模拟态 token',
    ).toBe(false);

    test.info().annotations.push({
      type: 'observed',
      description: `audit=${found.status} / bearer(after exit)=${decoded.length} / navs=${JSON.stringify(navs)}`,
    });
  });
});

async function queryAudit(
  page: import('@playwright/test').Page,
  token: string,
): Promise<{ actions: string[]; url: string; status: number; summary: string }> {
  const headers = { Authorization: `Bearer ${token}` };
  const tries: { url: string; params: Record<string, string | number> }[] = [
    { url: '/api/v1/platform/audit-logs', params: { action: cfg().auditAction, page: 1, page_size: 50 } },
    { url: '/api/v1/platform/audit-logs', params: { page: 1, page_size: 100 } },
  ];
  let last: { actions: string[]; url: string; status: number; summary: string } | null = null;
  for (const t of tries) {
    const res = await page.request.get(t.url, { params: t.params, headers });
    const text = await res.text();
    let actions: string[];
    try {
      actions = collectActions(JSON.parse(text));
    } catch {
      actions = [];
    }
    const query = Object.entries(t.params)
      .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
      .join('&');
    last = {
      actions,
      url: `${t.url}?${query}`,
      status: res.status(),
      summary: text.slice(0, 800),
    };
    if (actions.includes(cfg().auditAction)) return last;
  }
  if (!last) throw new Error('审计查询没有可用的尝试配置');
  return last;
}
