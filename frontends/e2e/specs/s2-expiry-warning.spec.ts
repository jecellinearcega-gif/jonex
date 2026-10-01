/**
 * S2 到期前 60 秒提示一次
 * 契约：docs/auth/tenant-impersonation-refresh-isolation-plan.md §4 S2、§3.4（exp-60s 那一行）
 *
 * Given 管理员已切换到 tenant_jonex_sso
 * When  距模拟 token 过期还有 60 秒
 * Then  弹出一次提醒「模拟会话即将结束」
 * And   顶部横幅切换为常驻倒计时
 * And   该 Toast 在会话剩余期间不再重复出现
 *
 * 【前置】后端 IMPERSONATE_EXPIRE_MINUTES 必须 ≥ 2（本用例默认按 2 分钟跑，用
 * E2E_IMPERSONATE_EXPIRE_MINUTES 对齐）。设为 1 分钟时「60 秒前」与「切换完成」几乎同时，
 * 观测不到「提示前」的基线，用例会显式 FAIL 而不是给出假结论。
 *
 * 【量具】antd v6 下「某提示没出现」类断言可能恒真 ⇒
 *   ① 先跑检测器自检（注入一个 .ant-message-notice，证明检测器测得到「出现」）；
 *   ② 倒计时用「先断言不存在、后断言存在」的成对断言 —— 后一次出现即是前一次缺席的正对照。
 */
import { expect, test } from '@playwright/test';
import {
  cfg,
  decodeJwtPayload,
  assertToastDetectorHealthy,
  expectImpersonationActive,
  installToastRecorder,
  login,
  matchingToasts,
  readStorage,
  readToasts,
  recordNavigations,
  recordNetwork,
  remainingSeconds,
  sleep,
  switchTenantViaUi,
  TESTID,
  MISSING_TESTID_HINT,
  expectTokenTenant,
} from '../lib/harness';

test.describe('S2 到期前 60 秒提示一次', () => {
  test('Toast 恰好一次 + 横幅切常驻倒计时 + 剩余期间不重复', async ({ page }) => {
    const c = cfg();
    test.setTimeout(300_000);

    await login(page);
    await installToastRecorder(page);
    recordNetwork(page); // S1 的契约：模拟态全程不得发 refresh —— 顺带在同一跑里守住
    recordNavigations(page);
    await switchTenantViaUi(page, c.targetTenant);
    await expectImpersonationActive(page, c.targetTenant);

    // token 的 exp 是契约 §3.4 的基准点
    const token = (await readStorage(page)).accessToken as string;
    const payload = decodeJwtPayload(token)!;
    const remaining = remainingSeconds(payload);
    const expected = c.impersonateExpireMinutes * 60;
    expect(
      remaining,
      `模拟 token 剩余 ${remaining}s，期望约 ${expected}s ⇒ E2E_IMPERSONATE_EXPIRE_MINUTES 与后端 IMPERSONATE_EXPIRE_MINUTES 不一致`,
    ).toBeLessThanOrEqual(expected + 10);
    expect(remaining, 'S2 需要观察「提示前」的基线，模拟 token 有效期必须 ≥ 2 分钟').toBeGreaterThan(100);

    // ---- 基线（距到期 > 60s）：还没有倒计时、还没有提示 --------------------
    await expectTokenTenant(page, c.targetTenant, true, '基线');
    await expect(page.locator(`[data-testid="${TESTID.countdown}"]`), MISSING_TESTID_HINT).toHaveCount(0);
    await assertToastDetectorHealthy(page);
    expect(matchingToasts(await readToasts(page)), '距到期 > 60s 时不应出现到期提示').toEqual([]);

    // ---- 走到 exp-52s（跨过 exp-60s 的阈值） ----------------------------
    await sleep((remaining - 52) * 1000);

    // 倒计时出现 —— 同时充当上面「基线时不存在」的正对照
    const countdown = page.locator(`[data-testid="${TESTID.countdown}"]`);
    if ((await countdown.count()) === 0) throw new Error(MISSING_TESTID_HINT);
    await expect(countdown.first(), 'exp-60s 后横幅应切换为常驻倒计时').toBeVisible({ timeout: 25_000 });

    // Toast 恰好一次
    await expect
      .poll(async () => matchingToasts(await readToasts(page)).length, {
        timeout: 25_000,
        message: 'exp-60s 时应弹出一次「模拟会话即将结束」提示（文案匹配用 E2E_TOAST_TEXT_PATTERN）',
      })
      .toBe(1);

    // ---- 走到 exp-6s：剩余期间不得重复 ----------------------------------
    await sleep(46_000);
    const toasts = matchingToasts(await readToasts(page));
    expect(
      toasts.map((t) => t.text),
      '同在会话剩余期间内，该 Toast 不得重复出现（按出现次数计，不是按文案去重）',
    ).toHaveLength(1);
    await expect(countdown.first(), '尚未到期时倒计时横幅仍在').toBeVisible();

    // 记录观察结果，便于排查（不算断言）
    test.info().annotations.push({
      type: 'observed',
      description: `toast=${JSON.stringify(toasts)} / 倒计时可见 / exp 剩余约 6s`,
    });
  });
});
