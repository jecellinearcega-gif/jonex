import { createRequest } from '@jonex/shared-lib';
import type { ApiClient } from '@jonex/shared-lib';
import type { ShellUser } from '@jonex/shell-sdk';
import {
  JONEX_REFRESH_TOKEN_KEY,
  readAccessToken,
  writeAccessToken,
  writeTokenExpiresAt,
  readCachedUser,
  writeCachedUser,
  clearAuthStorage,
  backupAuthForImpersonation,
  restoreAuthFromImpersonation,
  finishLogout,
  revokeServerSession,
  scheduleAutoRefresh,
  stopAutoRefresh,
  isImpersonating,
} from '@jonex/shell-sdk';
import { clearSsoNavigationInFlight } from '../utils/ssoInflight';
import {
  startImpersonationExpiryWatch,
  stopImpersonationExpiryWatch,
} from '../utils/impersonationSession';

const LOCALE_KEY = 'locale';

export const getAccessToken = readAccessToken;
export const getUser = readCachedUser<ShellUser>;

/** 本地解析 access token 的 exp 判断是否已过期（JWT payload 为 base64url）。
 * 仅用于刷新/启动时的零延迟预判；解析失败或无 exp 时返回 false，交由 `/auth/me` 权威校验。 */
export function isAccessTokenExpired(): boolean {
  const token = getAccessToken();
  if (!token) return false;
  try {
    const part = token.split('.')[1];
    if (!part) return false;
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/');
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');
    const payload = JSON.parse(atob(padded)) as { exp?: unknown };
    return typeof payload.exp === 'number' && payload.exp * 1000 <= Date.now();
  } catch {
    return false;
  }
}

function normalizeUser(raw: Record<string, unknown>): ShellUser {
  const id = String(raw.user_id || raw.id || '');
  const username = String(raw.username || '');
  const displayName = String(raw.display_name || raw.displayName || username);
  const tenantId = raw.tenant_id || raw.tenantId;
  const tenantName = raw.tenant_name || raw.tenantName;
  const roles: string[] = Array.isArray(raw.roles) ? (raw.roles as string[]) : raw.role ? [raw.role as string] : [];
  // 幂等：同时兜底 snake_case（后端原始）与 camelCase（已归一化对象二次进入）
  const isPlatformAdmin =
    raw.is_platform_admin === true || (raw as Record<string, unknown>).isPlatformAdmin === true;
  const isTenantAdmin =
    raw.is_tenant_admin === true || (raw as Record<string, unknown>).isTenantAdmin === true;
  const permissions: string[] = Array.isArray(raw.permissions) ? (raw.permissions as string[]) : [];
  const impersonated = raw.impersonated === true;
  const originalTenantId = raw.original_tenant_id || raw.originalTenantId;
  return {
    id,
    username,
    displayName,
    tenantId: tenantId ? String(tenantId) : undefined,
    tenantName: tenantName ? String(tenantName) : undefined,
    roles,
    isPlatformAdmin,
    isTenantAdmin,
    permissions,
    impersonated: impersonated || undefined,
    originalTenantId: originalTenantId ? String(originalTenantId) : undefined,
  };
}

export function setTokens(access: string, refresh: string, expiresIn?: number): void {
  writeAccessToken(access);
  if (refresh) localStorage.setItem(JONEX_REFRESH_TOKEN_KEY, refresh);
  if (expiresIn && expiresIn > 0) {
    writeTokenExpiresAt(Date.now() + expiresIn * 1000);
  }
  // 登录成功后启动定时自动刷新（滑动续期）；幂等，内部会先清旧定时器。
  scheduleAutoRefresh();
}

export function setUser(user: Record<string, unknown>): void {
  const normalized = normalizeUser(user);
  writeCachedUser(normalized);
}

export function setLocale(locale: string): void {
  localStorage.setItem(LOCALE_KEY, locale);
}

export function getLocale(): string {
  return localStorage.getItem(LOCALE_KEY) || 'zh';
}

