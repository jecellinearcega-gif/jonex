/**
 * 「主动登出」的两个哨兵。
 *
 * 两个键的需求**字面相反**，因此刻意用**两种介质** —— 这不是疏漏，是解耦：
 *
 * - `NAV_GUARD_KEY`（8s）需要「**随导航销毁**」。它服务的是**同一文档内跨模块**的对话
 *   （hosted 下 shell 与 remote 子应用是两个 bundle 拷贝，只有 window 这个同窗表面能对话），
 *   用来在导航提交前挡住 1.5s 轮询 / `jonex:token-expired` 的覆盖源。新文档里它**必须消失**，
 *   否则新文档的收尾跳转会被压制 8 秒。⇒ **window.top 属性**。
 *
 * - `LOGOUT_SETTLE_KEY`（30s）需要「**跨文档存活**」。它的消费点在登出**往返 Keycloak 之后
 *   的新文档**里（KC logout 直接 302 转回，浏览器加载全新文档），用来判定这次落点是不是
 *   自己那次登出 —— 是则不加 `?expired=1`。⇒ **sessionStorage**（同 origin + 同标签页存活）。
 *
 * ⚠️ **切勿「统一介质」把 NAV 也搬进 sessionStorage**：它跨文档存活后，新文档里第一次
 * `redirectToLogin()` 会因 NAV 仍在 8s 窗口内而让位（`sessionExpired.ts`），把本该立即执行的
 * 收尾跳转推迟到 8 秒后。两个键的介质是**功能性的**，不是风格问题。
 *
 * ⚠️ 前置条件：sessionStorage 严格按 `scheme+host+port` 隔离。后端下发的
 * `post_logout_redirect_uri` 必须与前端 origin **同源**，否则本标记同样读串 —— 且 Keycloak
 * 白名单按主机名匹配、**忽略端口**，端口不一致时它会静默放行，不报任何错。
 *
 * 注意：本模块刻意不 import ./sessionExpired —— 导航收口在那边反向依赖本模块，这里再依赖
 * 回去会形成循环 import。
 */
const NAV_GUARD_KEY = '__JONEX_LOGOUT_NAV_AT__';
const LOGOUT_SETTLE_KEY = '__JONEX_LOGOUT_SETTLE_AT__';

/** 兜底窗口：超过该时长仍未完成顶层导航，视为导航被取消，重新放行登录页跳转 */
export const LOGOUT_NAV_GRACE_MS = 8000;

/** 主动登出「兜底收尾」窗口：比导航窗口长，覆盖哨兵过期后由轮询接手的那一次跳转 */
export const LOGOUT_SETTLE_MS = 30000;

/* ------------------------------------------------------------------ *
 * NAV 哨兵：window.top 属性
 * ------------------------------------------------------------------ */

/**
 * 优先取 window.top（同 index.ts 的 __SHELL_CONTEXT__ 惯例：hosted 下 shell 与子应用同窗）。
 *
 * 跨域 iframe 下 window.top 仍可读出，但**读写其属性会抛错**，故读写一律包 try/catch 并退回
 * 本窗 —— 绝不能让哨兵抛异常打断调用方（登出会因此走不到导航那一步；轮询每 1.5s 抛一次）。
 */
function guardHost(): Window | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.top || window;
  } catch {
    return window;
  }
}

/** 在 window.top（不可用时退回本窗）上读哨兵键；任何异常都视为「无值」 */
function readGuard(key: string): unknown {
  const w = guardHost();
  if (!w) return undefined;
  try {
    return (w as any)[key];
  } catch {
    try {
      return (window as any)[key];
    } catch {
      return undefined;
    }
  }
}

/** 在 window.top（不可用时退回本窗）上写哨兵键；失败静默忽略 */
function writeGuard(key: string, value: number): void {
  const w = guardHost();
  if (!w) return;
  try {
    (w as any)[key] = value;
  } catch {
    try {
      (window as any)[key] = value;
    } catch {
      /* 静默：哨兵写不进去退化为无保护，也不能阻断登出 */
    }
  }
}

/* ------------------------------------------------------------------ *
 * SETTLE 哨兵：sessionStorage（跨文档存活）
 * ------------------------------------------------------------------ */

