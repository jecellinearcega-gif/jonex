import React, { useCallback, useMemo } from 'react';
import { Card, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { colors, radius } from '@jonex/platform-theme/tokens';
import { CommonTable, PageContent, PageHeader } from '@jonex/shared-lib';
import type { FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import type { TemplateRelation } from '../../data/mock';

const relationDefs = [
  { id: 'treats', source: 'drug', target: 'disease', relationType: '1:N' },
  { id: 'contains', source: 'drug', target: 'compound', relationType: 'N:M' },
  { id: 'controls', source: 'company', target: 'company', relationType: '1:N' },
];

/** 必须是 type 别名而不是 interface：CommonTable 的 F 要求隐式索引签名 */
type RelationFilters = {
  keyword?: string;
  relation_type?: string;
};

export default function TemplateRelations() {
  const { t } = useTranslation();

  const relations = useMemo<TemplateRelation[]>(
    () =>
      relationDefs.map((relation) => ({
        id: `tr-${relation.id}`,
        name: t(`templateRelations.demo.relations.${relation.id}.name`),
        sourceObject: t(`templateRelations.demo.objects.${relation.source}`),
        targetObject: t(`templateRelations.demo.objects.${relation.target}`),
        relationType: relation.relationType,
        constraints: t(`templateRelations.demo.relations.${relation.id}.constraints`),
      })),
    [t],
  );

  const relationTypes = useMemo(() => [...new Set(relations.map((r) => r.relationType))], [relations]);

  /** 数据是本地 mock，但过滤与切片仍收进 fetch —— CommonTable 只认这一个数据出口 */
  const fetchRows = useCallback(
    async ({ page, pageSize, keyword, relation_type }: TableQueryParams<RelationFilters>) => {
      const q = (keyword ?? '').trim();
      const rows = relations.filter((r) => {
        if (q && !r.name.includes(q) && !r.sourceObject.includes(q) && !r.targetObject.includes(q)) return false;
        if (relation_type && r.relationType !== relation_type) return false;
        return true;
      });
      const start = (page - 1) * pageSize;
      return { list: rows.slice(start, start + pageSize), total: rows.length };
    },
    [relations],
  );

  const filters = useMemo<FilterConfig[]>(
    () => [
      {
        key: 'keyword',
        label: t('common.search'),
        type: 'input',
        placeholder: t('templateRelations.searchPlaceholder'),
      },
      {
        key: 'relation_type',
        label: t('templateRelations.columnType'),
        type: 'select',
        placeholder: t('templateRelations.typeFilterPlaceholder'),
        options: relationTypes.map((v) => ({ label: v, value: v })),
      },
    ],
    [t, relationTypes],
  );

  const columns = useMemo(
    () => [
      { title: t('templateRelations.columnName'), dataIndex: 'name', key: 'name', width: 140 },
      {
        title: t('templateRelations.columnSource'),
        dataIndex: 'sourceObject',
        key: 'sourceObject',
        width: 120,
        render: (v: string) => <Tag color="blue">{v}</Tag>,
      },
      {
        title: t('templateRelations.columnTarget'),
        dataIndex: 'targetObject',
        key: 'targetObject',
        width: 120,
        render: (v: string) => <Tag color="green">{v}</Tag>,
      },
      {
        title: t('templateRelations.columnType'),
        dataIndex: 'relationType',
        key: 'relationType',
        width: 90,
        align: 'center' as const,
      },
      {
        title: t('templateRelations.columnConstraints'),
        dataIndex: 'constraints',
        key: 'constraints',
        ellipsis: true,
      },
    ],
    [t],
  );

  return (
    <>
      <PageHeader title={t('templateRelations.pageTitle')} style={{ marginBottom: 4 }} />
      <p style={{ color: colors.textMuted, margin: '0 0 16px', fontSize: 14 }}>
        {t('templateRelations.pageSubtitle')}
      </p>

      <PageContent showCard={false}>
        <Card style={{ borderRadius: radius.card }}>
          <CommonTable<TemplateRelation, RelationFilters>
            rowKey="id"
            filters={filters}
            columns={columns}
            fetch={fetchRows}
          />
        </Card>
      </PageContent>
    </>
  );
}
