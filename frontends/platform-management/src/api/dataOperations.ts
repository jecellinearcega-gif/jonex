/**
 * 数据运营模块的取数入口。
 *
 * 已接后端 3 个聚合端点（均为 GET /api/v1/knowledge-base/data-operations/*）：
 * data-assets / knowledge-assets / search-analytics。后端按「页」聚合，本层按「块」暴露。
 * 后端另有 overview 与 abnormal-assets（异常下钻分页）两个端点未接 —— 前者是全页 KPI 的
 * 合并版（各页已各取各的），后者是异常抽屉的分页列表（抽屉还没做）。
 *
 * 后端仍未实现的只剩两处（均为三期，后端侧自己写死）：提问主题聚类（`question_topics` 硬编码空数组）
 * 与 Wiki 侧指标（恒零值）。其余块都能取到真实数据 —— 取不到时各自在函数上就地标注。
 *
 * ── 领域空间 ──
 * 后端按 `knowledge_info.space_id` 圈定统计范围，前端只不过把这个空间作为 `domain_space_id` 下发
 * （不带该参数即租户级全量）。知识库筛选下拉（见 listKbOptions）也按同一个字段取，与图表同口径。
 */
import {
  DO_ASSET_COMPOSITION,
  DO_ASSET_KPIS,
  DO_KB_ALL,
  DO_KNOWLEDGE_KPIS,
  DO_SEARCH_KPIS,
  DO_SEARCH_MODES,
} from '../pages/DataOperations/constant';
import type {
  DoAnomalyAsset,
  DoAnomalySummary,
  DoAssetType,
  DoHotAsset,
  DoHotInstance,
  DoHotQuestion,
  DoIdleAsset,
  DoIdleKnowledge,
  DoKbOption,
  DoKnowledgeKpi,
  DoKpiCard,
  DoPipelineOverview,
  DoPipelineStats,
  DoProportionItem,
  DoScope,
  DoScopeActivity,
  DoSearchMode,
  DoStage,
  DoTrendPoint,
  DoUsageItem,
} from '../pages/DataOperations/types';
import { fmtCount, fmtRate, fmtSigned } from '../pages/DataOperations/utils';
import { formatDateTime } from '@jonex/shared-lib';
import apiClient from './request';

/* ══════════════════════ 后端契约 ══════════════════════ */

/** 后端与前端对「趋势点」的叫法不同：count 是原始计数，value 是前端图表的字段名 */
interface ApiTrendPoint {
  date: string;
  count: number;
}

interface ApiCompositionItem {
  category: string;
  /** 后端已按前端口径映射好的类型（表格 spreadsheet → table），7 类之外照直输出 other */
  asset_type: string;
  count: number;
}

interface ApiCitedAsset {
  asset_id: string;
  file_name: string | null;
  mime_type: string | null;
  cite_count: number;
  kb_id: string | null;
  /** 后端富化自 DB；富化不到时回落 kb_id，不是 null */
  kb_name: string | null;
  asset_type: string | null;
  last_cited_at: string | null;
}

/** 长期未使用资产。`last_cited_at` 为 null = 从未被引用过 */
interface ApiIdleAsset {
  asset_id: string;
  file_name: string;
  last_cited_at: string | null;
  asset_type: string;
  kb_id: string | null;
  kb_name: string;
  updated_at: string | null;
}

/** 知识库列表：整条契约字段很多，这里只声明下拉用得到的两个 */
interface ApiKbListItem {
  id: string;
  name: string;
}

interface ApiKbList {
  items: ApiKbListItem[];
  total?: number;
}

/** 处理任务的状态分布 —— 后端按**全量**任务算好，不是从明细里数出来的 */
interface ApiPipelineTaskStats {
  success: number;
  failed: number;
  running: number;
  pending: number;
  /** 仅成功任务参与；无成功任务或时间不齐时为 null */
  avg_duration_ms: number | null;
}

interface ApiAbnormalItem {
  asset_id: string;
  file_name: string | null;
  /** 失败发生在哪一步，取值即 `DoStage` 的三档 */
  task_type: string;
  occurred_at: string | null;
  /** 详情键值对，键对齐 `dataOperations.anomaly.field.*`（下钻抽屉的载体） */
  detail: Record<string, string>;
}

