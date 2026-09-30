import React, { useEffect, useState } from 'react';
import { Button, Alert, message } from 'antd';
import { LogoutOutlined } from '@ant-design/icons';
import * as Icons from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { switchLocale } from '@jonex/i18n-resources';
import { IMPERSONATION_EXPIRED_EVENT } from '@jonex/shell-sdk';
import { getUser, logout, exitImpersonation, autoExitImpersonation } from '../../api/auth';
import {
  IMPERSONATION_WARN_AHEAD_MS,
  formatImpersonationRemaining,
  startImpersonationExpiryWatch,
  subscribeImpersonationRemaining,
} from '../../utils/impersonationSession';
import { fetchMyMenus } from '../../api/menus';
import type { MenuNode } from '../../api/menus';
import { fetchUiConfig } from '../../api/uiConfig';
import type { ShellUiConfig } from '../../api/uiConfig';
import TaskCenterEntry from '../TaskCenterEntry';
import { TASK_CENTER_ENTRY_ENABLED } from '../../utils/taskCenterEntry';
import SpaceSwitcher from '../SpaceSwitcher';
import TenantImpersonationSwitcher from '../TenantImpersonationSwitcher';
import { AppLayout } from '@jonex/platform-theme';
import type { LayoutNavItem } from '@jonex/platform-theme';
import { userDisplayName, userRoleLabel } from '../../utils/userDisplay';

/** antd 图标名（如 'SearchOutlined'）→ 图标组件实例（导航图标由壳解析为 ReactNode 传入引擎） */
const resolveMenuIcon = (iconName: string): React.ReactNode => {
  const IconCmp = (Icons as unknown as Record<string, React.ComponentType | undefined>)[iconName];
  return IconCmp ? <IconCmp /> : null;
};