export function clearTokens(): void {
  stopAutoRefresh();
  // 模拟态到期看护同样要停：它持有一个 1 秒间隔的定时器与横幅订阅。
  // 登出走这里（logout → clearTokens），不停的话定时器会活到导航完成之后，
  // 继续朝一个正在被拆掉的会话 tick。
  stopImpersonationExpiryWatch();
  clearAuthStorage();
}

/**
 * Clean up forbidden URL token params (jonex_token, jonex_user, jonex_refresh_token).
 * Only handles ticket/code/state for future ticket-exchange flow.
 */
export function initCrossOriginAuth(): void {
  const params = new URLSearchParams(window.location.search);
  const blockedKeys = ['jonex_token', 'jonex_user', 'jonex_refresh_token'];
  let changed = false;
  blockedKeys.forEach((key) => {
    if (params.has(key)) {
      params.delete(key);
      changed = true;
    }
  });
  if (changed) {
    const url = new URL(window.location.href);
    url.search = params.toString();
    window.history.replaceState({}, '', url.toString());
  }
}

export function isAuthenticated(): boolean {
  return !!getAccessToken();
}

// 拦截器不提示错误，错误提示由页面 catch 统一处理（message.error(err?.message || fallback)）；
// 401 过期由 RequireAuth 的 jonex:token-expired 监听与轮询兜底统一跳转。
export const apiClient: ApiClient = createRequest({ baseURL: '/api/v1' });

export interface AuthenticatedLoginResult {
  status: 'authenticated';
  access_token: string;
  refresh_token: string;
  token_type?: string;
  expires_in?: number;
  user: ShellUser;
}

export interface LoginTicketResult {
  ticket: string;
  expires_in?: number;
}

export async function login(username: string, password: string, tenantId: string): Promise<AuthenticatedLoginResult> {
  const result = await apiClient.post<AuthenticatedLoginResult>(
    '/auth/login',
    { username, password },
    { headers: { 'X-Tenant-ID': tenantId } },
  );
  return { ...result, user: normalizeUser(result.user as unknown as Record<string, unknown>) };
}

export async function fetchCurrentUser(): Promise<ShellUser> {
  const raw = await apiClient.get<Record<string, unknown>>('/auth/me');
  return normalizeUser(raw);
}

/** 登录页启动配置（GET /auth/login-config，免鉴权） */
export interface LoginPageConfig {
  /** 默认租户 ID：登录页租户框预填值；空 = 不预填，由用户手输 */
  defaultTenantId: string;
  /**
   * 是否隐藏租户 ID 输入框。
   *
   * 服务端**已解析**过：配了隐藏却没有默认租户时恒为 false（否则页面无法登录），
   * 所以前端照值渲染即可，不要再自己判断。
   */
  hideTenantInput: boolean;
  /**
   * 企业 SSO 入口**可见性**（后端 `sso_entry_visible` = 部署级 SSO 可用 **且**
   * 未被 HIDE_SSO_LOGIN 隐藏）。
   *
   * 注意语义：这是「入口要不要渲染」，**不是**「SSO 是否可用」——
   * 部署级 SSO 可用但被 HIDE_SSO_LOGIN 显式隐藏时同样是 false。
   */
  enabled: boolean;
  /** 企业 SSO 供应方标识；未配置或后端未下发时缺省 */
  provider?: string;
}

/** 配置接口不可用时的保守兜底：显示租户框、不预填、**隐藏 SSO 入口**（保证登录页始终可用）。 */
const LOGIN_CONFIG_FALLBACK: LoginPageConfig = {
  defaultTenantId: '',
  hideTenantInput: false,
  enabled: false,
};

/**
 * 拉取登录页启动配置（默认租户 / 是否隐藏租户框 / SSO 入口可见性与供应方）。
 *
 * 登录前调用，故走免鉴权路径。取值来自服务端环境变量
 * （deploy/.env 第 7 节的 DEFAULT_TENANT_ID / HIDE_TENANT_INPUT / HIDE_SSO_LOGIN
 * 及部署级 SSO 配置）。SSO 入口显隐与供应方两字段已并入本接口一并下发，
 * 登录页只需一次请求。
 *
 * 失败时静默回落——配置接口不该让登录页打不开（登录是最后退路）。
 * 兜底值为**隐藏 SSO 入口**（与 HIDE_SSO_LOGIN 默认隐藏的语义一致）：
 * 拿不到配置时不冒出一个可能必然报错的按钮。
 */