export interface DataAssetsApi {
  total_assets: number;
  total_data_sources: number;
  cited_assets: number;
  /** 0~1 小数（契约示例 0.019），不是百分数 */
  usage_rate: number;
  access_trend: ApiTrendPoint[];
  composition: ApiCompositionItem[];
  top_cited_assets: ApiCitedAsset[];
  unused_assets: ApiIdleAsset[];
  /** 四级漏斗 */
  processing_pipeline: {
    total: number;
    parsed: number;
    extracted: number;
    compiled: number;
  };
  pipeline_task_stats: ApiPipelineTaskStats;
  abnormal_summary: {
    total: number;
    /** stage 是后端 `_STAGE_ORDER` 的三档，与前端 `DoStage` 是同一个闭集 */
    by_stage: { stage: DoStage; count: number }[];
  };
  /** 只含**未处置**的异常，按发生时间倒序；后端给的清单封顶 20 条 */
  abnormal_assets: ApiAbnormalItem[];
}

/**
 * 高频使用本体实例（后端取 Top10）。
 * `adopted_count` 结构性恒 0 —— 采用事件只记 document，实体采用无处可取，不是数据没到。
 */
export interface ApiUsedEntity {
  object_id: string;
  canonical_name: string;
  entity_type: string;
  hit_count: number;
  adopted_count: number;
  last_used_at: string | null;
  kb_id: string | null;
  kb_name: string | null;
}

/** 长期未使用实例。`last_used_at` 为 null = 全时段从未命中过 */
export interface ApiUnusedEntity {
  object_id: string;
  canonical_name: string;
  entity_type: string;
  created_at: string | null;
  updated_at: string | null;
  last_used_at: string | null;
  kb_id: string | null;
  kb_name: string | null;
}

export interface KnowledgeAssetsApi {
  ontology: { entities: number; relations: number; active_entities: number; activity_rate: number };
  wiki: { entities: number; concepts: number; active_pages: number; activity_rate: number };
  entity_type_distribution: { entity_type: string; count: number }[];
  kb_entity_distribution: {
    kb_id: string | null;
    kb_name: string;
    count: number;
    ratio: number;
  }[];
  entity_growth_trend: ApiTrendPoint[];
  top_used_entities: ApiUsedEntity[];
  unused_entities: ApiUnusedEntity[];
}

export interface SearchAnalyticsApi {
  total_requests: number;
  active_users: number;
  /** 用户数为 0 时后端给 null，与契约声明的数值类型不符 */
  avg_requests_per_user: number | null;
  searched_kbs: number;
  total_kbs: number;
  unused_kbs: number;
  used_services: number;
  request_trend: ApiTrendPoint[];
  pipeline_usage: {
    valid_requests: number;
    ontology: { count: number; rate: number };
    rag: { count: number; rate: number };
    wiki: { count: number; rate: number };
  };
  top_questions: { query: string; count: number }[];
  question_topics: unknown[];
  kb_usage: {
    kb_id: string;
    kb_name: string;
    request_count: number;
    last_used_at: string | null;
    /** 库类型 lightrag / openkb / jonex_wiki；后端富化不到时为 null */
    kb_type: string | null;
  }[];
  /** 领域服务归因：请求经哪个服务发起。`kb_count` 是这次统计范围内归属该服务的库数 */
  service_usage: {
    service_id: string;
    service_name: string;
    request_count: number;
    kb_count: number;
    last_used_at: string | null;
  }[];
}

/** 统计窗口：后端默认 7 天，前端各块共用同一个值 */
const DO_WINDOW_DAYS = 7;

/** 知识库列表的翻页大小：网关 limit 上限就是 100 */
const KB_PAGE_SIZE = 100;

/** 领域空间为空时不下发该参数，后端即按租户级全量统计 */
const scopeParams = (spaceId?: string | null, extra: Record<string, unknown> = {}) => ({
  ...(spaceId ? { domain_space_id: spaceId } : {}),
  days: DO_WINDOW_DAYS,
  ...extra,
});

