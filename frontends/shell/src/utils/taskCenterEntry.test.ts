/**
 * 顶栏「任务中心」入口的两个判据：**权限码** + **部署级隐藏**。
 *
 * 部署级隐藏来自后端 env `HIDE_TASK_CENTER`，经 `GET /platform/frontend/ui-config` 下发
 * （取值链路的测试在 `src/api/uiConfig.test.ts`）。本文件只测这两个判据的**组合语义**。
 *
 * ⚠ 范围锁：隐藏**只作用于顶栏那一个按钮**。持 `task_center:read` 的人从子应用侧栏或
 * 直接输 URL 仍能进任务中心页面 —— 这是 2026-10-09 定的口径。
 */
import { describe, expect, it } from 'vitest';
import {
  TASK_CENTER_ENTRY_ENABLED,
  TASK_CENTER_PERMISSION,
  TASK_CENTER_PATH,
  canSeeTaskCenter,
  canShowTaskCenterEntry,
  isTaskCenterPath,
} from './taskCenterEntry';

const WITH_PERMISSION = [TASK_CENTER_PERMISSION];
const WITHOUT_PERMISSION = ['user:read'];

describe('canShowTaskCenterEntry — 权限码 × 部署级隐藏', () => {
  it('有码、未隐藏 ⇒ 显示（我们自己的环境的常态）', () => {
    expect(canShowTaskCenterEntry(WITH_PERMISSION, false)).toBe(true);
    expect(canShowTaskCenterEntry(WITH_PERMISSION, undefined)).toBe(true);
  });

  it('有码、隐藏 ⇒ 不显示（客户环境配 HIDE_TASK_CENTER=true 的形态）', () => {
    expect(canShowTaskCenterEntry(WITH_PERMISSION, true)).toBe(false);
  });

  it('无码 ⇒ 不显示（隐藏与否都一样：本来就没入口，避免「点了就 403」的死按钮）', () => {
    expect(canShowTaskCenterEntry(WITHOUT_PERMISSION, false)).toBe(false);
    expect(canShowTaskCenterEntry(WITHOUT_PERMISSION, true)).toBe(false);
    expect(canShowTaskCenterEntry(undefined, false)).toBe(false);
  });

  it('`hidden` 只有严格 true 才算隐藏：老后端 / 接口没回（undefined）按显示', () => {
    // 隐藏必须是显式动作 —— 拿不到信号时宁可显示，否则没配开关的环境会莫名少入口。
    expect(canShowTaskCenterEntry(WITH_PERMISSION, undefined)).toBe(true);
    expect(canShowTaskCenterEntry(WITH_PERMISSION, false)).toBe(true);
  });
});

describe('范围锁 — 隐藏顶栏不等于关掉功能', () => {
  it('canSeeTaskCenter 完全不看隐藏开关（侧栏与路由守卫用的就是它）', () => {
    // 这两个函数签名不同正是范围锁本身：侧栏/守卫手上根本没有 hidden 这个入参。
    expect(canSeeTaskCenter(WITH_PERMISSION)).toBe(true);
    expect(canSeeTaskCenter(WITHOUT_PERMISSION)).toBe(false);
    expect(canSeeTaskCenter(undefined)).toBe(false);
  });

  it('隐藏与权限是两个正交的轴，四种组合都符合预期', () => {
    const combos: Array<[readonly string[] | undefined, boolean, boolean]> = [
      [WITH_PERMISSION, false, true], // 正常环境：看得见
      [WITH_PERMISSION, true, false], // 隐藏环境：顶栏没有，但 canSeeTaskCenter 仍为 true
      [WITHOUT_PERMISSION, false, false],
      [WITHOUT_PERMISSION, true, false],
    ];
    for (const [perms, hidden, expected] of combos) {
      expect(canShowTaskCenterEntry(perms, hidden)).toBe(expected);
      // 无论在哪种组合下，「能不能进页面」都只由权限码决定 —— 隐藏不参与
      expect(canSeeTaskCenter(perms)).toBe(!perms?.length ? false : perms.includes(TASK_CENTER_PERMISSION));
    }
  });
});

describe('编译期总开关与路径判定未被本次改动波及', () => {
  it('TASK_CENTER_ENTRY_ENABLED 仍是 true（2026-10-08 起放开）', () => {
    expect(TASK_CENTER_ENTRY_ENABLED).toBe(true);
  });

  it('isTaskCenterPath 仍按宿主完整路径判定（顶栏高亮用）', () => {
    expect(TASK_CENTER_PATH).toBe('/apps/platform-management/task-center');
    expect(isTaskCenterPath(TASK_CENTER_PATH)).toBe(true);
    expect(isTaskCenterPath('/apps/core-business/knowledge-search')).toBe(false);
  });
});
