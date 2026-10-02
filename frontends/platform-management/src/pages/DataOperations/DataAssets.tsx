import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Result, Table } from 'antd';
import { useTranslation } from 'react-i18next';
import { navigateCrossApp } from '@jonex/shell-sdk';
import {
  getAbnormalAssets,
  getAssetComposition,
  getAssetKpis,
  getAssetTrend,
  getPipeline,
  listHotAssets,
  listIdleAssets,
  listKbOptions,
} from '@/api/dataOperations';
import useAsyncData from '@/hooks/useAsyncData';
import useCurrentSpace from '@/hooks/useCurrentSpace';
import SectionCard from './components/SectionCard';
import StatCards, { type StatCardItem } from './components/StatCards';
import TrendAreaChart from './components/TrendAreaChart';
import ProportionList from './components/ProportionList';
import SegmentedFilter from './components/SegmentedFilter';
import AssetTypeTag from './components/AssetTypeTag';
import KbSelect from './components/KbSelect';
import PipelineOverview from './components/PipelineOverview';
import AnomalySummary from './components/AnomalySummary';
import { DO_KB_ALL } from './constant';
import type {
  DoAnomalyAsset,
  DoAnomalySummary,
  DoAssetType,
  DoHotAsset,
  DoIdleAsset,
  DoKbOption,
  DoKpiCard,
  DoPipelineOverview,
  DoPipelineStats,
} from './types';
import './index.scss';

type IdleWindow = 30 | 90;
type TrendRange = 7 | 14;