/**
 * 同一批并发调用合并成一次请求。后端按「页」聚合、本层按「块」暴露，
 * 一次页面加载会并发调 3-4 个块函数，不合并就是同一份数据拉 4 遍。
 *
 * key 在 promise 落地时立即删除：合并只发生在同一批并发之内，
 * 下次进页面或点刷新会重新请求，不会读到上一批的陈旧数据。
 */
const inflight = new Map<string, Promise<unknown>>();

function coalesce<T>(key: string, fetcher: () => Promise<T>): Promise<T> {
  const hit = inflight.get(key) as Promise<T> | undefined;
  if (hit) return hit;
  const promise = fetcher().finally(() => inflight.delete(key));
  inflight.set(key, promise);
  return promise;
}

/**
 * 单库收窄的库 id：下拉的「全部知识库」（DO_KB_ALL）是前端哨兵值，不是真实库 id，不下发。
 * 返回 undefined 时 axios 直接丢掉该参数，后端即按空间/租户全量统计。
 */
const narrowKbId = (kbId?: string) => (kbId && kbId !== DO_KB_ALL ? kbId : undefined);

/**
 * data-assets 一次取回「趋势 + 构成 + KPI」，三块各有自己的收窄条件与窗口，
 * 所以 key 里带上 days / kb_id：条件相同的并发调用合并成一次请求，不同的各发各的。
 */
const fetchDataAssets = (
  spaceId?: string | null,
  opts: { days?: number; kbId?: string; unusedThresholdDays?: number } = {},
) => {
  const days = opts.days ?? DO_WINDOW_DAYS;
  const kbId = narrowKbId(opts.kbId);
  return coalesce(`assets|${spaceId ?? ''}|${days}|${opts.unusedThresholdDays ?? ''}|${kbId ?? ''}`, () =>
    apiClient.get<DataAssetsApi>('/knowledge-base/data-operations/data-assets', {
      // scopeParams 的 extra 在 days 之后展开，所以这里传进去的 days 会覆盖默认窗口
      params: scopeParams(spaceId, {
        days,
        ...(opts.unusedThresholdDays ? { unused_threshold_days: opts.unusedThresholdDays } : {}),
        kb_id: kbId,
      }),
    }),
  );
};

/**
 * 同上：知识/检索两页里，趋势卡有自己的窗口，所以 days 也进 key，与其他块各取各的。
 *
 * `unusedThresholdDays` 只有「长期未使用知识」那一块用（30/90 切换）—— 后端按它算出
 * 阈值窗口、只把窗口内没命中过的实体放进 `unused_entities`。它必须进 coalesce key，
 * 否则两个窗口会复用同一份响应，切换看着像没生效。
 */
const fetchKnowledgeAssets = (
  spaceId?: string | null,
  days: number = DO_WINDOW_DAYS,
  unusedThresholdDays?: number,
) =>
  coalesce(`knowledge|${spaceId ?? ''}|${days}|${unusedThresholdDays ?? ''}`, () =>
    apiClient.get<KnowledgeAssetsApi>('/knowledge-base/data-operations/knowledge-assets', {
      params: scopeParams(spaceId, {
        days,
        ...(unusedThresholdDays ? { unused_threshold_days: unusedThresholdDays } : {}),
      }),
    }),
  );

const fetchSearchAnalytics = (spaceId?: string | null, days: number = DO_WINDOW_DAYS) =>
  coalesce(`search|${spaceId ?? ''}|${days}`, () =>
    apiClient.get<SearchAnalyticsApi>('/knowledge-base/data-operations/search-analytics', {
      params: scopeParams(spaceId, { days }),
    }),
  );

/** 趋势点：后端 count → 前端 value，日期轴原样 */
const toTrend = (points: ApiTrendPoint[]): DoTrendPoint[] =>
  points.map((point) => ({ date: point.date, value: point.count }));

/* ══════════════════════ 页 1：数据资产 ══════════════════════ */

