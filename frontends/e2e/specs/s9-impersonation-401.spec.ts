/**
 * S9 模拟态 401 不触发登出（异常流程 — 会话）—— 最高优先级
 * 契约：docs/auth/tenant-impersonation-refresh-isolation-plan.md §4 S9、§3.2 硬不变量 2/3、§2.3
 *
 * Given 管理员已切换到 tenant_jonex_sso
 * When  业务请求返回 401
 * Then  不发出 jonex:token-expired 事件
 * And   localStorage 中的 jonex_impersonation_backup 仍存在
 * And   不跳转到登录页
 *
 * 【本用例的 401 是怎么造出来的】—— 用真实路径，不用假造噪声
 * `switchTenant()` 内部就是 `impersonate() → 备份 → 写模拟 token → await fetchCurrentUser() → reload`，
 * 而 `fetchCurrentUser()` 打的是 axios 客户端上的 `/auth/me`。把 `/auth/me` 拦成 401，
 * 就等于「模拟态下的一次业务请求 401」，且必然发生（反空洞断言会证明它真的发生了）。
 * 因为 `fetchCurrentUser()` 抛错，末尾的 reload 不会执行 —— 这正是我们要的稳定观测态。
 *
 * 【为什么 401 必须走不到登出】
 * 401 分支里 `emitSessionExpired() → clearAuthStorage()` 会删掉 JONEX_IMPERSONATION_BACKUP_KEY，
 * 那是回原租户的唯一副本；后端因此**绝不能**用 401 拒绝模拟态续期（§2.3，必须是 403 + 3026）。
 */
import { expect, test } from '@playwright/test';
import {
  EVENTS,
  cfg,
  decodeJwtPayload,
  expectTokenTenant,
  installEventRecorder,
  login,
  navigatedToLogin,
  readEvents,
  readStorage,
  recordNavigations,
  recordNetwork,
  selectTenantOption,
  sleep,
} from '../lib/harness';

test.describe('S9 模拟态 401 不触发登出', () => {
  test('模拟态业务 401：不发 token-expired、备份仍在、不跳登录页', async ({ page }) => {
    const c = cfg();

    // ---------- Arrange ----------
    await login(page);
    const net = recordNetwork(page);
    const navs = recordNavigations(page);
    await installEventRecorder(page, [EVENTS.tokenExpired, EVENTS.impersonationExpired]);
    await expectTokenTenant(page, c.tenant, false, '登录态');

    // 把业务接口拦成 401（模拟态下的一次业务请求失败）
    await page.route(c.probe401Pattern, (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          success: false,
          code: 1002,
          message: 'E2E forced 401 (probe)',
        }),
      }),
    );

    // ---------- Act ----------
    // 刻意不等 reload：switchTenant 里 fetchCurrentUser() 会 401 抛错，reload 不会发生
    await selectTenantOption(page, c.targetTenant);

    // ⭐ 反空洞：401 真的发生了。没发生就说明探针失效，下面的「没登出」毫无意义。
    await expect
      .poll(() => net.probe.length, {
        timeout: 20000,
        message: `401 探针（${c.probe401Pattern}）没有被触发 ⇒ 本用例没有制造出 401，结论无效`,
      })
      .toBeGreaterThan(0);
    // 反空洞：确认此时确实是模拟态（模拟 token 已写入、备份已在）—— 缺陷路径只在这个前提下成立
    await expectTokenTenant(page, c.targetTenant, true, '401 发生时');
    await sleep(3000); // 给 401 分支的异步收口留时间

    // ---------- 主断言 ----------
    const events = await readEvents(page);
    expect(
      events,
      `模拟态 401 不得发出 ${EVENTS.tokenExpired}（那会走 clearAuthStorage → 删掉回原租户的唯一备份）`,
    ).not.toContain(EVENTS.tokenExpired);

    const s = await readStorage(page);
    expect(s.backup, 'jonex_impersonation_backup 必须仍然存在').toBeTruthy();
    expect(decodeJwtPayload(s.backup as string)?.tenant_id, '备份里应是原租户的 token').toBe(c.tenant);
    expect(s.accessToken, '模拟 token 不得被清掉').toBeTruthy();

    expect(navigatedToLogin(navs), `模拟态 401 不得跳登录页；跳转链=${JSON.stringify(navs)}`).toBe(false);

    // ---------- ⭐ 正对照（判「未出现」必须配） ----------
    // 先证明「事件名 + 采集器」有效：手工派发同名事件，采集器必须听得到。
    await page.evaluate((ev) => {
      window.dispatchEvent(new CustomEvent(ev));
    }, EVENTS.tokenExpired);
    await expect
      .poll(async () => (await readEvents(page)).includes(EVENTS.tokenExpired), {
        timeout: 10_000,
        message: `正对照失败：手工派发的 ${EVENTS.tokenExpired} 没被采集到 ⇒ 「没发事件」的判定无判别力`,
      })
      .toBe(true);

    // 再证明「事件 → 跳登录页」这条链本身是通的：若应用真的发过事件，我们必然会看到跳转。
    await expect
      .poll(() => navigatedToLogin(navs), {
        timeout: 20_000,
        message: `正对照失败：派发 ${EVENTS.tokenExpired} 后没有跳登录页 ⇒ 跳转链不成立，上面「不跳登录页」的断言无判别力；跳转链=${JSON.stringify(
          navs,
        )}`,
      })
      .toBe(true);
  });
});
