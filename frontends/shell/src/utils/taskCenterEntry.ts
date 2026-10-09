import { isPathActive } from '@jonex/platform-theme';

/** 任务中心在宿主里的完整路径：hosted 前缀 /apps/<appId> + 子应用内部路由 /task-center */
export const TASK_CENTER_PATH = '/apps/platform-management/task-center';

/**
 * 顶栏入口开关。**2026-10-08 起放开**：能力侧判权已落地
 * （`capabilities/task_center/capability.py` 的 `_ACTION_PERMISSIONS` 按 read / write 判权），
 * 「后端没有任何 RBAC 判权、放出去等于谁都能停任务」这个关闭理由不再成立。
 *
 * 留开关而不是删渲染行：入口组件与 `canSeeTaskCenter` 的权限口径要保持可验证。
 */
export const TASK_CENTER_ENTRY_ENABLED = true;

/** 任务中心自己的码（2026-10-08 起与「任务调度」的 `platform:task:read` 分开） */
export const TASK_CENTER_PERMISSION = 'task_center:read';

/**
 * 是否处于任务中心页。委托引擎的 active 判定，与侧栏高亮共用同一口径：
 * 两份实现会在口径演进（trailing slash 归一、支持 query/hash）时静默分叉。
 */
export const isTaskCenterPath = (pathname: string): boolean => isPathActive(TASK_CENTER_PATH, pathname);

/** 是否持进入任务中心的权限码。无码者在子应用 router 守卫处会被 redirect 到 403 */
export const canSeeTaskCenter = (permissions: readonly string[] | undefined): boolean =>
  Boolean(permissions?.includes(TASK_CENTER_PERMISSION));

/**
 * 顶栏入口**是否渲染** = 持权限码 **且** 本部署没隐藏它。
 *
 * ⚠ 只作用于**顶栏这一个按钮**。子应用侧栏入口与路由守卫仍只看 `canSeeTaskCenter`
 * （= 只看权限码），所以本部署隐藏**不阻止**任何人从侧栏或直接输 URL 进入任务中心页面
 * —— 这是 2026-10-09 定的口径，**别把这个开关接到侧栏或路由守卫上**（那就成了「关功能」）。
 *
 * ⚠ `hidden` 为 `undefined`（老后端 / 接口没回 / 兜底路径）一律按**不隐藏**处理：
 * 隐藏必须是显式动作，拿不到信号时宁可显示。
 */
export const canShowTaskCenterEntry = (
  permissions: readonly string[] | undefined,
  hidden: boolean | undefined,
): boolean => canSeeTaskCenter(permissions) && hidden !== true;