/**
 * 知识库筛选下拉的选项：**当前空间下的知识库**，未选空间即租户全量。
 *
 * 取平台既有的知识库列表（按 `knowledge_info.space_id` 筛），与 4 个聚合端点圈定范围用的是同一个字段，
 * 所以「图表统哪些库」与「下拉能选哪些库」是同一批。传空空间时两边也一致：列表不判空间成员，
 * 聚合端点在这条路上只放行租户管理员。
 *
 * 网关把 limit 卡在 100，翻页取完为止，不让空间里的库多了以后被静默截成前 100 个。
 */
export async function listKbOptions(spaceId?: string | null): Promise<DoKbOption[]> {
  const options: DoKbOption[] = [];
  for (let offset = 0; ; offset += KB_PAGE_SIZE) {
    const page = await apiClient.get<ApiKbList>('/knowledge-base/knowledge-info', {
      params: { ...(spaceId ? { space_id: spaceId } : {}), offset, limit: KB_PAGE_SIZE },
    });
    options.push(...page.items.map(({ id, name }) => ({ id, name })));
    // 短页即末页；再拿 total 兜一道，免得满页 + 后端不返回 total 时白跑一趟
    if (page.items.length < KB_PAGE_SIZE) return options;
    if (typeof page.total === 'number' && options.length >= page.total) return options;
  }
}

/** 四张 KPI 卡取自 data-assets 的汇总字段；引用率后端给 0~1 小数，展示要乘 100 */
export async function getAssetKpis(spaceId?: string | null): Promise<DoKpiCard[]> {
  const data = await fetchDataAssets(spaceId);
  const values: Record<string, string> = {
    total: fmtCount(data.total_assets),
    source: fmtCount(data.total_data_sources),
    cited: fmtCount(data.cited_assets),
    usageRate: fmtRate(data.usage_rate * 100),
  };
  return DO_ASSET_KPIS.map((kpi) => ({ ...kpi, value: values[kpi.key] ?? kpi.value }));
}

/**
 * 数据接入趋势。单库筛选按 kb_id 收窄；窗口由页面按「近 7 / 近 14 天」直接给定 ——
 * 切换会各取一次（7 天是 14 天的子集，但两个窗口的 X 轴、以及用户在切的那一下的反馈，都靠这次请求）。
 */
export async function getAssetTrend(
  kbId: string,
  spaceId?: string | null,
  days: number = DO_WINDOW_DAYS,
): Promise<DoTrendPoint[]> {
  const data = await fetchDataAssets(spaceId, { days, kbId });
  return toTrend(data.access_trend);
}

/** 资产构成。按来源的构成契约里没有对应字段，页面的「来源类型」分支已下线 */
export async function getAssetComposition(
  kbId: string,
  spaceId?: string | null,
): Promise<{
  /** 按数据类型分组的构成 */
  composition: DoProportionItem[];
}> {
  const data = await fetchDataAssets(spaceId, { kbId });
  // 后端已直出前端口径的 asset_type（表格替前端做了 spreadsheet → table），
  // 前端不再自己按 category 映射一遍 —— 两处各映射一次必然会漂。
  const counts = new Map(data.composition.map((item) => [item.asset_type, item.count]));
  return {
    composition: DO_ASSET_COMPOSITION.map((item) => ({
      ...item,
      count: counts.get(item.key) ?? 0,
    })),
  };
}

/** 前端资产类型全集：6 个业务类 + `other` 兜底展示位 */
const ASSET_TYPES: readonly DoAssetType[] = ['document', 'table', 'image', 'structured', 'video', 'audio', 'other'];

/**
 * 后端 `asset_type` → 前端类型标签。
 *
 * 直读后端字段，不在前端按 mime 再判一次：后端用的是 `(mime, 扩展名)`，
 * 而 `.json` / `.yaml` / `.parquet` 的 mime 常是 `application/octet-stream`，
 * 只看 mime 会把它们判成文档，与同一页的构成统计对不上号。
 * 后端对 7 类之外的资产照直输出 `other`，落到兜底位。
 */
function toAssetType(assetType: string | null | undefined): DoAssetType {
  return ASSET_TYPES.includes(assetType as DoAssetType) ? (assetType as DoAssetType) : 'other';
}

