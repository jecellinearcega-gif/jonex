import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Result, Table } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  getKnowledgeKpis,
  getKnowledgeOverview,
  getKnowledgeTrend,
  listHotInstances,
  listIdleKnowledge,
} from '@/api/dataOperations';
import useCurrentSpace from '@/hooks/useCurrentSpace';
import useAsyncData from '@/hooks/useAsyncData';
import SectionCard from './components/SectionCard';
import StatCards, { type StatCardItem } from './components/StatCards';
import TrendAreaChart from './components/TrendAreaChart';
import ProportionList from './components/ProportionList';
import SegmentedFilter from './components/SegmentedFilter';
import type { DoHotInstance, DoIdleKnowledge, DoKnowledgeKpi, DoScope } from './types';
import './index.scss';

type KnowledgeOverview = Awaited<ReturnType<typeof getKnowledgeOverview>>;
type IdleWindow = 30 | 90;
type TrendRange = 7 | 14;

export default function KnowledgeAssets() {
  const { t } = useTranslation();
  // 切换领域空间 → 知识侧 KPI、趋势、分布、活跃度全部按成员库重算
  const spaceId = useCurrentSpace();

  const [scope, setScope] = useState<DoScope>('ontology');
  const [trendRange, setTrendRange] = useState<TrendRange>(7);
  const [idleWindow, setIdleWindow] = useState<IdleWindow>(30);

  const [kpis, setKpis] = useState<DoKnowledgeKpi[]>([]);
  const [kpiLoading, setKpiLoading] = useState(true);
  const [overview, setOverview] = useState<KnowledgeOverview | null>(null);
  const [scopeLoading, setScopeLoading] = useState(true);
  const [hotInstances, setHotInstances] = useState<DoHotInstance[]>([]);
  const [idleKnowledge, setIdleKnowledge] = useState<DoIdleKnowledge[]>([]);
  const [idleLoading, setIdleLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadKpis = useCallback(async () => {
    setKpiLoading(true);
    setError(null);
    try {
      setKpis(await getKnowledgeKpis(spaceId));
    } catch (e) {
      setError(e instanceof Error ? e.message : t('dataOperations.error.loadFailed'));
    } finally {
      setKpiLoading(false);
    }
  }, [t, spaceId]);

  // 分析对象切换只影响下方三块，KPI 行不参与，避免无谓的闪烁
  const loadByScope = useCallback(async () => {
    setScopeLoading(true);
    try {
      const [ov, hot] = await Promise.all([getKnowledgeOverview(scope, spaceId), listHotInstances(scope, spaceId)]);
      setOverview(ov);
      setHotInstances(hot);
    } catch {
      setOverview(null);
      setHotInstances([]);
    } finally {
      setScopeLoading(false);
    }
  }, [scope, spaceId]);

  useEffect(() => {
    void loadKpis();
  }, [loadKpis]);

  useEffect(() => {
    void loadByScope();
  }, [loadByScope]);

  useEffect(() => {
    let cancelled = false;
    setIdleLoading(true);
    listIdleKnowledge(scope, idleWindow, spaceId)
      .then((list) => {
        if (!cancelled) setIdleKnowledge(list);
      })
      .catch(() => {
        if (!cancelled) setIdleKnowledge([]);
      })
      .finally(() => {
        if (!cancelled) setIdleLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scope, idleWindow, spaceId]);

  const kpiItems: StatCardItem[] = useMemo(
    () =>
      kpis.map((kpi) => ({
        key: kpi.key,
        label: t(kpi.labelKey),
        value: kpi.value,
        delta: (
          <>
            {t('dataOperations.kpi.newIn7d')} {kpi.deltaText}
          </>
        ),
        accent: kpi.accent,
        iconKey: kpi.iconKey,
      })),
    [kpis, t],
  );

  // 增长趋势的窗口是卡片自己的事：切 7/14 天只重取这一块，KPI、分布、活跃度都还按近 7 天
  const loadTrend = useCallback(() => getKnowledgeTrend(scope, spaceId, trendRange), [scope, spaceId, trendRange]);
  const { data: trend, loading: trendLoading } = useAsyncData(loadTrend);

  const hotColumns = [
    {
      title: t('dataOperations.column.instanceName'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => <strong>{v}</strong>,
    },
    { title: t('dataOperations.column.objectType'), dataIndex: 'objectType', key: 'objectType', width: 150 },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName', width: 180 },
    {
      title: t('dataOperations.column.hitCount'),
      dataIndex: 'hitCount',
      key: 'hitCount',
      width: 110,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{v.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.column.adoptCount'),
      dataIndex: 'adoptCount',
      key: 'adoptCount',
      width: 110,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{v.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.column.lastUsedAt'),
      dataIndex: 'lastUsedAt',
      key: 'lastUsedAt',
      width: 170,
    },
  ];

  const idleColumns = [
    {
      title: t('dataOperations.column.instanceName'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => <strong>{v}</strong>,
    },
    { title: t('dataOperations.column.objectType'), dataIndex: 'objectType', key: 'objectType', width: 150 },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName', width: 180 },
    {
      title: t('dataOperations.column.lastUsedAt'),
      dataIndex: 'lastUsedAt',
      key: 'lastUsedAt',
      width: 170,
      render: (v: string | null) => v ?? <span className="yx-do-muted">{t('dataOperations.common.neverUsed')}</span>,
    },
    {
      title: t('dataOperations.column.updatedAt'),
      dataIndex: 'updatedAt',
      key: 'updatedAt',
      width: 170,
    },
  ];

  if (error) {
    return (
      <Result
        status="error"
        title={t('dataOperations.error.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={() => void loadKpis()}>
            {t('dataOperations.common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <StatCards items={kpiItems} loading={kpiLoading} />

      <div className="yx-card yx-do-scope-bar">
        <div className="yx-do-scope-left">
          <span className="yx-do-scope-label">{t('dataOperations.scope.label')}</span>
          <SegmentedFilter
            size="middle"
            value={scope}
            onChange={setScope}
            options={[
              { value: 'ontology', label: t('dataOperations.scope.ontology') },
              { value: 'wiki', label: t('dataOperations.scope.wiki') },
            ]}
          />
        </div>
        <div className="yx-do-scope-metrics">
          <span className="yx-do-scope-metric">
            {t('dataOperations.scope.activeInstances')}
            <em>{(overview?.activity.activeCount ?? 0).toLocaleString()}</em>
          </span>
          <span className="yx-do-scope-metric">
            {t('dataOperations.scope.activityRate')}
            <em>{(overview?.activity.activeRate ?? 0).toFixed(2)}%</em>
          </span>
        </div>
      </div>

      <div className="yx-do-two-col">
        <SectionCard
          title={t('dataOperations.section.growthTitle')}
          extra={
            <SegmentedFilter
              value={trendRange}
              onChange={setTrendRange}
              options={[
                { value: 7, label: t('dataOperations.filter.last7d') },
                { value: 14, label: t('dataOperations.filter.last14d') },
              ]}
            />
          }
        >
          <TrendAreaChart
            data={trend ?? []}
            loading={trendLoading}
            color="#8b5cf6"
            valueName={t('dataOperations.column.count')}
          />
        </SectionCard>

        <SectionCard
          title={scope === 'wiki' ? t('dataOperations.section.distWikiTitle') : t('dataOperations.section.distTitle')}
        >
          <ProportionList items={overview?.dist ?? []} loading={scopeLoading} />
        </SectionCard>
      </div>

      <SectionCard
        title={
          scope === 'wiki'
            ? t('dataOperations.section.hotWikiInstancesTitle')
            : t('dataOperations.section.hotInstancesTitle')
        }
        hint={t('dataOperations.section.hotInstancesHint')}
      >
        <Table
          rowKey="id"
          size="small"
          columns={hotColumns}
          dataSource={hotInstances}
          loading={scopeLoading}
          pagination={false}
        />
      </SectionCard>

      <SectionCard
        title={t('dataOperations.section.idleKnowledgeTitle')}
        extra={
          <SegmentedFilter
            value={idleWindow}
            onChange={setIdleWindow}
            options={[
              { value: 30, label: t('dataOperations.filter.idle30') },
              { value: 90, label: t('dataOperations.filter.idle90') },
            ]}
          />
        }
      >
        <Table
          rowKey="id"
          size="small"
          columns={idleColumns}
          dataSource={idleKnowledge}
          loading={idleLoading}
          pagination={false}
        />
      </SectionCard>
    </>
  );
}
