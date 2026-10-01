/**
 * 模拟租户会话（impersonation）的判据与到期时间。
 *
 * 判据的**唯一权威来源是 access token 自己的 payload**（`impersonated === true`，
 * 由后端 `create_impersonation_token()` 写入）。
 *
 * ⛔ 不要用缓存的 `jonex_user.impersonated` 替代：用户缓存可能滞后（切租户过程中
 * `jonex_user` 早已是目标租户、而 Authorization 头还是原租户 token —— 这正是
 * 「界面显示 A 租户、请求实为 B 租户」这个跨租户缺陷难以察觉的原因之一）。
 * 缓存可以骗人，被真正发出去的 token 不会。
 *
 * JWT payload 为 base64url（无 padding），解码手法与 shell 的
 * `isAccessTokenExpired()` 保持一致。
 */
import { readAccessToken } from './authStorage';

interface AccessTokenPayload {
  exp?: unknown;
  impersonated?: unknown;
}

/** 解码 access token 的 payload；任何一步失败都返回 null（绝不抛异常） */
function decodeAccessTokenPayload(): AccessTokenPayload | null {
  const token = readAccessToken();
  if (!token) return null;
  try {
    const part = token.split('.')[1];
    if (!part) return null;
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/');
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');
    const payload = JSON.parse(atob(padded)) as unknown;
    if (payload === null || typeof payload !== 'object') return null;
    return payload as AccessTokenPayload;
  } catch {
    return null;
  }
}

/**
 * 当前是否处于模拟租户会话。
 *
 * 只解 access token payload 的 `impersonated === true`（严格全等：字符串 `"true"`
 * 不算）。解析失败 / 无 token 一律返回 false —— **判不准就不按模拟态处理**，
 * 非模拟态路径（本地密码登录、SSO）的行为因此一个字节都不变。
 */
export function isImpersonating(): boolean {
  return decodeAccessTokenPayload()?.impersonated === true;
}

/** 当前 access token 的绝对到期时间戳（ms）；解不出 `exp` 时返回 null */
export function readAccessTokenExpiresAtMs(): number | null {
  const exp = decodeAccessTokenPayload()?.exp;
  return typeof exp === 'number' && Number.isFinite(exp) ? exp * 1000 : null;
}