/**
 * 高频引用资产。
 *
 * `kbName` 只作展示、`kbId` 是「前往知识库」的跳转目标，两者不是一回事：
 * 后端富化不到库名时回落 kb_id，所以只有 kb_id 为 null 时才真的没得跳。
 */
export async function listHotAssets(spaceId?: string | null): Promise<DoHotAsset[]> {
  const data = await fetchDataAssets(spaceId);
  return data.top_cited_assets.map((item) => ({
    id: item.asset_id,
    name: item.file_name || item.asset_id,
    assetType: toAssetType(item.asset_type),
    kbName: item.kb_name || item.kb_id || '',
    kbId: item.kb_id ?? null,
    refCount: item.cite_count,
    lastRefAt: item.last_cited_at ? formatDateTime(item.last_cited_at) : '',
  }));
}

/**
 * 长期未使用资产：READY 资产中「最近一次被采用」为空或早于阈值。
 * 阈值真下发给后端（`unused_threshold_days`），30/90 切换各取各的。
 */
export async function listIdleAssets(windowDays: 30 | 90, spaceId?: string | null): Promise<DoIdleAsset[]> {
  const data = await fetchDataAssets(spaceId, { unusedThresholdDays: windowDays });
  return data.unused_assets.map((item) => ({
    id: item.asset_id,
    name: item.file_name || item.asset_id,
    assetType: toAssetType(item.asset_type),
    kbName: item.kb_name || item.kb_id || '',
    kbId: item.kb_id ?? null,
    // 保住 null：表格据此渲染「从未引用」，格式化会把这个信息抹成 '-'
    lastRefAt: item.last_cited_at ? formatDateTime(item.last_cited_at) : null,
    updatedAt: formatDateTime(item.updated_at),
  }));
}

/**
 * 数据处理流水：四级漏斗 + 任务状态分布。
 *
 * 状态分布直接取后端的 `pipeline_task_stats`（全量任务算好的），不拿明细现数 ——
 * 后端那份明细是 TOP-20，数出来是「最近 20 条的分布」。
 */
export async function getPipeline(spaceId?: string | null): Promise<{
  data: DoPipelineOverview;
  stats: DoPipelineStats;
}> {
  const res = await fetchDataAssets(spaceId);
  const stats = res.pipeline_task_stats;
  return {
    data: res.processing_pipeline,
    stats: {
      success: stats.success,
      failed: stats.failed,
      running: stats.running,
      pending: stats.pending,
      avgDurationMs: stats.avg_duration_ms,
    },
  };
}

/**
 * 异常资产：汇总 + 最近一条失败。
 *
 * 后端给的清单只含**未处置**的（与同一张卡的 `abnormal_summary.total` 同口径），
 * 已按发生时间倒序，所以第一条就是「最近」。已处置的走下钻接口，本页不展示。
 */
export async function getAbnormalAssets(spaceId?: string | null): Promise<{
  summary: DoAnomalySummary;
  latest: DoAnomalyAsset | null;
}> {
  const data = await fetchDataAssets(spaceId);
  const latest = data.abnormal_assets[0];
  return {
    summary: {
      total: data.abnormal_summary.total,
      byStage: data.abnormal_summary.by_stage,
    },
    latest: latest
      ? {
          id: latest.asset_id,
          assetName: latest.file_name || latest.asset_id,
          stage: latest.task_type as DoStage,
          // 取 detail.reason 而不是同级的 error：后端在那里已经把空原因兜成了固定文案
          reason: latest.detail?.reason ?? '',
          occurredAt: formatDateTime(latest.occurred_at),
        }
      : null,
  };
}

/* ══════════════════════ 页 2：知识资产 ══════════════════════ */

