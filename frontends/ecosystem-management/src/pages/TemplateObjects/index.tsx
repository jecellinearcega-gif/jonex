import React, { useCallback, useMemo } from 'react';
import { Card, Table, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { colors, radius } from '@jonex/platform-theme/tokens';
import { CommonTable, PageContent, PageHeader } from '@jonex/shared-lib';
import type { FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import type { TemplateObject } from '../../data/mock';

const objectDefs = [
  {
    id: 'disease',
    domainId: 'healthcare',
    fields: [
      { id: 'diseaseName', type: 'string', required: true },
      { id: 'icdCode', type: 'string', required: true },
      { id: 'symptomDescription', type: 'text', required: false },
    ],
    status: 'active' as const,
  },
  {
    id: 'drug',
    domainId: 'healthcare',
    fields: [
      { id: 'drugName', type: 'string', required: true },
      { id: 'approvalNumber', type: 'string', required: true },
      { id: 'manufacturer', type: 'string', required: false },
    ],
    status: 'active' as const,
  },
  {
    id: 'company',
    domainId: 'fintech',
    fields: [
      { id: 'companyName', type: 'string', required: true },
      { id: 'creditCode', type: 'string', required: true },
      { id: 'industry', type: 'string', required: false },
    ],
    status: 'active' as const,
  },
];

/** 必须是 type 别名而不是 interface：CommonTable 的 F 要求隐式索引签名 */
type ObjectFilters = {
  keyword?: string;
  domain_id?: string;
};

export default function TemplateObjects() {
  const { t } = useTranslation();

  const objects = useMemo<TemplateObject[]>(
    () =>
      objectDefs.map((object) => ({
        id: `to-${object.id}`,
        name: t(`templateObjects.demo.objects.${object.id}`),
        domainId: object.domainId,
        domainName: t(`templateObjects.demo.domains.${object.domainId}`),
        fields: object.fields.map((field) => ({
          name: t(`templateObjects.demo.fields.${field.id}`),
          type: field.type,
          required: field.required,
        })),
        status: object.status,
      })),
    [t],
  );

  const domains = useMemo(
    () => [...new Map(objects.map((object) => [object.domainId, object.domainName])).entries()],
    [objects],
  );

  /** 数据是本地 mock，但过滤与切片仍收进 fetch —— CommonTable 只认这一个数据出口 */
  const fetchRows = useCallback(
    async ({ page, pageSize, keyword, domain_id }: TableQueryParams<ObjectFilters>) => {
      const q = (keyword ?? '').trim();
      const rows = objects.filter((o) => {
        if (q && !o.name.includes(q)) return false;
        if (domain_id && o.domainId !== domain_id) return false;
        return true;
      });
      const start = (page - 1) * pageSize;
      return { list: rows.slice(start, start + pageSize), total: rows.length };
    },
    [objects],
  );

  const filters = useMemo<FilterConfig[]>(
    () => [
      {
        key: 'keyword',
        label: t('common.search'),
        type: 'input',
        placeholder: t('templateObjects.searchPlaceholder'),
      },
      {
        key: 'domain_id',
        label: t('templateObjects.columnDomain'),
        type: 'select',
        placeholder: t('templateObjects.domainFilterPlaceholder'),
        options: domains.map(([value, label]) => ({ label, value })),
      },
    ],
    [t, domains],
  );

  const statusLabelMap: Record<string, string> = {
    active: t('status.active'),
    draft: t('status.draft'),
  };
  const statusClsMap: Record<string, string> = {
    active: 'active',
    draft: 'pending',
  };

  const columns = useMemo(
    () => [
      { title: t('templateObjects.columnName'), dataIndex: 'name', key: 'name', width: 140 },
      {
        title: t('templateObjects.columnDomain'),
        dataIndex: 'domainName',
        key: 'domainName',
        width: 120,
        render: (v: string) => <Tag>{v}</Tag>,
      },
      {
        title: t('templateObjects.columnFieldCount'),
        key: 'fieldCount',
        width: 80,
        align: 'center' as const,
        render: (_: unknown, record: TemplateObject) => record.fields.length,
      },
      {
        title: t('templateObjects.columnStatus'),
        dataIndex: 'status',
        key: 'status',
        width: 80,
        render: (v: string) => (
          <span className={`yx-status-badge ${statusClsMap[v] || v}`}>{statusLabelMap[v] || v}</span>
        ),
      },
    ],
    // statusLabelMap / statusClsMap 都派生自 t，随 t 变化
    [t],
  );

  return (
    <>
      <PageHeader title={t('templateObjects.pageTitle')} style={{ marginBottom: 4 }} />
      <p style={{ color: colors.textMuted, margin: '0 0 16px', fontSize: 14 }}>
        {t('templateObjects.pageSubtitle')}
      </p>

      <PageContent showCard={false}>
        <Card style={{ borderRadius: radius.card }}>
          <CommonTable<TemplateObject, ObjectFilters>
            rowKey="id"
            filters={filters}
            columns={columns}
            fetch={fetchRows}
            expandable={{
              expandedRowRender: (record: TemplateObject) => (
                <div style={{ padding: '8px 0' }}>
                  <h4 style={{ marginBottom: 8, fontSize: 13, color: colors.brandDark }}>
                    {t('templateObjects.fieldDefSection')}
                  </h4>
                  {/* 展开行内嵌表不是列表级：保持默认 antd Table，不迁 CommonTable、不传 size */}
                  <Table
                    dataSource={record.fields.map((f, i) => ({ ...f, key: i }))}
                    columns={[
                      { title: t('templateObjects.fieldName'), dataIndex: 'name', key: 'name' },
                      { title: t('templateObjects.fieldType'), dataIndex: 'type', key: 'type', width: 100 },
                      {
                        title: t('templateObjects.fieldRequired'),
                        dataIndex: 'required',
                        key: 'required',
                        width: 80,
                        render: (v: boolean) => (
                          <Tag color={v ? 'red' : 'default'}>
                            {v ? t('templateObjects.required') : t('templateObjects.optional')}
                          </Tag>
                        ),
                      },
                    ]}
                    rowKey="key"
                    pagination={false}
                  />
                </div>
              ),
              rowExpandable: () => true,
            }}
          />
        </Card>
      </PageContent>
    </>
  );
}
