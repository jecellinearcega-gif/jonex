import { clearAuthStorage } from './authStorage';
import { stopAutoRefresh } from './autoRefresh';
import { isImpersonating } from './impersonation';
import { buildLoginRedirectUrl, stripBlockedAuthQuery, stripCallbackQuery } from './authRedirect';
import { consumeLogoutSettle, isLogoutNavigationInFlight, markLogoutNavigationInFlight } from './logoutNavigation';

/**
 * 统一登录跳转与会话失效信号。
 *
 * 全前端「跳转到登录页」的代码收敛到这里：
 * - redirectToLogin() 是全前端唯一执行 location.href 跳转的实现；
 * - emitSessionExpired() 是 401 / 会话失效的唯一信号出口（清 token + 发事件，不跳转）；
 * - installSessionExpiredRedirect() 让 shell 与 standalone 子应用挂载同一个跳转执行者；
 * - finishLogout() 是主动登出的统一收尾（置哨兵 + 顶层导航到 Keycloak 或登录页）。
 */

export interface RedirectToLoginOptions {
  /** 登录页地址，默认 '/login' */
  loginUrl: string;
  /** 子应用 id（跨域 ticket 交换用），shell 宿主可不传 */
  appId?: string;
  /** 会话已过期标记（?expired=1），仅 401/过期跳转使用；登出、首次引导不传 */
  expired?: boolean;
}

/** 当前页面 URL，清理敏感 query（jonex_token 等）与 ticket 回调参数 */
export function getCleanCurrentUrl(): string {
  const url = new URL(window.location.href);
  stripBlockedAuthQuery(url);
  stripCallbackQuery(url);
  return url.toString();
}

/** 真正的登录页跳转实现。私有：不受哨兵约束，只给 redirectToLogin 与 finishLogout 用。 */
function navigateToLogin(options: RedirectToLoginOptions): void {
  let target = buildLoginRedirectUrl(options.loginUrl, getCleanCurrentUrl(), options.appId);
  // 主动登出的兜底收尾跳转不拼 ?expired=1：对用户而言那是一次主动登出，不是会话过期。
  // 短路顺序不可调换：只有确实要拼 expired 时才消费标记，否则会误吞真·会话过期路径的标记。
  if (options.expired && !consumeLogoutSettle()) {
    const u = new URL(target, window.location.origin);
    u.searchParams.set('expired', '1');
    target = u.toString();
  }
  window.location.href = target;
}

/**
 * 唯一登录页跳转收口：所有跳转 login 的地方都必须走这里（受主动登出在途哨兵约束）。
 *
 * 返回 true = 本次导航已发起；false = 因「主动登出顶层导航在途」而让位。
 * ⚠️ 调用方若靠一次性 latch（redirectingRef / handled）防重入，**必须只在 true 时上锁**，
 *    否则让位一次之后会永久失去兜底（哨兵过期后没人来收尾，用户停在无登录态的页面上）。
 */
export function redirectToLogin(options: RedirectToLoginOptions): boolean {
  if (isLogoutNavigationInFlight()) return false;
  navigateToLogin(options);
  return true;
}

/**
 * 主动登出统一收尾：先置哨兵、再发起导航。
 *
 * 顺序是硬约束，封进本函数即消除 6 个调用点各写两行时写反的时序隐患：
 * - 哨兵必须**先于**导航置位，否则挡不住随后 1.5s 轮询 / token-expired 事件的覆盖；
 * - ssoLogoutUrl 为 null（本地密码登录）时走私有 navigateToLogin，**不置哨兵**，
 *   行为与改动前完全一致，也不会让用户白等兜底窗口。
 */
export function finishLogout(ssoLogoutUrl: string | null, fallback: RedirectToLoginOptions): void {
  if (!ssoLogoutUrl) {
    navigateToLogin(fallback);
    return;
  }
  markLogoutNavigationInFlight();
  window.location.assign(ssoLogoutUrl);
}

/** 模拟态会话失效信号：语义是「还原原租户」，不是「登出」（见 emitSessionExpired） */
export const IMPERSONATION_EXPIRED_EVENT = 'jonex:impersonation-expired';

/** 向顶层窗口广播事件（跨 iframe 子应用），失败静默 */
function dispatchAuthEvent(type: string): void {
  try {
    (window.top || window.parent || window).dispatchEvent(new CustomEvent(type));
  } catch {
    /* swallow */
  }
}

/**
 * 会话失效信号：清 token + 发 jonex:token-expired 事件。
 *
 * 调试开关 localStorage['jonex_disable_401_redirect']='1' 时静默跳过
 * （不清 token 不发事件，仅由调用方 reject 给页面 catch），用于观察 401 不被踢回登录页。
 *
 * **模拟态改道**（硬不变量）：模拟会话失效必须走「还原原租户 + 重载」，不得走「登出」——
 * clearAuthStorage() 会删掉 JONEX_IMPERSONATION_BACKUP_KEY，那是回到原租户的唯一凭证，
 * 删了就再也退不出模拟。故模拟态只发 jonex:impersonation-expired，一个字节的 storage 都不动。
 * 判据是 access token 的 impersonated claim（isImpersonating），非模拟态路径行为完全不变。
 */
export function emitSessionExpired(): void {
  if (localStorage.getItem('jonex_disable_401_redirect') === '1') return;
  stopAutoRefresh(); // 会话失效：停止定时刷新，避免已清 token 后仍续期
  if (isImpersonating()) {
    dispatchAuthEvent(IMPERSONATION_EXPIRED_EVENT);
    return;
  }
  clearAuthStorage({ keepLocale: true });
  dispatchAuthEvent('jonex:token-expired');
}

/** 挂载会话失效跳转（幂等）：监听 jonex:token-expired → redirectToLogin。返回取消函数 */
export function installSessionExpiredRedirect(options: Omit<RedirectToLoginOptions, 'expired'>): () => void {
  let handled = false;
  const handler = () => {
    if (handled) return;
    // 让位（主动登出导航在途）时不锁定：留给兜底窗口过后重试，避免永久失去收尾
    if (!redirectToLogin({ ...options, expired: true })) return;
    handled = true;
  };
  window.addEventListener('jonex:token-expired', handler);
  return () => window.removeEventListener('jonex:token-expired', handler);
}