/** 四张卡：本体两项取 ontology，Wiki 两项后端恒 0（Wiki 侧未接入） */
export async function getKnowledgeKpis(spaceId?: string | null): Promise<DoKnowledgeKpi[]> {
  const data = await fetchKnowledgeAssets(spaceId);
  const values: Record<string, number> = {
    object: data.ontology.entities,
    relation: data.ontology.relations,
    entity: data.wiki.entities,
    concept: data.wiki.concepts,
  };
  // 近 7 天新增：只有本体实体的增长趋势能求和，其余三项后端未提供，恒 +0 而非留白
  const objectDelta = data.entity_growth_trend.reduce((sum, point) => sum + point.count, 0);
  return DO_KNOWLEDGE_KPIS.map((kpi) => ({
    ...kpi,
    value: fmtCount(values[kpi.key] ?? 0),
    deltaText: fmtSigned(kpi.key === 'object' ? objectDelta : 0),
  }));
}

/**
 * 分析对象概览：分布 + 活跃度。窗口固定在近 7 天，与 KPI 卡的「近 7 天新增/活跃」同口径；
 * 增长趋势有自己的窗口，见 getKnowledgeTrend。
 */
export async function getKnowledgeOverview(
  scope: DoScope,
  spaceId?: string | null,
): Promise<{
  dist: DoProportionItem[];
  activity: DoScopeActivity;
}> {
  const data = await fetchKnowledgeAssets(spaceId);
  if (scope === 'wiki') {
    // ⚠️ 后端只有 ontology 一侧的数据，wiki 全为 0 —— 切到 Wiki 都是空态，不是没取数
    return {
      dist: [],
      activity: { activeCount: data.wiki.active_pages, activeRate: data.wiki.activity_rate * 100 },
    };
  }
  return {
    // 后端「其他」汇总行的 kb_id 是 null，用固定 key 兜住，免得 React 列表里出现 null key
    dist: data.kb_entity_distribution.map((item) => ({
      key: item.kb_id ?? 'other',
      label: item.kb_name,
      count: item.count,
    })),
    activity: {
      activeCount: data.ontology.active_entities,
      activeRate: data.ontology.activity_rate * 100,
    },
  };
}

/** 知识资产增长趋势。窗口由页面的「近 7 / 近 14 天」给定 —— 切一次取一次 */
export async function getKnowledgeTrend(
  scope: DoScope,
  spaceId?: string | null,
  days: number = DO_WINDOW_DAYS,
): Promise<DoTrendPoint[]> {
  const data = await fetchKnowledgeAssets(spaceId, days);
  // ⚠️ 后端只有 ontology 的增长趋势，wiki 侧全为 0 —— 切到 Wiki 是空态
  return scope === 'wiki' ? [] : toTrend(data.entity_growth_trend);
}

/** 高频使用本体实例（后端 Top10）。后端只统计本体实体，Wiki 侧没这一项 —— 切到 Wiki 是空表 */
export async function listHotInstances(scope: DoScope, spaceId?: string | null): Promise<DoHotInstance[]> {
  const data = await fetchKnowledgeAssets(spaceId);
  if (scope === 'wiki') return [];
  return data.top_used_entities.map((item) => ({
    id: item.object_id,
    name: item.canonical_name,
    objectType: item.entity_type,
    kbName: item.kb_name ?? '',
    hitCount: item.hit_count,
    adoptCount: item.adopted_count,
    lastUsedAt: item.last_used_at ? formatDateTime(item.last_used_at) : '',
  }));
}

/**
 * 长期未使用知识。`windowDays` 就是后端的 `unused_threshold_days` —— 判据是「阈值窗口内
 * 一次都没命中过」，算在服务端，所以 30/90 切换是真筛选而不是前端过滤。
 */
export async function listIdleKnowledge(
  scope: DoScope,
  windowDays: 30 | 90,
  spaceId?: string | null,
): Promise<DoIdleKnowledge[]> {
  const data = await fetchKnowledgeAssets(spaceId, DO_WINDOW_DAYS, windowDays);
  // 同 listHotInstances：后端只有本体实体
  if (scope === 'wiki') return [];
  return data.unused_entities.map((item) => ({
    id: item.object_id,
    name: item.canonical_name,
    objectType: item.entity_type,
    kbName: item.kb_name ?? '',
    // 保持 null：页面靠它区分「从未使用过」与「有日期」
    lastUsedAt: item.last_used_at ? formatDateTime(item.last_used_at) : null,
    updatedAt: formatDateTime(item.updated_at),
  }));
}

