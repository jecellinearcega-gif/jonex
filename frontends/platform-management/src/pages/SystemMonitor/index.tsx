import { Card, Empty } from 'antd';
import { DashboardOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { PageContent, PageHeader } from '@jonex/shared-lib';

/**
 * 全局系统监控 —— 占位页（研发中）。
 *
 * 建立此页的目的是给权限码 `platform:monitor:read` 一个真实落点：
 * 目标权限模型里「全局系统监控」是平台管理员的系统级能力，若只建码不建页，
 * 角色权限页会出现一个勾了也没有入口的权限项。
 *
 * 功能实现后替换本文件内容即可，路由与权限码无需再动。
 * 方案：docs/permissions/PERMISSIONS_REDESIGN.md §11.2
 */
export default function SystemMonitor() {
  const { t } = useTranslation();

  return (
    <>
      <PageHeader title={t('systemMonitor.title')} style={{ marginBottom: 4 }} />
      <p style={{ color: '#64748b', margin: '0 0 16px', fontSize: 14 }}>{t('systemMonitor.description')}</p>

      <PageContent showCard={false}>
        <Card>
          <Empty
            image={<DashboardOutlined style={{ fontSize: 56, color: '#cbd5e1' }} />}
            description={<span style={{ color: '#64748b' }}>{t('systemMonitor.underDevelopment')}</span>}
          />
        </Card>
      </PageContent>
    </>
  );
}
