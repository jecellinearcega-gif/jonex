import { colors } from '@jonex/platform-theme/tokens';

/**
 * 「IdP 认证通过、平台侧不放行」的三档原因，与后端
 * `capabilities/platform/auth/auth_service.py` 的 SSO_ERROR_REASON_* 常量一一对应。
 * 后端新增一档而这里没跟上时，页面会回落到通用兜底文案（不会白屏），
 * 但那时应该把新档补进来。
 */
export const SSO_ERROR_REASONS = [
  'sync_required',
  'account_disabled',
  'identity_conflict',
] as const;

export type SsoErrorReason = (typeof SSO_ERROR_REASONS)[number];

/**
 * 图标语义键。真正的 antd 图标组件映射留在页面里 —— 模型层不 import 渲染库，
 * 才能在 node 环境下直接单测（本仓库前端没有 jsdom）。
 */
export type SsoErrorIconKey = 'stop' | 'lock' | 'userDelete';

export interface SsoErrorContent {
  titleKey: string;
  descKey: string;
  reasonKey: string;
  iconKey: SsoErrorIconKey;
  iconColor: string;
  iconBg: string;
}

const CONTENT: Record<SsoErrorReason, SsoErrorContent> = {
  sync_required: {
    titleKey: 'auth.ssoError.titleSyncRequired',
    descKey: 'auth.ssoError.descSyncRequired',
    reasonKey: 'auth.ssoError.reasonSyncRequired',
    iconKey: 'stop',
    iconColor: colors.errorText,
    iconBg: colors.errorBg,
  },
  account_disabled: {
    titleKey: 'auth.ssoError.titleAccountDisabled',
    descKey: 'auth.ssoError.descAccountDisabled',
    reasonKey: 'auth.ssoError.reasonAccountDisabled',
    iconKey: 'lock',
    iconColor: colors.warningText,
    iconBg: colors.warningBg,
  },
  identity_conflict: {
    titleKey: 'auth.ssoError.titleIdentityConflict',
    descKey: 'auth.ssoError.descIdentityConflict',
    reasonKey: 'auth.ssoError.reasonIdentityConflict',
    iconKey: 'userDelete',
    iconColor: colors.errorText,
    iconBg: colors.errorBg,
  },
};

/** 未知 / 缺失 reason 的兜底：直开链接、或后端加了新档而前端尚未发版时都会走到这里。 */
export const SSO_ERROR_FALLBACK: SsoErrorContent = {
  titleKey: 'auth.ssoError.titleFallback',
  descKey: 'auth.ssoError.descFallback',
  reasonKey: 'auth.ssoError.reasonFallback',
  iconKey: 'stop',
  iconColor: colors.errorText,
  iconBg: colors.errorBg,
};

function isSsoErrorReason(value: string | null): value is SsoErrorReason {
  return value !== null && (SSO_ERROR_REASONS as readonly string[]).includes(value);
}

export function resolveSsoErrorContent(reason: string | null): SsoErrorContent {
  return isSsoErrorReason(reason) ? CONTENT[reason] : SSO_ERROR_FALLBACK;
}

/** 「返回登录页」的目标地址；带租户时预填登录表单的租户输入框（登录页支持 `?tenant=`）。 */
export function buildBackToLoginUrl(tenant: string | null): string {
  const trimmed = (tenant ?? '').trim();
  return trimmed ? `/login?tenant=${encodeURIComponent(trimmed)}` : '/login';
}
