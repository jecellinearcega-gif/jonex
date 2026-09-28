import { describe, expect, it } from 'vitest';
import { colors } from '@jonex/platform-theme/tokens';
import {
  SSO_ERROR_FALLBACK,
  SSO_ERROR_REASONS,
  buildBackToLoginUrl,
  resolveSsoErrorContent,
} from './ssoErrorModel';

describe('resolveSsoErrorContent', () => {
  it('三档 reason 各映射到自己的文案键与图标', () => {
    // 12 个键（3 档 × title/desc/reason + 兜底 3 个）全部逐字钉住：
    // 只断 titleKey 的话，descKey / reasonKey 拼错（如 descSyncRequird）不会被拦，
    // 上线表现是错误页上直接把生键名渲染给用户看。
    expect(resolveSsoErrorContent('sync_required').titleKey).toBe('auth.ssoError.titleSyncRequired');
    expect(resolveSsoErrorContent('sync_required').descKey).toBe('auth.ssoError.descSyncRequired');
    expect(resolveSsoErrorContent('sync_required').reasonKey).toBe('auth.ssoError.reasonSyncRequired');
    expect(resolveSsoErrorContent('account_disabled').titleKey).toBe('auth.ssoError.titleAccountDisabled');
    expect(resolveSsoErrorContent('account_disabled').descKey).toBe('auth.ssoError.descAccountDisabled');
    expect(resolveSsoErrorContent('account_disabled').reasonKey).toBe('auth.ssoError.reasonAccountDisabled');
    expect(resolveSsoErrorContent('identity_conflict').titleKey).toBe('auth.ssoError.titleIdentityConflict');
    expect(resolveSsoErrorContent('identity_conflict').descKey).toBe('auth.ssoError.descIdentityConflict');
    expect(resolveSsoErrorContent('identity_conflict').reasonKey).toBe('auth.ssoError.reasonIdentityConflict');
    expect(SSO_ERROR_FALLBACK.titleKey).toBe('auth.ssoError.titleFallback');
    expect(SSO_ERROR_FALLBACK.descKey).toBe('auth.ssoError.descFallback');
    expect(SSO_ERROR_FALLBACK.reasonKey).toBe('auth.ssoError.reasonFallback');

    // 图标语义键：三档各不相同；兜底刻意与 sync_required 同款（同属「无访问权限」语义）
    expect([
      resolveSsoErrorContent('sync_required').iconKey,
      resolveSsoErrorContent('account_disabled').iconKey,
      resolveSsoErrorContent('identity_conflict').iconKey,
      SSO_ERROR_FALLBACK.iconKey,
    ]).toEqual(['stop', 'lock', 'userDelete', 'stop']);
  });

  it('账号停用用琥珀色，另外两档用红色', () => {
    expect(resolveSsoErrorContent('account_disabled').iconKey).toBe('lock');
    // 绝对断言（相对断言只能证明「两档相同、另一档不同」——
    // 把 account_disabled 与另两档的配色整体对调，那组断言照样全绿）
    expect(resolveSsoErrorContent('sync_required').iconColor).toBe(colors.errorText);
    expect(resolveSsoErrorContent('identity_conflict').iconColor).toBe(colors.errorText);
    expect(resolveSsoErrorContent('account_disabled').iconColor).toBe(colors.warningText);
    expect(resolveSsoErrorContent('sync_required').iconBg).toBe(colors.errorBg);
    expect(resolveSsoErrorContent('identity_conflict').iconBg).toBe(colors.errorBg);
    expect(resolveSsoErrorContent('account_disabled').iconBg).toBe(colors.warningBg);

    expect(resolveSsoErrorContent('sync_required').iconColor).toBe(
      resolveSsoErrorContent('identity_conflict').iconColor,
    );
    expect(resolveSsoErrorContent('account_disabled').iconColor).not.toBe(
      resolveSsoErrorContent('sync_required').iconColor,
    );
  });

  it('未知 / 缺失 reason 回落到兜底，不抛错', () => {
    expect(resolveSsoErrorContent(null)).toBe(SSO_ERROR_FALLBACK);
    expect(resolveSsoErrorContent('')).toBe(SSO_ERROR_FALLBACK);
    expect(resolveSsoErrorContent('something_new_from_backend')).toBe(SSO_ERROR_FALLBACK);
  });

  it('SSO_ERROR_REASONS 就是后端那三个 reason 字面量', () => {
    // 与后端 capabilities/platform/auth/auth_service.py 的 SSO_ERROR_REASON_* 对齐：
    // 前端多一个自造值 → 该档永远落兜底；少一个 → 真信号被当未知档。
    // 排序后比较，避免「顺序变了」被误报成语义变化。
    expect([...SSO_ERROR_REASONS].sort()).toEqual([
      'account_disabled',
      'identity_conflict',
      'sync_required',
    ]);
  });

  it('三档都不复用兜底的键（防止误配成同一个文案）', () => {
    const keys = ['sync_required', 'account_disabled', 'identity_conflict'].map(
      (r) => resolveSsoErrorContent(r).titleKey,
    );
    expect(new Set(keys).size).toBe(3);
    keys.forEach((k) => expect(k).not.toBe(SSO_ERROR_FALLBACK.titleKey));
  });
});

describe('buildBackToLoginUrl', () => {
  it('带租户时预填登录表单', () => {
    expect(buildBackToLoginUrl('tenant_jonex_demo')).toBe(
      '/login?tenant=tenant_jonex_demo',
    );
  });

  it('租户为特殊字符时做 URL 编码', () => {
    expect(buildBackToLoginUrl('a b&c')).toBe('/login?tenant=a%20b%26c');
  });

  it('无租户 / 空串 / 纯空白时不带参数', () => {
    expect(buildBackToLoginUrl(null)).toBe('/login');
    expect(buildBackToLoginUrl('')).toBe('/login');
    expect(buildBackToLoginUrl('   ')).toBe('/login');
  });
});
