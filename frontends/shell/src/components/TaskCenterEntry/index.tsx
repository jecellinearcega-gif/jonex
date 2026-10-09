import React from 'react';
import { Button } from 'antd';
import { ScheduleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import type { ShellUser } from '@jonex/shell-sdk';
import { TASK_CENTER_PATH, canShowTaskCenterEntry, isTaskCenterPath } from '../../utils/taskCenterEntry';

interface Props {
  user: ShellUser | null;
  /** 折叠态：只显示图标、隐藏文字标签，与相邻的「切换租户」一致 */
  collapsed?: boolean;
  /**
   * 部署级隐藏（后端 env `HIDE_TASK_CENTER`，经 `/platform/frontend/ui-config` 下发）。
   * 缺省 / `undefined` = 不隐藏。
   */
  hidden?: boolean;
}

/** 顶栏「任务中心」快捷入口：持 task_center:read 且本部署没隐藏才可见，停留在任务中心页时高亮 */
export default function TaskCenterEntry({ user, collapsed = false, hidden = false }: Props) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();

  // 两个条件一起判（见 canShowTaskCenterEntry）：
  //   ① 与子应用 router 守卫同码 —— 能进页面才看得到入口，避免「点了就 403」的死按钮；
  //   ② 部署级隐藏 —— ⚠ **只在这一个组件里生效**：侧栏入口与路由守卫仍按权限码放行，
  //      即本开关不阻止从侧栏或直接输 URL 进任务中心页面（2026-10-09 定的口径）。
  if (!canShowTaskCenterEntry(user?.permissions, hidden)) return null;

  const active = isTaskCenterPath(location.pathname);

  return (
    <Button
      type="text"
      icon={<ScheduleOutlined />}
      // antd 给图标渲染 <span role="img" aria-label="schedule"> 且未加 aria-hidden，
      // 图名会并进按钮的可访问名 —— 展开态实测 "schedule 任务中心"，折叠态文字不渲染则只剩 "schedule"。
      // 故两态都显式指定，读屏与测试才拿到同一个名字。
      // 展开态另有视觉文字「任务中心」，与 aria-label 一致，不违反 label-in-name。
      aria-label={t('navigation.taskCenter')}
      // 折叠态无文字，补原生 tooltip 供鼠标用户识别
      title={collapsed ? t('navigation.taskCenter') : undefined}
      style={{
        fontSize: 14,
        ...(collapsed ? { padding: '0 8px' } : {}),
        // 借用侧栏选中项的变量：主题里只有它表达「淡主题底 + 主题色字」这层语义。
        // 代价是语义属主在侧栏——变量若改名，这里会静默退化为透明高亮而不报错。
        ...(active ? { background: 'var(--yx-sidebar-active)', color: 'var(--yx-accent)' } : {}),
      }}
      onClick={() => {
        // 已在该页时不再导航，否则会往 history 压一条重复记录
        if (!active) navigate(TASK_CENTER_PATH);
      }}
    >
      {!collapsed && t('navigation.taskCenter')}
    </Button>
  );
}