export default function AppShellLayout({ children }: { children: React.ReactNode }) {
  const { t, i18n } = useTranslation();
  const [user] = useState(() => getUser());
  const navigate = useNavigate();
  const location = useLocation();
  const [menus, setMenus] = useState<MenuNode[]>([]);
  const [menuLoading, setMenuLoading] = useState(true);
  // 外壳 UI 开关（顶栏「任务中心」按钮显隐）。取值失败时 fetchUiConfig 自己回落成
  // 「不隐藏」，所以这里不需要错误态 —— 拿不到信号时按显示来。
  const [uiConfig, setUiConfig] = useState<ShellUiConfig | null>(null);
  const [exiting, setExiting] = useState(false);
  const [impersonationRemainingMs, setImpersonationRemainingMs] = useState<number | null>(null);

  // 模拟态到期看护：进入最后 60 秒弹一次 Toast + 横幅转常驻倒计时，到点自动退出模拟。
  // 判据在 startImpersonationExpiryWatch 内部（access token 的 impersonated claim），
  // 这里**不做前置判断**：非模拟态它直接空转返回，绝不排定时器、不产生副作用。
  // 定时器是内存态，switchTenant() 的整页重载会清掉它 —— 重载后由这里重新挂上。
  useEffect(() => {
    const unsubscribe = subscribeImpersonationRemaining(setImpersonationRemainingMs);
    startImpersonationExpiryWatch({
      onWarn: () => message.warning(t('impersonation.expiring')),
      onExpired: autoExitImpersonation,
    });
    return unsubscribe;
  }, [t]);

  // 模拟态会话在业务请求上失效（401）：shell-sdk 的 emitSessionExpired() 会改道发这个事件，
  // 语义是「还原原租户」而不是「登出」（登出会清掉回原租户的唯一凭证）。
  useEffect(() => {
    const handler = () => {
      void autoExitImpersonation();
    };
    window.addEventListener(IMPERSONATION_EXPIRED_EVENT, handler);
    return () => window.removeEventListener(IMPERSONATION_EXPIRED_EVENT, handler);
  }, []);

  // 侧边栏菜单来自后端 /menus/my（按用户权限码过滤后的菜单树）
  useEffect(() => {
    fetchMyMenus()
      .then(setMenus)
      .catch(() => setMenus([]))
      .finally(() => setMenuLoading(false));
  }, []);

  // 外壳 UI 开关（部署级 /platform/frontend/ui-config
  // 值由后端按 env 解析好，前端照值渲染）。fetchUiConfig 内部已吞掉失败并回落成「不隐藏」，
  // 所以不挂 .catch —— 一个开关读不到不该让外壳报错。
  useEffect(() => {
    void fetchUiConfig().then(setUiConfig);
  }, []);

  // 语言切换副作用收敛到 @jonex/i18n-resources 的 switchLocale（更新实例 + 持久化 + 广播同窗/iframe 子应用）
  const handleLocaleChange = (locale: string) => {
    switchLocale(i18n, locale);
  };

  // 后端菜单树 → 引擎受控数据（label 翻译、icon 解析成 ReactNode；嵌套递归）
  // 数据量小，无需 memo：菜单加载/路由变化时随渲染派生即可
  const toNavItem = (node: MenuNode): LayoutNavItem => ({
    key: String(node.id),
    label: t(node.name),
    icon: node.icon ? (resolveMenuIcon(node.icon) ?? undefined) : undefined,
    path: node.path ?? undefined,
    children: node.children?.length ? node.children.map(toNavItem) : undefined,
  });
  const navItems: LayoutNavItem[] = menus.map(toNavItem);

  return (
    <AppLayout
      logo={{ href: '/apps/core-business/knowledge-search', alt: 'Jonex', locale: i18n.language }}
      navItems={navItems}
      sidebarExtra={({ collapsed }) => <SpaceSwitcher collapsed={collapsed} />}
      navLoading={menuLoading}
      activePath={location.pathname}
      onNavigate={(path) => navigate(path)}
      showCollapseTrigger
      topbarRight={{
        // 顺序即顶栏右侧的显示顺序（AppLayout 内部按 custom → language → user 渲染）
        custom: ({ collapsed }) => (
          <>
            {TASK_CENTER_ENTRY_ENABLED && (
              <TaskCenterEntry user={user} collapsed={collapsed} hidden={uiConfig?.hideTaskCenter} />
            )}
            <TenantImpersonationSwitcher user={user} collapsed={collapsed} />
          </>
        ),
        language: {
          current: i18n.language,
          onSwitch: handleLocaleChange,
        },
        user: user
          ? {
              avatarText: userDisplayName(user, t),
              name: userDisplayName(user, t),
              subtitle: userRoleLabel(user, t),
              actions: [{ key: 'logout', icon: <LogoutOutlined />, label: t('auth.logout'), onClick: () => logout() }],
            }
          : undefined,
      }}
    >
      {user?.impersonated && (
        <div style={{ position: 'sticky', top: 0, zIndex: 100 }}>
          <Alert
            type="warning"
            showIcon
            banner
            // E2E 量具契约：值必须与 frontends/e2e/lib/harness.ts 的 TESTID.banner 逐字符一致。
            // antd Alert 会把 data-* 透传到带 role="alert" 的根 div（v6: pickAttrs(otherProps,{data:true})）。
            data-testid="impersonation-banner"
            message={
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                {t('impersonation.banner', { tenant: user.tenantName || user.tenantId })}
                {impersonationRemainingMs !== null && impersonationRemainingMs <= IMPERSONATION_WARN_AHEAD_MS && (
                  // E2E 契约：TESTID.countdown（frontends/e2e/lib/harness.ts），逐字符一致
                  <span data-testid="impersonation-countdown">
                    {t('impersonation.countdown', { time: formatImpersonationRemaining(impersonationRemainingMs) })}
                  </span>
                )}
                <Button
                  type="link"
                  size="small"
                  loading={exiting}
                  onClick={async () => {
                    setExiting(true);
                    try {
                      await exitImpersonation();
                    } catch {
                      // 失败已由响应拦截器 toast
                    } finally {
                      setExiting(false);
                    }
                  }}
                >
                  {t('impersonation.exit')}
                </Button>
              </span>
            }
          />
        </div>
      )}
      {children}
    </AppLayout>
  );
}