/* ══════════════════════ 页 3：检索分析 ══════════════════════ */

/** 检索概览：KPI + 检索方式。窗口固定在近 7 天 —— KPI 卡面写死了「近 7 天」，不能被趋势卡的窗口牵动 */
export async function getSearchOverview(spaceId?: string | null): Promise<{
  kpis: DoKpiCard[];
  modes: DoSearchMode[];
}> {
  const data = await fetchSearchAnalytics(spaceId);
  const values: Record<string, string> = {
    requests: fmtCount(data.total_requests),
    kbs: `${data.searched_kbs}/${data.total_kbs}`,
    users: fmtCount(data.active_users),
    // 用户数为 0 时后端给 null（契约声明是数值），「人均」没有定义，显示占位而不是 0
    avg: data.avg_requests_per_user == null ? '-' : String(data.avg_requests_per_user),
  };
  return {
    kpis: DO_SEARCH_KPIS.map((kpi) => ({ ...kpi, value: values[kpi.key] ?? kpi.value })),
    // ⚠️ 三个计数取自检索事件表 —— 埋点上线后才有数据，存量库没跑过 backfill 时三项都是 0（参与率同源）
    modes: DO_SEARCH_MODES.map((mode) => ({
      ...mode,
      count: data.pipeline_usage[mode.key].count,
      participationRate: data.pipeline_usage[mode.key].rate * 100,
    })),
  };
}

/** 检索请求趋势。窗口由页面的「近 7 / 近 14 天」给定 —— 切一次取一次 */
export async function getSearchTrend(spaceId?: string | null, days: number = DO_WINDOW_DAYS): Promise<DoTrendPoint[]> {
  const data = await fetchSearchAnalytics(spaceId, days);
  return toTrend(data.request_trend);
}

export async function listHotQuestions(spaceId?: string | null): Promise<DoHotQuestion[]> {
  const data = await fetchSearchAnalytics(spaceId);
  return data.top_questions.map((item, index) => ({
    rank: index + 1,
    question: item.query,
    // ⚠️ 后端 top_questions 只有 query 与 count，没有主题字段（聚类是三期）—— 主题列只能是空的
    topicKey: '',
    count: item.count,
  }));
}

/** 提问主题分类。后端 `question_topics` 是硬编码空数组（离线聚类，三期），此块必然是空表 */
export async function getQuestionTopics(spaceId?: string | null): Promise<DoProportionItem[]> {
  await fetchSearchAnalytics(spaceId);
  return [];
}

/**
 * 后端 `kb_type` → i18n key。未知取值（含 null）不拼 key，列上显示「—」，
 * 免得把 `dataOperations.kbType.xxx` 这种原始 key 漏到界面上。
 */
const KB_TYPE_KEYS: Record<string, string> = {
  lightrag: 'dataOperations.kbType.lightrag',
  openkb: 'dataOperations.kbType.openkb',
  jonex_wiki: 'dataOperations.kbType.jonex_wiki',
};

/** 知识库使用情况。后端按检索历史聚合 —— 表里只会出现近 7 天真有检索记录的库 */
export async function listKbUsage(spaceId?: string | null): Promise<DoUsageItem[]> {
  const data = await fetchSearchAnalytics(spaceId);
  return data.kb_usage.map((item) => ({
    id: item.kb_id,
    name: item.kb_name,
    typeKey: item.kb_type ? KB_TYPE_KEYS[item.kb_type] : undefined,
    count: item.request_count,
    lastUsedAt: item.last_used_at ? formatDateTime(item.last_used_at) : '',
  }));
}

/** 领域服务使用情况。后端按「请求经哪个服务发起」归因 —— 表里只会出现近 7 天有请求的服务 */
export async function listServiceUsage(spaceId?: string | null): Promise<DoUsageItem[]> {
  const data = await fetchSearchAnalytics(spaceId);
  return data.service_usage.map((item) => ({
    id: item.service_id,
    name: item.service_name,
    count: item.request_count,
    lastUsedAt: item.last_used_at ? formatDateTime(item.last_used_at) : '',
  }));
}
