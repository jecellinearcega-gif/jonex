/**
 * 前端自动刷新 token（滑动续期）：
 *
 * - 登录后记录 access token 的绝对过期时间戳（JONEX_TOKEN_EXPIRES_AT_KEY）；
 * - scheduleAutoRefresh() 在过期前定时调用 /auth/refresh 刷新凭证，成功后重排下一轮；
 * - refreshTokens() 单飞刷新：并发请求（多个 401 / 定时器）共享同一个 in-flight 请求，
 *   只发一次 /auth/refresh，刷新结果写入 localStorage 供所有调用方读取。
 *
 * 刷新走原生 fetch，不经过 axios 拦截器，避免 401 处理器递归。
 */
import {
  writeAccessToken,
  writeTokenExpiresAt,
  readTokenExpiresAt,
} from './authStorage';
import { isImpersonating } from './impersonation';

/** 刷新端点：与生产 Nginx / 本地 dev-gateway 的 /api/v1 前缀一致 */
const REFRESH_URL = '/api/v1/auth/refresh';
/** 提前多少毫秒刷新（过期前 60 秒） */
const REFRESH_AHEAD_MS = 60 * 1000;

/** 与后端 success_response 信封对齐 */
interface RefreshResponseData {
  access_token?: string;
  expires_in?: number;
}
interface RefreshResponseEnvelope {
  success: boolean;
  code?: number;
  data?: RefreshResponseData;
}

let refreshInFlight: Promise<boolean> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

async function callRefresh(): Promise<boolean> {
  // ⛔ 模拟态硬不变量：**永不发起 /auth/refresh**。
  // 模拟会话没有任何可续期的 refresh 凭证（create_impersonation_token() 从不签发
  // refresh），浏览器却会自动带上登录留下的**原租户** jonex_refresh cookie —— 一旦发出，
  // 换回的就是原租户的 access token 并覆盖模拟 token（跨租户静默写入）。
  // 短路必须发生在**发请求之前**：这不是优化，是正确性要求（不依赖后端版本）。
  if (isImpersonating()) return false;

  // refresh token 已迁移到 httpOnly cookie（SSO PRD §5.4/BC4），JS 不可读，
  // 不再读 localStorage、也不手动带 Authorization 头；靠 credentials: include
  // 让浏览器自动携带同源 httpOnly cookie（jonex_refresh）。后端 /auth/refresh
  // 只认 cookie，无 cookie 时快速 401。
  try {
    const resp = await fetch(REFRESH_URL, {
      method: 'POST',
      credentials: 'include',
    });
    if (!resp.ok) return false;
    const body = (await resp.json()) as RefreshResponseEnvelope;
    const data = body?.data;
    if (!data?.access_token) return false;
    writeAccessToken(data.access_token);
    if (data.expires_in) writeTokenExpiresAt(Date.now() + data.expires_in * 1000);
    return true;
  } catch {
    return false;
  }
}

/** 单飞刷新：并发调用共享同一个 in-flight 请求。成功返回 true 并已写入新 token。 */
export function refreshTokens(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = callRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

/** 启动定时自动刷新：在 access token 过期前自动刷新并续期（幂等，先清旧定时器） */
export function scheduleAutoRefresh(): void {
  stopAutoRefresh();
  const expiresAt = readTokenExpiresAt();
  if (!expiresAt) return;
  const delay = Math.max(0, expiresAt - Date.now() - REFRESH_AHEAD_MS);
  refreshTimer = setTimeout(() => {
    void refreshTokens().then((ok) => {
      if (ok) scheduleAutoRefresh(); // 成功后重排下一轮
    });
  }, delay);
}

/** 停止定时自动刷新（登出 / 会话失效时调用） */
export function stopAutoRefresh(): void {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}
