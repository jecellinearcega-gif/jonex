import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, Result, Space, Tag, Tooltip } from 'antd';
import type { TableColumnsType } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { CommonTable, formatDateTime } from '@jonex/shared-lib';
import type { FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import { listMcpServices } from '@/api/mcpServices';
import { listSpaces } from '@/api/spaces';
import type { McpServiceItem } from '@/api/mcpServices';
import McpServiceDetailDrawer, { type McpServiceDetailDrawerHandle } from './McpServiceDetailDrawer';
import './index.css';

interface McpServicesTabProps {
  /** 点击「前往 MCP Key 管理」时切换到服务访问Key 视图 */
  onGoToKeys?: () => void;
}

/** 必须是 type 别名而不是 interface：CommonTable 的 F 要求隐式索引签名 */
type ServiceFilters = {
  keyword?: string;
  space_id?: string;
};

/** MCP 服务目录 — 服务列表 Tab */
export default function McpServicesTab({ onGoToKeys }: McpServicesTabProps) {
  const { t } = useTranslation();
  const [error, setError] = useState<string | null>(null);
  /** 接口 HTTP status（403=权限不足时错误态换用权限图标，而非报错图标） */
  const [errorStatus, setErrorStatus] = useState<number | undefined>(undefined);
  /** 接口返回的全量条数：区分「本来就没有服务」与「筛选没命中」两种空态 */
  const [totalCount, setTotalCount] = useState<number | null>(null);
  // 领域空间筛选选项（来源 GET /knowledge-base/spaces 接口，而非列表数据聚合）
  const [spaceOptions, setSpaceOptions] = useState<{ label: string; value: string }[]>([]);

  const detailDrawerRef = useRef<McpServiceDetailDrawerHandle>(null);

  /**
   * 取数 / 过滤 / 切片全在这里 —— CommonTable 只认这一个数据出口。
   * ⚠️ 关键词与空间都从参数取，不要读页面 state：fetch 存在 ref 里，闭包不参与重渲染。
   */
  const fetchRows = useCallback(
    async ({ page, pageSize, keyword, space_id }: TableQueryParams<ServiceFilters>) => {
      const q = (keyword ?? '').trim().toLocaleLowerCase();
      try {
        const result = await listMcpServices();
        const all = result.items || [];
        setTotalCount(all.length);
        setError(null);
        setErrorStatus(undefined);
        const rows = all.filter((item) => {
          if (
            q &&
            !`${item.name || ''} ${item.tool || ''} ${item.space_name || ''}`.toLocaleLowerCase().includes(q)
          ) {
            return false;
          }
          if (space_id && item.space_id !== space_id) return false;
          return true;
        });
        const start = (page - 1) * pageSize;
        return { list: rows.slice(start, start + pageSize), total: rows.length };
      } catch (err: unknown) {
        // 接口报错：由下面的 Result 分支渲染错误态 + 重试按钮
        setError(err instanceof Error ? err.message : t('mcpServiceDirectory.loadFailed'));
        setErrorStatus(
          typeof (err as { status?: number } | null)?.status === 'number'
            ? (err as { status?: number }).status
            : undefined,
        );
        throw err;
      }
    },
    [t],
  );

  const filters = useMemo<FilterConfig[]>(
    () => [
      {
        key: 'keyword',
        label: t('common.keyword'),
        type: 'input',
        placeholder: t('mcpServiceDirectory.searchServicePlaceholder'),
      },
      {
        key: 'space_id',
        label: t('mcpServiceDirectory.colSpace'),
        type: 'select',
        placeholder: t('mcpServiceDirectory.allSpaces'),
        options: spaceOptions,
      },
    ],
    [t, spaceOptions],
  );

  /** 领域空间筛选选项（来源接口 GET /knowledge-base/spaces，失败静默为空） */
  useEffect(() => {
    listSpaces()
      .then((list) => setSpaceOptions(list.map((s) => ({ label: s.name ?? s.id, value: s.id }))))
      .catch(() => {
        // 失败静默，下拉仅剩「全部领域空间」
      });
  }, []);

  const columns: TableColumnsType<McpServiceItem> = [
    {
      title: t('mcpServiceDirectory.colName'),
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record: McpServiceItem) => (
        <Space size={6}>
          <Button
            type="link"
            style={{ padding: 0, height: 'auto', color: '#1C6FEA', fontWeight: 600 }}
            onClick={() => detailDrawerRef.current?.open(record)}
          >
            {name}
          </Button>
        </Space>
      ),
    },
    {
      title: t('mcpServiceDirectory.colCapabilityType'),
      key: 'capability_type',
      width: 100,
      render: (_: unknown, record: McpServiceItem) =>
        record.service_type === 'system' ? (
          <Tag color="blue">{t('mcpServiceDirectory.capabilityWrite')}</Tag>
        ) : (
          <Tag>{t('mcpServiceDirectory.capabilityDomain')}</Tag>
        ),
    },
    {
      title: t('mcpServiceDirectory.colToolName'),
      key: 'tool_name',
      render: (_: unknown, record: McpServiceItem) =>
        record.tool ? <span>{record.tool}</span> : <span style={{ color: '#94a3b8' }}>-</span>,
    },
    {
      title: t('mcpServiceDirectory.colSource'),
      key: 'source',
      width: 120,
      render: (_: unknown, record: McpServiceItem) =>
        record.service_type === 'system' ? (
          <span>{t('mcpServiceDirectory.sourceBuiltin')}</span>
        ) : (
          record.space_name || '-'
        ),
    },
    {
      title: t('mcpServiceDirectory.colResourceScope'),
      key: 'resource_scope',
      render: (_: unknown, record: McpServiceItem) => {
        // 知识写入（system）无预授权知识库，资源范围由 Key 授权决定
        if (record.service_type === 'system') {
          return <span style={{ color: '#94a3b8' }}>{t('mcpServiceDirectory.scopeByKey')}</span>;
        }
        const names = record.kb_names || [];
        if (names.length === 0) return <span style={{ color: '#94a3b8' }}>-</span>;
        const shown = names.slice(0, 3);
        const rest = names.slice(3);
        return (
          <Space size={8} wrap>
            {shown.map((n) => (
              <Tag key={n}>{n}</Tag>
            ))}
            {rest.length > 0 && (
              <Tooltip title={rest.join('、')}>
                <Tag>+{rest.length}</Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
    },
    {
      title: t('mcpServiceDirectory.colStatus'),
      dataIndex: 'is_published',
      key: 'is_published',
      render: (v: boolean, record: McpServiceItem) =>
        v ? (
          <Space size={4} wrap>
            <Tag color="success">{t('mcpServiceDirectory.statusPublished')}</Tag>
            {record.enabled === 0 && (
              <Tooltip title={t('mcpServiceDirectory.statusUnavailableTip')}>
                <Tag color="orange">{t('mcpServiceDirectory.statusUnavailable')}</Tag>
              </Tooltip>
            )}
          </Space>
        ) : (
          <Tag>{t('mcpServiceDirectory.statusUnpublished')}</Tag>
        ),
    },
    {
      title: t('mcpServiceDirectory.colLastCall'),
      dataIndex: 'last_call_at',
      key: 'last_call_at',
      width: 150,
      render: (v: string | null) =>
        v ? formatDateTime(v) : <span style={{ color: '#94a3b8' }}>{t('mcpServiceDirectory.neverCalled')}</span>,
    },
    {
      title: t('mcpServiceDirectory.colActions'),
      key: 'actions',
      width: 90,
      render: (_: unknown, record: McpServiceItem) => (
        <Button type="link" size="small" onClick={() => detailDrawerRef.current?.open(record)}>
          {t('mcpServiceDirectory.viewDetail')}
        </Button>
      ),
    },
  ];

  // ── 错误态覆盖：接口失败时渲染 Result + 重试按钮 ──
  // 权限不足(403)时使用 antd 内置 403 权限图标，而非报错图标
  if (error) {
    const forbidden = errorStatus === 403;
    return (
      <Result
        status={forbidden ? '403' : 'error'}
        title={forbidden ? t('mcpServiceDirectory.forbiddenTitle') : t('mcpServiceDirectory.loadFailed')}
        subTitle={error}
        extra={
          // ⚠️ 这里不能调 tableRef.current.refresh()：错误态下 CommonTable 已被卸载、ref 为 null。
          // 清掉 error 让它重新挂载，挂载时取数 effect 会自己跑一次。
          <Button type="primary" icon={<ReloadOutlined />} onClick={() => setError(null)}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div>
      <CommonTable<McpServiceItem, ServiceFilters>
        rowKey="id"
        filters={filters}
        columns={columns}
        fetch={fetchRows}
        scroll={{ x: 'max-content' }}
        emptyText={
          totalCount === 0 ? (
            <Empty description={t('mcpServiceDirectory.emptyText')} />
          ) : (
            <Empty description={t('mcpServiceDirectory.emptySearchText')} />
          )
        }
      />

      <McpServiceDetailDrawer ref={detailDrawerRef} onGoToKeys={onGoToKeys} />
    </div>
  );
}