export async function fetchLoginConfig(): Promise<LoginPageConfig> {
  try {
    const raw = await apiClient.get<{
      default_tenant_id?: string;
      hide_tenant_input?: boolean;
      enabled?: boolean;
      provider?: string;
    }>('/auth/login-config');
    return {
      defaultTenantId: String(raw?.default_tenant_id ?? '').trim(),
      hideTenantInput: Boolean(raw?.hide_tenant_input),
      // 显式判字段存在，不照抄 Boolean(...) 惯用法：这样「HTTP 200 但 data 里没有
      // enabled 字段」（新前端 + 旧后端的滚动发布组合）落回 false 是**写在代码里**的意图。
      // 兜底值若改为 true，Boolean(undefined) 会静默给出 false 之外的结果。
      enabled: typeof raw?.enabled === 'boolean' ? raw.enabled : false,
      provider: typeof raw?.provider === 'string' ? raw.provider : undefined,
    };
  } catch {
    return LOGIN_CONFIG_FALLBACK;
  }
}

export interface TenantListItem {
  id: string;
  name: string;
}

export interface ImpersonateResult {
  token: string;
  target_tenant_id: string;
  target_tenant_name?: string;
}

export async function listTenants(): Promise<{ items: TenantListItem[]; total: number }> {
  return apiClient.get<{ items: TenantListItem[]; total: number }>('/platform/tenants', {
    params: { page: 1, page_size: 100 },
  });
}

export async function impersonate(targetTenantId: string): Promise<ImpersonateResult> {
  return apiClient.post<ImpersonateResult>('/auth/impersonate', {
    target_tenant_id: targetTenantId,
  });
}

export async function endImpersonation(): Promise<void> {
  await apiClient.post<null>('/auth/impersonate/end');
}

/** 切换到目标租户：备份原 token → 写模拟 token → 刷新用户缓存 → 整页重载 */
export async function switchTenant(targetTenantId: string): Promise<void> {
  const result = await impersonate(targetTenantId);
  backupAuthForImpersonation();
  writeAccessToken(result.token);
  // 拿掉登录态留下的「续期加速器」：默认续期定时器 + 残留的原租户过期时间戳。
  // 后者不清，reload 后 scheduleAutoRefresh() 会判定即将过期、立刻发起一次
  // /auth/refresh，用原租户 cookie 换回原租户 token 覆盖模拟 token（跨租户缺陷的触发点）。
  // （writeTokenExpiresAt(null) 在 backupAuthForImpersonation() 里已有一道，这里是第二道。）
  stopAutoRefresh();
  writeTokenExpiresAt(null);
  // 启动模拟态专用看护（到期前 60 秒提示 + 到期自动退出）。
  // 注意它活不过下面的整页重载，重载后由 AppShellLayout 挂载时重启 —— 那才是真正的宿主，
  // 这里调用只是让「重载被阻断」的极端情形下也不至于完全没有看护。
  startImpersonationExpiryWatch({ onExpired: autoExitImpersonation });
  const freshUser = await fetchCurrentUser();
  writeCachedUser(freshUser);
  window.location.reload();
}

/** 退出模拟：记审计 → 还原原 token → 刷新用户缓存 → 整页重载 */
export async function exitImpersonation(): Promise<void> {
  await endImpersonation();
  restoreAuthFromImpersonation();
  const freshUser = await fetchCurrentUser();
  writeCachedUser(freshUser);
  window.location.reload();
}

