import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Descriptions, Drawer, Flex, Skeleton, Space, Table, Typography } from 'antd';
import type { DescriptionsProps, TableProps } from 'antd';
import { useTranslation } from 'react-i18next';
import { fmtDateTime } from '../../utils/datetime';
import {
  getEntityTypeLabel,
  getSyncRecord,
  getSyncStatusMeta,
  getSyncTypeLabel,
  type SyncFailureDetail,
  type SyncRecordDetailResponse,
} from '../../api/identityIntegration';

/**
 * 「同步记录详情」侧边抽屉（E6）。
 *
 * 入口有两个：列表行经 `SyncRecordSummary.id` 打开，手动同步结束后由 E4 的 `task_id` 直接带出。
 *
 * E6 的 `user_change` 是对象（新增/更新/停用/启用/删除），与 E5 同名字段的标量语义不同。
 */
const { Text, Title } = Typography;

const DASH = '—';

interface SyncRecordDrawerProps {
  /** 非空即打开并按该 ID 拉取详情；null 表示关闭 */
  recordId: number | null;
  onClose: () => void;
}

export default function SyncRecordDrawer({ recordId, onClose }: SyncRecordDrawerProps) {
  const { t } = useTranslation();
  const [data, setData] = useState<SyncRecordDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = recordId != null;
  const dl = (key: string, options?: Record<string, unknown>) => t(`tenantManagement.recordDrawer.${key}`, options);

  const load = useCallback(
    async (id: number) => {
      setLoading(true);
      setError(null);
      try {
        setData(await getSyncRecord(id));
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : t('common.loadFailed'));
      } finally {
        setLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    if (recordId == null) return;
    load(recordId);
  }, [recordId, load]);

  const items: DescriptionsProps['items'] = data
    ? [
        { key: 'time', label: dl('syncTime'), children: data.sync_time ? fmtDateTime(data.sync_time) : DASH },
        { key: 'type', label: dl('syncType'), children: getSyncTypeLabel(t, data.sync_type) },
        {
          key: 'status',
          label: dl('status'),
          children: (() => {
            const meta = getSyncStatusMeta(t, data.status);
            return <Text type={meta.type}>{meta.label}</Text>;
          })(),
        },
        {
          key: 'change',
          label: dl('userChange'),
          children: (
            <Space size={16}>
              <span>
                {dl('added')} <Text strong>{data.user_change.added}</Text>
              </span>
              <span>
                {dl('updated')} <Text strong>{data.user_change.updated}</Text>
              </span>
              <span>
                {dl('disabled')} <Text strong>{data.user_change.disabled}</Text>
              </span>
              <span>
                {dl('enabled')} <Text strong>{data.user_change.enabled}</Text>
              </span>
              <span>
                {dl('deleted')} <Text strong>{data.user_change.deleted}</Text>
              </span>
            </Space>
          ),
        },
      ]
    : [];

  const columns: TableProps<SyncFailureDetail>['columns'] = [
    {
      title: dl('colEntityType'),
      dataIndex: 'entity_type',
      key: 'entity_type',
      width: 100,
      render: (v: string) => getEntityTypeLabel(t, v),
    },
    {
      title: dl('colEntityId'),
      dataIndex: 'external_entity_id',
      key: 'external_entity_id',
      width: 180,
      render: (v: string | null) => v || DASH,
    },
    { title: dl('colReason'), dataIndex: 'reason', key: 'reason', render: (v: string | null) => v || DASH },
  ];

  return (
    <Drawer title={dl('title')} open={open} onClose={onClose} width={640}>
      {loading ? (
        <Skeleton active title={false} paragraph={{ rows: 4 }} />
      ) : error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={() => recordId != null && load(recordId)}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : data ? (
        <Flex vertical gap={16}>
          <Descriptions size="small" column={1} items={items} />
          <Flex vertical gap={8}>
            <Title level={5} style={{ margin: 0 }}>
              {dl('failuresTitle', { count: data.failures.length })}
            </Title>
            <Table<SyncFailureDetail>
              columns={columns}
              dataSource={data.failures}
              rowKey={(r) => `${r.entity_type}-${r.external_entity_id ?? ''}-${r.reason ?? ''}`}
              size="small"
              locale={{ emptyText: dl('failuresEmpty') }}
              pagination={{ pageSize: 10, size: 'small', hideOnSinglePage: true }}
            />
          </Flex>
        </Flex>
      ) : null}
    </Drawer>
  );
}