/**
 * 「本模块实例已消费过」标记：把「一次性」从「依赖 storage 可写」变成硬保证。
 *
 * 为什么需要它：一次性原本只靠 `removeItem` 实现。若 removeItem 抛异常（storage 被中途禁用），
 * 标记仍在，30s 窗口内每次调用都会返回 true ⇒ **误吞这 30s 内的真·会话过期**（该弹的提示不弹）。
 * 只在同一文档内有效（整页重载会重置）—— 这正是需要的范围，跨文档的持久性由 sessionStorage 负责。
 *
 * 与 ssoInflight.ts 的 consumed 同构。
 */
let settleConsumed = false;

/**
 * 取 sessionStorage；不可用一律返回 null。
 *
 * 每次调用都重新探测而非在模块级缓存：一是模块可能在无 window 环境被 import（vitest 是 node
 * 环境），缓存会让守卫失效；二是浏览器可能中途禁用 storage，此时旧引用开始抛异常。
 *
 * ⚠️ 「读属性本身就抛 SecurityError」是真实存在的一档（Safari 阻止全部 Cookie / 无痕严格设置），
 * `typeof window === 'undefined'` 挡不住它（window 在、getter 抛），故必须 try/catch 包住。
 */
function getSessionStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage || null;
  } catch {
    return null;
  }
}

/** 读 SETTLE 原始值；不可用/异常一律返回 null */
function readSettle(): string | null {
  const storage = getSessionStorage();
  if (!storage) return null;
  try {
    return storage.getItem(LOGOUT_SETTLE_KEY);
  } catch {
    return null;
  }
}

/** 写 SETTLE；失败静默（写不进只退化为「无保护」，绝不能阻断登出） */
function writeSettle(value: number): void {
  const storage = getSessionStorage();
  if (!storage) return;
  try {
    storage.setItem(LOGOUT_SETTLE_KEY, String(value));
  } catch {
    /* 静默 */
  }
}

/** 删 SETTLE；失败静默（删不掉由内存护栏兜底，不影响正确性） */
function deleteSettle(): void {
  const storage = getSessionStorage();
  if (!storage) return;
  try {
    storage.removeItem(LOGOUT_SETTLE_KEY);
  } catch {
    /* 静默 */
  }
}

/* ------------------------------------------------------------------ *
 * 对外 API
 * ------------------------------------------------------------------ */

/** 标记本窗已发起主动登出的顶层导航。须在**发起导航之前**调用，否则挡不住随后的轮询/事件覆盖源。 */
export function markLogoutNavigationInFlight(): void {
  const now = Date.now();
  // 新一次登出重新开启消费资格：用户失败后重试登出必须还能被识别。
  // 放在 storage 守卫之前，是因为它标的是「刚发起了一次登出」这一事实，与 storage 是否可写无关
  // （写不进去时 consume 本就返回 false，复位标记不会造成误报）。
  settleConsumed = false;
  writeGuard(NAV_GUARD_KEY, now);
  writeSettle(now);
}

/** 登出顶层导航是否仍在途（带自过期，避免导航被取消后永久压制登录页跳转） */
export function isLogoutNavigationInFlight(): boolean {
  const at = readGuard(NAV_GUARD_KEY);
  return typeof at === 'number' && Date.now() - at < LOGOUT_NAV_GRACE_MS;
}

/**
 * 消费「主动登出兜底收尾」标记。
 * 返回 true = 本次是主动登出的收尾跳转，不应带 ?expired=1（用户是主动登出，不是会话过期）。
 *
 * 一次性（消费一次即失效），由两道保险共同保证：内存护栏 + 删键。缺一不可 ——
 * 删键可能失败，内存护栏保证即使删不掉也只消费一次，避免压制其后真正发生的会话过期提示。
 */
export function consumeLogoutSettle(): boolean {
  // 先置位再读：这是内存护栏成立的前提（removeItem 失败时，同一次事件循环内的两次消费
  // 不能都返回 true）。
  if (settleConsumed) return false;
  settleConsumed = true;

  const raw = readSettle();
  deleteSettle(); // 先消费后判定：读取失败也要把标记清掉，避免残留压制真·会话过期

  // 空串 / 非数字 / NaN / Infinity 全部归为无效值（Number('') === 0 会骗过 isFinite，故先做真值判断）
  const at = raw ? Number(raw) : Number.NaN;
  if (!Number.isFinite(at)) return false;

  // 必须同时卡下界：系统时钟向后跳（休眠恢复 / NTP 校正）会让 age 变负，而负数恒小于窗口，
  // 只看上界等于放行任意陈旧的值 —— 一个几天前的标记也会被当成刚回来。
  const age = Date.now() - at;
  return age >= 0 && age < LOGOUT_SETTLE_MS;
}
