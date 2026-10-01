import { defineConfig } from '@playwright/test';

/**
 * 模拟租户会话续期隔离 —— 验收 E2E 配置。
 *
 * ⛔ 本配置**不启动任何服务**：需要完整栈（Gateway + Sidecar + platform + 前端）已由用户拉起，
 *    见 README.md「前置条件」。baseURL 用 E2E_BASE_URL 指过去即可。
 * ⛔ 串行（workers=1）：本套用例会改 localStorage / 依赖精确的 60 秒时点，并行会互相干扰。
 */
export default defineConfig({
  testDir: './specs',
  timeout: 300_000, // S2/S3/S7 要真的等到 exp（IMPERSONATE_EXPIRE_MINUTES=1~2 分钟）+ 收尾
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0, // 验收用例不接受重试掩盖时序问题；红灯要人看
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:5173',
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
});
