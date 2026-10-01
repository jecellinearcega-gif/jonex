/**
 * S7 退出模拟后原会话可正常续期（边界）
 * 契约：docs/auth/tenant-impersonation-refresh-isolation-plan.md §4 S7、§3.4 闭环自洽性、§3.5
 *
 * Given 管理员已通过自动到期退出模拟
 * When  原租户 access token 过期并触发续期
 * Then  响应状态码为 200
 * And   用户停留在原租户，不被跳转到登录页
 *
 * 【怎么触发续期】自动退出后 `expires_at` 为 null（§3.5），定时器不排 —— 于是把
 * jonex_token_expires_at 种成过去值再 reload，让应用自己的 scheduleAutoRefresh 立刻走一次
 * `callRefresh()`（与登录态用的是同一条通道：POST /api/v1/auth/refresh + httpOnly cookie）。
 * 这样验的是**应用真实发出的那次续期**，不是我们手工造的请求。
 *
 * 【本用例同时是「标记 cookie 已清」的证据】若 /auth/impersonate/end 没清掉 jonex_impersonating，
 * 这次续期会被后端按契约 §2.2 拒成 403（而不是 200）。
 *
 * 【前置】后端 IMPERSONATE_EXPIRE_MINUTES=1（用 E2E_IMPERSONATE_EXPIRE_MINUTES=1 对齐）。
 */
import { expect, test } from '@playwright/test';
import {
  cfg,
  expectImpersonationActive,
  expectTokenTenant,
  installEventRecorder,
  login,
  navigatedToLogin,
  readStorage,
  recordNavigations,
  recordNetwork,
  recordRefreshResponses,
  remainingSeconds,
  seedStaleExpiresAt,
  sleep,
  switchTenantViaUi,
  waitForAutoExit,
  decodeJwtPayload,
  EVENTS,
} from '../lib/harness';

test.describe('S7 退出模拟后原会话可正常续期', () => {
  test('自动退出后原租户续期返回 200，且不被跳登录页', async ({ page }) => {
    const c = cfg();
    test.setTimeout(300_000);

    await login(page);
    const net = recordNetwork(page);
    const refreshes = recordRefreshResponses(page);
    const navs = recordNavigations(page);
    await installEventRecorder(page, [EVENTS.tokenExpired]);

    await switchTenantViaUi(page, c.targetTenant);
    await expectImpersonationActive(page, c.targetTenant);

    const payload = decodeJwtPayload((await readStorage(page)).accessToken as string)!;
    const remaining = remainingSeconds(payload);
    expect(remaining, 'token 已过期 ⇒ 无法观测「到期退出」，本用例结论无效').toBeGreaterThan(15);
    await sleep((remaining + 12) * 1000);

    // ---------- Arrange：确认已自动退出模拟 ----------
    await waitForAutoExit(page);
    await expectTokenTenant(page, c.tenant, false, '自动退出后');
    const afterExit = await readStorage(page);
    const tokenAfterExit = afterExit.accessToken as string;

    // ---------- Act：让原租户会话自己发起一次续期 ----------
    const refreshCountBefore = net.refresh.length;
    await seedStaleExpiresAt(page);
    await page.reload();
    await page.waitForLoadState('load');

    await expect
      .poll(() => net.refresh.length, {
        timeout: 30_000,
        message:
          '退出模拟后应用没有发起任何 /api/v1/auth/refresh ⇒ 无法验证续期（反空洞失败：可能是没回到原租户态、或 callRefresh 仍被当成模拟态短路）',
      })
      .toBeGreaterThan(refreshCountBefore);

    // ---------- 主断言 ----------
    const mine = refreshes.filter((r) => r.url.includes('/api/v1/auth/refresh'));
    expect(mine.length, '应捕获到本次续期的响应').toBeGreaterThan(0);
    const last = mine[mine.length - 1];
    expect(
      last.status,
      `退出模拟后的续期必须是 200（403 = 标记 cookie 没清干净 / 3026；401 = 原租户会话已失效）；实际=${last.status} body=${last.body.slice(0, 300)}`,
    ).toBe(200);
    expect(last.body, '续期响应体应包含新的 access_token').toContain('access_token');

    expect(navigatedToLogin(navs), `续期成功后不得跳登录页；跳转链=${JSON.stringify(navs)}`).toBe(false);

    await expect
      .poll(
        async () => {
          const s = await readStorage(page);
          return s.accessToken !== tokenAfterExit;
        },
        { timeout: 20_000, message: '续期成功后应用应写入新的 access token' },
      )
      .toBe(true);

    const final = await readStorage(page);
    const finalPayload = decodeJwtPayload(final.accessToken as string)!;
    expect(finalPayload.tenant_id, '续期后仍应是原租户').toBe(c.tenant);
    expect(!!finalPayload.impersonated, '续期后不得带 impersonated 标记').toBe(false);

    test.info().annotations.push({
      type: 'observed',
      description: `refresh status=${last.status} / navs=${JSON.stringify(navs)} / exp=${String(finalPayload.exp)}`,
    });
  });
});
