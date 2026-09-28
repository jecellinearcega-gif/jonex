import { apiClient } from './auth';

/** 外壳 UI 开关（后端 `GET /platform/frontend/ui-config`） */
export interface ShellUiConfig {
  /**
   * 是否隐藏 **shell 顶栏**那个「任务中心」快捷按钮。
   *
   * 服务端已按部署级环境变量 `HIDE_TASK_CENTER` 解析好，前端**照值渲染**即可，
   * 不要再自己判断（与登录页那套 `hide_tenant_input` / `enabled` 同一个口径）。
   */
  hideTaskCenter: boolean;
}

/**
 * 配置接口不可用时的兜底：**不隐藏**。
 *
 * 方向与登录页那个兜底（拿不到配置就**隐藏** SSO 入口）相反，是有理由的：那边藏错
 * 只是少一个入口、露错会给出一个必然报错的按钮挡住登录这条最后退路，所以保守取「隐藏」；
 * 这边藏错了同样是少一个入口，但**没配开关的环境莫名少了入口**更难被发现。
 * 所以拿不到信号时按「不隐藏」——隐藏必须是显式动作。
 */
const UI_CONFIG_FALLBACK: ShellUiConfig = { hideTaskCenter: false };

/**
 * 拉取外壳 UI 开关（顶栏「任务中心」按钮显隐）。
 *
 * 走已登录路径（普通鉴权，不像 `/auth/login-config` 那样免鉴权 —— 外壳渲染时用户已经登录了）。
 * 失败时静默回落为**不隐藏**：外壳不该因为一个开关读不到而报错，也不该让入口闪动。
 */
export async function fetchUiConfig(): Promise<ShellUiConfig> {
  try {
    const raw = await apiClient.get<{ hide_task_center?: boolean }>('/platform/frontend/ui-config');
    // 显式判 `=== true`：新前端 + 旧后端（响应里还没这个键）要明确落回「不隐藏」，
    // 而不是依赖 Boolean(undefined) 这种「碰巧对」的写法。
    return { hideTaskCenter: raw?.hide_task_center === true };
  } catch {
    return UI_CONFIG_FALLBACK;
  }
}