export default function DataAssets() {
  const { t } = useTranslation();
  // 切换领域空间 → 整页按新空间的成员库重新取数
  const spaceId = useCurrentSpace();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kpis, setKpis] = useState<DoKpiCard[]>([]);
  const [kbOptions, setKbOptions] = useState<DoKbOption[]>([]);
  const [hotAssets, setHotAssets] = useState<DoHotAsset[]>([]);
  const [pipeline, setPipeline] = useState<{ data: DoPipelineOverview; stats: DoPipelineStats } | null>(null);
  const [abnormal, setAbnormal] = useState<{ summary: DoAnomalySummary; latest: DoAnomalyAsset | null } | null>(null);

  const [trendRange, setTrendRange] = useState<TrendRange>(7);

  const [idleWindow, setIdleWindow] = useState<IdleWindow>(30);

  // 趋势与构成各有一个知识库筛选，切其中一张不牵动同排的另一张
  const [trendKb, setTrendKb] = useState(DO_KB_ALL);
  const [compKb, setCompKb] = useState(DO_KB_ALL);

  // 切空间后原选中库可能已不在选项内，派生回「全部知识库」。
  // 不用 effect 去改 state：那样会多一帧脏值、多触发一次取数
  const effectiveTrendKb = kbOptions.some((kb) => kb.id === trendKb) ? trendKb : DO_KB_ALL;
  const effectiveCompKb = kbOptions.some((kb) => kb.id === compKb) ? compKb : DO_KB_ALL;

  const loadMain = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 这五块同属 data-assets，取数层按 key 合并成一次请求，不是五个 HTTP 调用
      const [kpiList, options, hot, pipelineData, abnormalData] = await Promise.all([
        getAssetKpis(spaceId),
        listKbOptions(spaceId),
        listHotAssets(spaceId),
        getPipeline(spaceId),
        getAbnormalAssets(spaceId),
      ]);
      setKpis(kpiList);
      setKbOptions(options);
      setHotAssets(hot);
      setPipeline(pipelineData);
      setAbnormal(abnormalData);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('dataOperations.error.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t, spaceId]);

  useEffect(() => {
    void loadMain();
  }, [loadMain]);

  const loadTrend = useCallback(
    () => getAssetTrend(effectiveTrendKb, spaceId, trendRange),
    [effectiveTrendKb, spaceId, trendRange],
  );
  const loadComposition = useCallback(() => getAssetComposition(effectiveCompKb, spaceId), [effectiveCompKb, spaceId]);
  const loadIdle = useCallback(() => listIdleAssets(idleWindow, spaceId), [idleWindow, spaceId]);

  // 这三块与首屏分开取数：切筛选器时只让所在卡片回到骨架态，不牵动整页
  const { data: trend, loading: trendLoading } = useAsyncData(loadTrend);
  const { data: composition, loading: compLoading } = useAsyncData(loadComposition);
  const { data: idleAssets, loading: idleLoading } = useAsyncData(loadIdle);

  const kpiItems: StatCardItem[] = useMemo(
    () =>
      kpis.map((kpi) => ({
        key: kpi.key,
        label: t(kpi.labelKey),
        value: kpi.value,
        accent: kpi.accent,
        iconKey: kpi.iconKey,
      })),
    [kpis, t],
  );

  // 按来源的构成契约里没有对应字段，「类型 / 来源」分段器与 compositionBySource 一并下线
  const compositionData = composition?.composition ?? [];

  const typeLabel = useCallback(
    (type: DoAssetType) => <AssetTypeTag type={type} label={t(`dataOperations.assetType.${type}`)} />,
    [t],
  );

  // 目标页归属 core-business，是另一个子应用：ShellContext.navigate 只会拼本应用的 basePath，
  // 改走 shell-sdk 的跨应用导航 —— hosted 下由 shell 的 SPA 路由切过去（文档不重载），
  // standalone / iframe fallback 下没人接管，SDK 内部回落到顶层窗口整页导航
  const goToKb = useCallback((kbId: string) => {
    navigateCrossApp(`/apps/core-business/domain-knowledge/${kbId}`);
  }, []);

  // 两张表共用同一种「前往知识库」：没有 kbId 就没有跳转目标（文档已删 / 跨租户，
  // 后端富化不到），置灰而不是点进空白页
  const renderKbAction = useCallback(
    (kbId: string | null) =>
      kbId ? (
        <a className="yx-do-link" onClick={() => goToKb(kbId)}>
          {t('dataOperations.common.goToKb')}
        </a>
      ) : (
        <span className="yx-do-link yx-do-link--disabled">{t('dataOperations.common.goToKb')}</span>
      ),
    [goToKb, t],
  );

  const hotColumns = [
    {
      title: t('dataOperations.column.assetName'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => <strong>{v}</strong>,
    },
    {
      title: t('dataOperations.column.type'),
      dataIndex: 'assetType',
      key: 'assetType',
      width: 110,
      render: typeLabel,
    },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName' },
    {
      title: t('dataOperations.column.refCount'),
      dataIndex: 'refCount',
      key: 'refCount',
      width: 110,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{v.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.column.lastRefAt'),
      dataIndex: 'lastRefAt',
      key: 'lastRefAt',
      width: 170,
    },
    {
      title: t('dataOperations.column.action'),
      key: 'action',
      width: 120,
      render: (_: unknown, row: DoHotAsset) => renderKbAction(row.kbId),
    },
  ];

  const idleColumns = [
    {
      title: t('dataOperations.column.assetName'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => <strong>{v}</strong>,
    },
    { title: t('dataOperations.column.type'), dataIndex: 'assetType', key: 'assetType', width: 110, render: typeLabel },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName' },
    {
      title: t('dataOperations.column.lastRefAt'),
      dataIndex: 'lastRefAt',
      key: 'lastRefAt',
      width: 170,
      render: (v: string | null) =>
        v ?? <span className="yx-do-muted">{t('dataOperations.common.neverReferenced')}</span>,
    },
    { title: t('dataOperations.column.updatedAt'), dataIndex: 'updatedAt', key: 'updatedAt', width: 170 },
    {
      title: t('dataOperations.column.action'),
      key: 'action',
      width: 120,
      render: (_: unknown, row: DoIdleAsset) => renderKbAction(row.kbId),
    },
  ];

  if (error) {
    return (
      <Result
        status="error"
        title={t('dataOperations.error.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={() => void loadMain()}>
            {t('dataOperations.common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <StatCards items={kpiItems} loading={loading} />

      <div className="yx-do-two-col yx-do-two-col--wide-right">
        <SectionCard
          title={t('dataOperations.section.trendTitle')}
          extra={
            <>
              <KbSelect value={effectiveTrendKb} options={kbOptions} onChange={setTrendKb} loading={loading} />
              <SegmentedFilter
                value={trendRange}
                onChange={setTrendRange}
                options={[
                  { value: 7, label: t('dataOperations.filter.last7d') },
                  { value: 14, label: t('dataOperations.filter.last14d') },
                ]}
              />
            </>
          }
        >
          {/* fill：本排两卡等高，右卡更高，不撑满的话趋势卡底部会空出一截 */}
          <TrendAreaChart
            data={trend ?? []}
            loading={trendLoading}
            valueName={t('dataOperations.column.ingested')}
            fill
          />
        </SectionCard>

        <SectionCard
          title={t('dataOperations.section.compositionTitle')}
          extra={<KbSelect value={effectiveCompKb} options={kbOptions} onChange={setCompKb} loading={loading} />}
        >
          <ProportionList items={compositionData} loading={compLoading} />
        </SectionCard>
      </div>

      <SectionCard title={t('dataOperations.section.hotAssetsTitle')} hint={t('dataOperations.section.hotAssetsHint')}>
        <Table
          rowKey="id"
          size="small"
          columns={hotColumns}
          dataSource={hotAssets}
          loading={loading}
          pagination={false}
        />
      </SectionCard>

      <SectionCard
        title={t('dataOperations.section.idleAssetsTitle')}
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
          dataSource={idleAssets ?? []}
          loading={idleLoading}
          pagination={false}
        />
      </SectionCard>

      <div className="yx-do-two-col-even">
        <SectionCard
          title={t('dataOperations.section.pipelineTitle')}
          description={t('dataOperations.section.pipelineDesc')}
        >
          <PipelineOverview data={pipeline?.data ?? null} stats={pipeline?.stats ?? null} loading={loading} />
        </SectionCard>

        <SectionCard title={t('dataOperations.section.anomalyTitle')}>
          <AnomalySummary summary={abnormal?.summary ?? null} latest={abnormal?.latest ?? null} loading={loading} />
        </SectionCard>
      </div>
    </>
  );
}
