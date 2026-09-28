/**
 * 「企业 SSO 登录正在顶层导航中」哨兵 —— 跨文档版本。
 *
 * 场景：点「企业 SSO 登录」后走 window.location.assign 跳出本应用去 Keycloak。跳出去之后本应用
 * 完全失去控制权，Keycloak 不可达 / 租户 ID 填错（realm 不存在）/ 落点未放行这三种情况下，用户
 * 会停在浏览器错误页、Keycloak 错误页或 JSON 页，既回不来也不知道发生了什么。故：跳转前落一个
 * 时间戳，用户回到登录页时消费它 → 弹一次中性提示给出出口。
 *
 * 为什么用 sessionStorage，而**不能**复用 ./logoutNavigation 那套 window.top 键：
 * logoutNavigation 服务的是「同一文档内跨模块对话」（hosted 下 shell ↔ remote 子应用同窗同文档，
 * 只有 window 这个同窗表面能对话）。本例是「跨文档存活」——导航去了 Keycloak 的 origin 再回来：
 *   - bfcache 恢复（同一文档、同一 window）→ window 键还在，sessionStorage 键也在；
 *   - 全量重载（新文档、新 window）→ window 键**没了**，sessionStorage 键还在。
 * 两条路径浏览器都可能走，复用 window 键会退化成「有时提示、有时不提示」的间歇性失效。
 * sessionStorage 按 origin + 标签页隔离：导航去别的 origin 不丢，关标签页自动清 —— 正是需要的生命周期。
 *
 * 可靠性纪律：这是兜底机制，绝不能反过来成为故障源。隐私模式 / 禁用 storage / 无 window
 * （SSR、vitest node 环境）下三处访问一律静默降级，绝不抛异常阻断登录。
 */

export const SSO_IN_FLIGHT_KEY = 'jonex.sso.inflight.at';

/**
 * 哨兵最大存活期。
 *
 * 为什么是 15 分钟：企业 SSO 往往要经 Casdoor → 企业 IdP → MFA 多跳，用户在 IdP 页上停留
 * 超过 5 分钟是常态。窗口取小了，用户点完 SSO 卡在 IdP 页、再后退回登录页时会一句话都没有，
 * 剩下的只有迷惑（这与后端 state 无关：state 约束的是 authorize → callback，本哨兵约束的是
 * 点击 → 回到登录页，两者衡量的不是同一段旅程，取值只需各自覆盖自己的场景）。
 *
 * 窗口偏大的误报风险（很久以前点过 SSO、回登录页却莫名看到提示）由「认证成功即清哨兵」
 * （clearSsoNavigationInFlight）兜住，窗口本身只需挡住足够老的陈旧值，不必精确。
 */
export const SSO_IN_FLIGHT_MAX_AGE_MS = 15 * 60 * 1000;

/**
 * 「本模块实例已消费过」标记：把「一次性」从「依赖 storage 可写」变成硬保证。
 *
 * 只在同一文档内有效（整页重载会重置），这正是需要的范围 —— 跨文档的持久性由 sessionStorage 负责。
 */
let consumed = false;

/**
 * 取 sessionStorage；不可用一律返回 null。
 *
 * 每次调用都重新探测而非在模块级缓存：一是模块可能在无 window 环境被 import（缓存会让守卫失效），
 * 二是浏览器可能中途禁用 storage（此时旧引用会开始抛异常）。
 */
function getSessionStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage || null;
  } catch {
    // 部分浏览器在「禁用第三方 cookie / 无痕」下读属性本身就抛 SecurityError
    return null;
  }
}

/** 标记本窗已发起 SSO 顶层导航。须在**发起导航之前**调用，否则跳走就来不及写了。 */
export function markSsoNavigationInFlight(): void {
  // 新一次导航重新开启消费资格：用户失败后重试 SSO 必须还能收到提示。
  // 放在 storage 守卫之前，是因为它标的是「刚发起了一次导航」这一事实，与 storage 是否可写无关
  // （写不进去时 consume 本就返回 false，复位标记不会造成误报）。
  consumed = false;

  const storage = getSessionStorage();
  if (!storage) return;
  try {
    storage.setItem(SSO_IN_FLIGHT_KEY, String(Date.now()));
  } catch {
    /* 静默：写不进去只退化为「不提示」，绝不能因此阻断登录跳转 */
  }
}

/**
 * 消费「刚从 SSO 导航回来」信号。
 *
 * 语义是「读到即清、一次性」：无论值是否有效、判定结果如何，都先把键清掉，
 * 这样用户之后手动回到登录页不会再重复弹提示。
 *
 * @returns true = 本次确实是一次存活期内的 SSO 跳转回跳，调用方应弹一次中性提示。
 */
export function consumeSsoReturnHint(): boolean {
  // 先置位再读 storage：removeItem 单独 try/catch 且失败仍要继续判定，若只靠删键实现一次性，
  // 读得到却删不掉时（同一次事件循环内的两次消费）会都返回 true → 弹两次提示。
  // 放在读 storage 之前是本兜底成立的前提。
  if (consumed) return false;
  consumed = true;

  const storage = getSessionStorage();
  if (!storage) return false;

  let raw: string | null = null;
  try {
    raw = storage.getItem(SSO_IN_FLIGHT_KEY);
  } catch {
    // 读失败保持初始值 null，按「无值」处理；下面的清除仍要尝试，避免脏值一直留着
  }

  try {
    storage.removeItem(SSO_IN_FLIGHT_KEY);
  } catch {
    /* 静默：删不掉最多导致下次多弹一次，不影响本次判定 */
  }

  // 空串 / 非数字 / NaN / Infinity 全部归为无效值（Number('') === 0 会骗过 isFinite，故先做真值判断）
  const at = raw ? Number(raw) : Number.NaN;
  if (!Number.isFinite(at)) return false;

  // 必须同时卡下界：系统时钟向后跳（WSL2 休眠恢复 / NTP 校正）会让 age 变负，而负数恒小于
  // MAX_AGE，只看上界等于放行任意陈旧的值 —— 一个几天前的哨兵也会被当成刚回跳。
  const age = Date.now() - at;
  return age >= 0 && age < SSO_IN_FLIGHT_MAX_AGE_MS;
}

/** 清除哨兵（供登出路径调用：主动登出后再回登录页不应弹「SSO 回跳」提示）。 */
export function clearSsoNavigationInFlight(): void {
  // 消费资格一并复位：清哨兵即宣告本轮 SSO 旅程结束（主动登出路径），之后回到登录页不应再弹。
  consumed = false;

  const storage = getSessionStorage();
  if (!storage) return;
  try {
    storage.removeItem(SSO_IN_FLIGHT_KEY);
  } catch {
    /* 静默：删不掉则等自过期窗口结束，不影响正确性 */
  }
}
