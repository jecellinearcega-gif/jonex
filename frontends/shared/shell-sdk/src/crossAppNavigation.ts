/**
 * 跨应用导航协议。
 *
 * 子应用之间互相跳转（如数据运营「前往知识库」→ core-business）用不了 ShellContext.navigate ——
 * 它只会拼本应用的 basePath。改走本协议，由 shell 的 router 做导航，文档不重载。
 *
 * 两条通道按 shell 的两种挂载形态分流：
 * - hosted（Module Federation）：shell 与本窗同窗，同步派发 jonex:navigate 事件，
 *   接管时当场 acknowledge，调用方据此判断「有没有人接」；
 * - iframe fallback（本地开发的默认形态）：本窗是被 shell 嵌入的 standalone，事件在另一份
 *   文档里，改发 postMessage 给父窗。
 *
 * 两条都接不上才回落整页导航（改造前的行为）。
 */
import { isEmbedded } from './embed';

/** 跨应用导航事件名（同窗事件与 postMessage 共用）。与 jonex:token-expired / jonex:locale-change 同属窗口级约定。 */
export const CROSS_APP_NAVIGATE_EVENT = 'jonex:navigate';

export interface CrossAppNavigateDetail {
  /** 目标路径，形如 `/apps/<appId>/...`。是否归本协议接管由 shell 判断（协议只覆盖跨应用跳转） */
  path: string;
  /** 接收方（shell）接管后调用，标记本次导航已由 SPA 路由完成 */
  acknowledge: () => void;
}

/**
 * 跳转到另一个子应用。返回 true = 已由 shell 的 SPA 路由接管（文档未重载）。
 *
 * 回落到整页导航时目标是**顶层窗口**：hosted 下它就是本窗，而 iframe fallback 下
 * 本窗是被嵌的子应用 —— 把 `/apps/...` 交给本窗会把子应用套进子应用，只有顶层窗口才是 shell。
 */
export function navigateCrossApp(path: string): boolean {
  if (typeof window === 'undefined') return false;

  let handled = false;
  window.dispatchEvent(
    new CustomEvent<CrossAppNavigateDetail>(CROSS_APP_NAVIGATE_EVENT, {
      detail: {
        path,
        acknowledge: () => {
          handled = true;
        },
      },
    }),
  );
  if (handled) return true;

  // iframe fallback：父窗一定是 shell（embed 标记就是它注入的），postMessage 的 targetOrigin
  // 取本窗 origin 即同源校验。回执拿不到、也没法同步等，故直接当作已接管 —— 唯一的例外是
  // 父窗跑着不认识本协议的旧 shell，那时表现为「点了没反应」，刷新页面即恢复。
  if (isEmbedded() && window.parent !== window) {
    window.parent.postMessage({ type: CROSS_APP_NAVIGATE_EVENT, path }, window.location.origin);
    return true;
  }

  // 跨域 iframe 下读写 window.top 会抛 SecurityError：退回本窗，不能让调用方拿到异常
  try {
    const target = window.top || window;
    target.location.href = path;
  } catch {
    window.location.href = path;
  }
  return false;
}
