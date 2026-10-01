/**
 * S1 模拟态不续期（主流程）—— 最高优先级
 * 契约：docs/auth/tenant-impersonation-refresh-isolation-plan.md §4 S1、§3.2 硬不变量 1
 *
 * Given 管理员已切换到 tenant_jonex_sso
 * When  模拟 access token 临近过期
 * Then  前端不向 /api/v1/auth/refresh 发起任何请求
 * And   localStorage 中的 access token 仍是目标租户的模拟 token
 *
 * ⛔ 判据是「未发出请求」，不是「最终 token 值没变」：缺陷存在时后者可能因时序恰好通过
 *    （refresh 失败 / 尚未回写），而「发过请求」这件事本身就已经是串租户了。
 */
import { expect, test } from '@playwright/test';
import {
  EVENTS,
  cfg,
  decodeJwtPayload,
  expectBannerVisible,
  expectImpersonationActive,
  expectTokenTenant,
  installEventRecorder,
  login,
  navigatedToLogin,
  readEvents,
  readStorage,
  recordNavigations,
  recordNetwork,
  REFRESH_PATH,
  seedStaleExpiresAt,
  sleep,
  switchTenantViaUi,
} from '../lib/harness';

test.describe('S1 模拟态不续期', () => {
  test('切换后前端一次都不发 /api/v1/auth/refresh，access token 仍为模拟 token', async ({ page }) => {
    const c = cfg();

    // ---------- Arrange ----------
    await login(page);
    // 采集器必须在切换**之前**就位：缺陷（原租户 cookie 换回原租户 token）在切换后的
    // reload 上就会触发，晚挂监听会漏掉最关键的窗口。
    const net = recordNetwork(page);
    const navs = recordNavigations(page);
    await installEventRecorder(page, [EVENTS.tokenExpired]);

    const before = await readStorage(page);
    const originalToken = before.accessToken as string;
    await expectTokenTenant(page, c.tenant, false, '登录态');
    expect(originalToken, '登录态必须有 access token').toBeTruthy();

    // 「模拟 token 临近过期」的确定性前置：把 jonex_token_expires_at 种成过去值。
    // 契约 §0 的「加速器」正是这个残留值 —— 若 switchTenant / backupAuthForImpersonation
    // 没有把它清成 null，reload 后 scheduleAutoRefresh 会立刻开火（delay ≤ 0）。
    // 用它代替「等 30 分钟真到期」，缺陷必现且用例时长可控。
    await seedStaleExpiresAt(page);

    // ---------- Act ----------
    await switchTenantViaUi(page, c.targetTenant);
    await expectImpersonationActive(page, c.targetTenant);
    await expectBannerVisible(page);

    const switched = await readStorage(page);
    const impersonationToken = switched.accessToken as string;
    expect(impersonationToken, '切换后的 token 必须是新签发的模拟 token').not.toBe(originalToken);
    const requestsSeenAtSwitch = net.all.length;

    // ---------- 观察窗口 ----------
    await sleep(c.s1ObserveMs);

    // ⭐ 反空洞 ①：采集器在整个窗口内一直是活的（切换后的 reload 会重新发一批启动请求）。
    expect(
      requestsSeenAtSwitch,
      '切换后没有观察到任何请求 ⇒ 采集器失效，下面的「零 refresh」结论无效（不要改断言，先修量具）',
    ).toBeGreaterThan(0);
    // ⭐ 反空洞 ②：登录态确认不是模拟态，且切换真的发生（否则「没发 refresh」会因为压根没切而空洞通过）。
    expect(decodeJwtPayload(originalToken)?.impersonated, '登录态不应带 impersonated 标记').toBeFalsy();

    // ---------- 主断言 ----------
    expect(net.refresh, `模拟态下不得发起任何 ${REFRESH_PATH} 请求（判据是「未发出请求」，不是最终 token 值）`).toEqual(
      [],
    );

    // ---------- ⭐ 正对照：证明「零 refresh」不是因为采集器/判据失灵 ----------
    // 在页面里手工发一次 refresh（走同源、带 cookie，与 callRefresh 的通道一致），
    // 若采集器与 URL 判据有效，计数必须从 0 变成 1。
    await page.evaluate(
      (path) => fetch(path, { method: 'POST', credentials: 'include' }).then((r) => r.status),
      REFRESH_PATH,
    );
    await expect
      .poll(() => net.refresh.length, {
        timeout: 10_000,
        message: `正对照失败：手工发出的 ${REFRESH_PATH} 未被采集到 ⇒ 本用例的「零 refresh」判定无判别力`,
      })
      .toBe(1);

    // ---------- token 断言 ----------
    await expectTokenTenant(page, c.targetTenant, true, '观察窗口结束后');
    const after = await readStorage(page);
    expect(after.accessToken, 'access token 不得被原租户 token 覆盖').toBe(impersonationToken);
    expect(after.backup, '原租户会话备份必须仍然存在（回原租户的唯一凭证）').toBeTruthy();
    expect(decodeJwtPayload(after.backup as string)?.tenant_id, '备份里应是原租户的 token').toBe(c.tenant);

    // ---------- 其他不该发生的事 ----------
    expect(navigatedToLogin(navs), `不得跳登录页；跳转链=${JSON.stringify(navs)}`).toBe(false);
    expect(await readEvents(page), `不得发出 ${EVENTS.tokenExpired}`).not.toContain(EVENTS.tokenExpired);
  });
});