/**
 * 模拟会话**到期（或失效）**的自动收尾：审计 → 本地还原 → 刷新用户缓存 → 整页重载。
 *
 * 与手动退出 `exitImpersonation()` 的唯一区别是**降级顺序**，而且是硬约束：
 * **本地还原优先**。`endImpersonation()` 只承担审计与清标记 cookie，它失败（网络抖动、
 * 后端尚未升级）绝不能阻塞本地还原 —— 卡在「界面目标租户、请求原租户」的错态里，比少一条
 * 审计严重得多，审计可以后补。手动退出保留原语义（失败直接抛给页面提示用户）。
 *
 * 本地还原也失败（备份缺失或损坏 —— 还原后仍是模拟 token）则退回登出路径：宁可让用户
 * 重新登录，也绝不继续以错误身份发请求。
 *
 * 闭环自洽：还原后 `expires_at` 为 null ⇒ 不排续期定时器，由退出后第一次业务 401 驱动
 * 正常 refresh（原租户 cookie 此时重新合法），因此不会掉登录页。
 */
export async function autoExitImpersonation(): Promise<void> {
  try {
    await endImpersonation();
  } catch {
    /* 审计失败不阻塞本地还原 */
  }
  restoreAuthFromImpersonation();
  if (isImpersonating()) {
    await logout();
    return;
  }
  try {
    writeCachedUser(await fetchCurrentUser());
  } catch {
    /* /auth/me 失败不阻塞重载：下一次 401 会正常续期 */
  }
  window.location.reload();
}

/**
 * 构造企业 SSO 登录发起地址（纯函数，不发请求）。
 *
 * 调用方须做**顶层导航**（window.location.assign）到该地址，后端会 302 到
 * Keycloak authorize 页面。不能用 apiClient/fetch：授权码流程依赖浏览器整页跳转
 * 与后续 httpOnly cookie 下发。
 *
 * - tenant_id：后端据此推导 Keycloak realm（jonex-{tenant_id}），必须是合法业务租户，
 *   不能是空串或默认租户，否则后端返回 403（code 3005）。
 * - redirect_uri：**登录成功后的前端落点页**（不是 OIDC 回调地址，回调由后端配置
 *   KEYCLOAK_REDIRECT_URI）；必须命中后端 AUTH_ALLOWED_REDIRECT_URIS 白名单，
 *   否则后端返回 403（code 3006）。缺省取当前页面 origin 根路径，保证与部署地址一致。
 *   VITE_SSO_REDIRECT_URI 仅用于 shell 以独立地址（如 :5173）运行、且该地址已加入
 *   白名单的场景。
 */
export function buildSsoAuthorizeUrl(tenantId: string): string {
  const landing =
    ((import.meta as any).env?.VITE_SSO_REDIRECT_URI as string | undefined) ||
    `${window.location.origin}/`;
  const params = new URLSearchParams({ tenant_id: tenantId, redirect_uri: landing });
  return `/api/v1/auth/oidc/authorize?${params.toString()}`;
}

export async function createLoginTicket(appId: string, redirectUri: string, state: string): Promise<LoginTicketResult> {
  return apiClient.post<LoginTicketResult>('/auth/login-ticket', {
    appId,
    redirectUri,
    state,
  });
}

export async function logout(): Promise<void> {
  // 先让后端吊销会话并清 httpOnly 的 refresh cookie，再清本地并跳转。
  // 只清 storage 是「假登出」：refresh token 在 httpOnly cookie 里，JS 删不掉，
  // 服务端会话也仍然有效 —— 登出后访问根路径会被 /auth/refresh 静默恢复登录态。
  // SSO 会话（sso_logout_url 非 null）必须由顶层导航到 Keycloak 才能结束，故走 finishLogout。
  const ssoLogoutUrl = await revokeServerSession();
  clearTokens();
  // 登出即视为那次 SSO 导航的收尾，哨兵不应跨会话/跨用户残留：哨兵存在 sessionStorage，
  // 按 origin + 标签页隔离而**不按用户**，本可存活 5 分钟；若不清除，同一标签页换人登录后
  // 再登出，登录页会消费到上一个用户留下的失败痕迹，弹出张冠李戴的提示。
  clearSsoNavigationInFlight();
  finishLogout(ssoLogoutUrl, { loginUrl: '/login' });
}

export default apiClient;
