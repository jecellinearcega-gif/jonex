/**
 * 模拟租户会话（impersonation）的到期看护。
 *
 * 模拟 access token 的有效期 = IMPERSONATE_EXPIRE_MINUTES（默认 30 分钟），且**没有任何
 * 续期凭证**（见 shell-sdk/autoRefresh.ts 的模拟态短路）—— 到期只能自动退出模拟、回到原租户。
 *
 * 以 token 自己的 `exp` 为基准（不用 jonex_token_expires_at，那个键在模拟态被刻意清空）：
 *
 * | 时点 | 行为 |
 * | --- | --- |
 * | exp - 60s | onWarn() 恰好一次（Toast）+ 横幅切常驻倒计时（订阅者自取剩余毫秒） |
 * | exp | onExpired() 恰好一次（还原原租户 + 整页重载） |
 *
 * ⛔ 非模拟态（本地密码登录 / SSO）**不排任何定时器、不产生任何副作用**：
 * 判据是 access token 的 impersonated claim，解不出 exp 也一律不排（判不准就不动）。
 */
import { isImpersonating, readAccessTokenExpiresAtMs } from '@jonex/shell-sdk';

/** 到期前多久开始提示（与 autoRefresh 的提前量同量级，便于用户读完提示） */
export const IMPERSONATION_WARN_AHEAD_MS = 60 * 1000;
/** 倒计时刷新间隔 */
const TICK_INTERVAL_MS = 1000;

export interface ImpersonationExpiryHandlers {
  /** 到期：还原原租户会话并整页重载 */
  onExpired: () => void | Promise<void>;
  /** 进入最后 60 秒：提示一次（Toast） */
  onWarn?: () => void;
}

let started = false;
let warned = false;
let expiring = false;
let timer: ReturnType<typeof setInterval> | null = null;
let activeHandlers: ImpersonationExpiryHandlers | null = null;
let remainingMs: number | null = null;
const listeners = new Set<(remainingMs: number | null) => void>();

/** 剩余毫秒 → `mm:ss`（向上取整到秒；负数夹到 00:00，不出现 `-00:01`） */
export function formatImpersonationRemaining(ms: number): string {
  const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

/** 当前剩余毫秒；未在看护中（或已到期）返回 null */
export function getImpersonationRemainingMs(): number | null {
  return remainingMs;
}

/** 订阅剩余毫秒变化（横幅倒计时用）。订阅即刻回调一次当前值，返回取消订阅函数。 */
export function subscribeImpersonationRemaining(listener: (remainingMs: number | null) => void): () => void {
  listeners.add(listener);
  listener(remainingMs);
  return () => {
    listeners.delete(listener);
  };
}

function publish(next: number | null): void {
  remainingMs = next;
  listeners.forEach((listener) => listener(next));
}

/** 停止看护并清空剩余值（登出 / 会话失效 / 退出模拟时调用） */
export function stopImpersonationExpiryWatch(): void {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  started = false;
  expiring = false;
  activeHandlers = null;
  publish(null);
}

async function expire(): Promise<void> {
  if (expiring) return; // 到期只收口一次
  expiring = true;
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  publish(null);
  try {
    await activeHandlers?.onExpired();
  } catch {
    /* 退出流程内部已按「本地还原优先」降级；这里只保证定时器循环不被异常带崩 */
  }
}

/**
 * 开始看护（幂等）。
 *
 * 非模拟态、或模拟 token 解不出 exp 时**直接返回**，不排定时器、不产生任何副作用。
 * 注意：看护是**内存态**，`switchTenant()` 之后的整页重载会把它清掉 ——
 * 重载后必须由 AppShellLayout 挂载时再调一次（那才是真正的宿主）。
 */
export function startImpersonationExpiryWatch(handlers: ImpersonationExpiryHandlers): void {
  if (started) return;
  if (!isImpersonating()) return;
  const expiresAt = readAccessTokenExpiresAtMs();
  if (expiresAt === null) return;

  started = true;
  warned = false;
  expiring = false;
  activeHandlers = handlers;

  const tick = () => {
    const left = expiresAt - Date.now();
    if (left <= 0) {
      void expire();
      return;
    }
    publish(left);
    if (left <= IMPERSONATION_WARN_AHEAD_MS && !warned) {
      warned = true; // 「仅一次」：余下会话期间不再重复弹
      activeHandlers?.onWarn?.();
    }
  };

  timer = setInterval(tick, TICK_INTERVAL_MS);
  tick();
}
