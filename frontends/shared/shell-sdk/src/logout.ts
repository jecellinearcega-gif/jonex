/**
 * 登出时的服务端会话吊销。
 *
 * 只清前端 storage 是「假登出」：refresh token 由后端以 httpOnly cookie 下发，JS 读不到
 * 也删不掉，服务端会话同样仍然有效 —— 登出后访问根路径会被 /auth/refresh 凭旧 cookie
 * 静默恢复登录态。所以登出必须先让后端吊销会话并下发 delete_cookie。
 */

/** 后端登出端点（经 Gateway 透传到 platform 的 /auth/logout）。 */
const LOGOUT_ENDPOINT = '/api/v1/auth/logout';

/** 服务端吊销的超时上限：后端不可达时不能把用户卡在登出操作上。 */
const REVOKE_TIMEOUT_MS = 3000;

/**
 * 请求后端吊销会话并清除 httpOnly refresh cookie。
 *
 * 同源请求默认携带 cookie，无需额外配置 credentials。失败（网络异常 / 超时 / 非 2xx /
 * JSON 解析失败）一律静默返回 null：本地登出必须继续，否则用户会卡在无法退出的已登录态。
 *
 * 返回值是响应体 `data.sso_logout_url`：非 null 表示本次是 Keycloak SSO 会话，必须由
 * **浏览器顶层导航**打开该地址才能结束 Keycloak 侧会话（fetch 拿到 302 只会自己跟随，
 * Keycloak 的 Set-Cookie 到不了浏览器）。null 时按本地登出处理即可。
 */
export async function revokeServerSession(timeoutMs: number = REVOKE_TIMEOUT_MS): Promise<string | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(LOGOUT_ENDPOINT, {
      method: 'POST',
      credentials: 'same-origin',
      signal: controller.signal,
    });
    if (!resp.ok) return null;
    const body = await resp.json();
    const url = body?.data?.sso_logout_url;
    return typeof url === 'string' && url ? url : null;
  } catch {
    return null; // 静默：本地登出继续
  } finally {
    clearTimeout(timer);
  }
}
