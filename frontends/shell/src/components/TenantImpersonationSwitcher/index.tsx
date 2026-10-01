import React, { useEffect, useState } from 'react';
import { Button, Dropdown } from 'antd';
import { SwapOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { ShellUser } from '@jonex/shell-sdk';
import { listTenants, switchTenant } from '../../api/auth';
import type { TenantListItem } from '../../api/auth';

interface Props {
  user: ShellUser | null;
  /** 折叠态：只显示图标、隐藏文字标签，按钮更紧凑 */
  collapsed?: boolean;
}

/**
 * E2E 量具契约（`frontends/e2e/lib/harness.ts` 的 `TESTID`），值必须**逐字符一致**。
 * ⛔ 不要改成按按钮文案定位：antd 是 v6，两字按钮名会被插入空格 ⇒ 文案定位必然假 FAIL。
 * testid 挂在 antd 组件上是安全的：antd 会把 `data-*` 透传到根 DOM 元素
 * （Button → `<button>`；Dropdown 的 `menu.items` 配置项 → `<li>`，`MenuItemType` 显式声明了 `data-*`）。
 */
const TESTID_SWITCHER = 'tenant-impersonation-switcher';
const testIdSwitcherOption = (tenantId: string): string => `tenant-impersonation-option-${tenantId}`;

/** 顶栏「切换租户」入口：仅平台管理员、非模拟态可见；模拟态由横幅承接 */
export default function TenantImpersonationSwitcher({ user, collapsed = false }: Props) {
  const { t } = useTranslation();
  const [tenants, setTenants] = useState<TenantListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!user?.isPlatformAdmin || user.impersonated) return;
    let cancelled = false;
    setLoading(true);
    listTenants()
      .then((data) => {
        if (!cancelled) setTenants(data.items ?? []);
      })
      .catch(() => {
        if (!cancelled) setTenants([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [user?.isPlatformAdmin, user?.impersonated]);

  if (!user?.isPlatformAdmin || user.impersonated) return null;

  const switchable = tenants.filter((item) => item.id !== user.tenantId);

  return (
    <Dropdown
      placement="bottomRight"
      disabled={loading || switchable.length === 0}
      menu={{
        items: switchable.map((item) => ({
          key: item.id,
          label: item.name,
          'data-testid': testIdSwitcherOption(item.id),
          onClick: async () => {
            setBusy(true);
            try {
              await switchTenant(item.id);
            } catch {
              // 失败已由响应拦截器 toast
            } finally {
              setBusy(false);
            }
          },
        })),
      }}
    >
      <Button
        type="text"
        icon={<SwapOutlined />}
        loading={busy || loading}
        data-testid={TESTID_SWITCHER}
        style={{ fontSize: 14, ...(collapsed ? { padding: '0 8px' } : {}) }}
      >
        {!collapsed && t('impersonation.switch')}
      </Button>
    </Dropdown>
  );
}
