/**
 * 数据运营模块的类型定义。
 *
 * 页面、组件与取数层（api/dataOperations.ts）共用这一份 —— 组件只认这里的形状，
 * 不关心数据来自常量还是后端接口。
 */

/**
 * 资产类型 —— 决定 tag 配色与文案 key。
 *
 * `other` 是后端 7 类分类里落不进前 6 类的那一档（契约要求前端留个展示位），
 * 也是后端 `asset_type` 出现未知值时的兜底 —— 宁可在页面上标「其他」，
 * 也不要把判不出来的资产悄悄混进「文档」。
 */
export type DoAssetType = 'document' | 'table' | 'image' | 'structured' | 'video' | 'audio' | 'other';

/** 处理阶段 —— 数据处理流水与异常资产共用 */
export type DoStage = 'parse' | 'extract' | 'compile';

/** 分析对象口径 —— 知识资产页的分段器 */
export type DoScope = 'ontology' | 'wiki';

/**
 * KPI 卡。value 存已格式化的展示串（千分位）：这些数字来自不同接口、
 * 格式规则也不完全一致（如「6/9」这种比值），在数据层定稿比在每个页面各写一遍 toLocaleString 稳。
 */
export interface DoKpiCard {
  key: string;
  labelKey: string;
  value: string;
  /** 强调色，用于图标底色 */
  accent: string;
  /** 图标标识，映射表在 components/StatCards.tsx */
  iconKey: string;
}

/** 趋势图数据点 */
export interface DoTrendPoint {
  date: string;
  value: number;
}

/**
 * 占比条条目。percent 不在这里存：占比一律由 count 与同组总量算出（见 ProportionList），
 * 免得常量里的数字与 count 对不上。口径不同的比率（如检索参与率）另用专门类型。
 */
export interface DoProportionItem {
  key: string;
  /** i18n 文案 key；知识库名这类不需要翻译的走 label */
  labelKey?: string;
  label?: string;
  count: number;
}

export interface DoHotAsset {
  id: string;
  name: string;
  assetType: DoAssetType;
  kbName: string;
  /** 所属知识库 id，「前往知识库」的跳转目标。后端富化不到时为 null */
  kbId: string | null;
  refCount: number;
  lastRefAt: string;
}

export interface DoIdleAsset {
  id: string;
  name: string;
  assetType: DoAssetType;
  kbName: string;
  /** 所属知识库 id，「前往知识库」的跳转目标。后端富化不到时为 null */
  kbId: string | null;
  /** null 表示从未被引用 */
  lastRefAt: string | null;
  updatedAt: string;
}

/** 数据处理流水概览：四级流水线的四个大数字 */
export interface DoPipelineOverview {
  total: number;
  parsed: number;
  extracted: number;
  compiled: number;
}

export type DoTaskStatus = 'success' | 'failed' | 'running' | 'pending';

/**
 * 任务状态分布与平均耗时。
 *
 * ⚠️ 这几个数**必须取后端的 `pipeline_task_stats`**，不能拿任务明细现数：
 * 明细是按开始时间倒序的 TOP-20，数出来的是「最近 20 条里的分布」，
 * 任务一多就会把成功数少算成 20 以内。
 */
export interface DoPipelineStats {
  success: number;
  failed: number;
  running: number;
  pending: number;
  /** 仅成功任务参与；没有成功任务、或成功任务两端时间不齐时为 null */
  avgDurationMs: number | null;
}

/** 异常资产汇总：总数 + 分阶段失败数 */
export interface DoAnomalySummary {
  total: number;
  byStage: { stage: DoStage; count: number }[];
}

/**
 * 异常资产（= 失败的处理任务）。
 *
 * 后端还会给 `detail`（抽屉详情，键对齐 `dataOperations.anomaly.field.*`）与
 * `handled` / `kbName` / `retryCount`，那是异常下钻抽屉的字段；抽屉还没做，
 * 这里只保留摘要卡用得到的那几个。哪天做下钻再把它们加回来。
 */
export interface DoAnomalyAsset {
  id: string;
  assetName: string;
  stage: DoStage;
  reason: string;
  occurredAt: string;
}

export interface DoKnowledgeKpi {
  key: string;
  labelKey: string;
  value: string;
  /** 「近 7 天新增」的数值部分，前缀走 i18n 的 kpi.newIn7d */
  deltaText: string;
  accent: string;
  iconKey: string;
}

/** 分析对象切换时右侧的两个活跃度指标 */
export interface DoScopeActivity {
  /** 近 7 天活跃实例数 */
  activeCount: number;
  /** 实例活跃度百分数，如 33.58 */
  activeRate: number;
}

export interface DoHotInstance {
  id: string;
  name: string;
  objectType: string;
  kbName: string;
  hitCount: number;
  adoptCount: number;
  lastUsedAt: string;
}

export interface DoIdleKnowledge {
  id: string;
  name: string;
  objectType: string;
  kbName: string;
  /** null 表示从未使用 */
  lastUsedAt: string | null;
  updatedAt: string;
}

/** 检索方式。参与率不等于占比（同一请求可同时走多条链路），故单独给字段 */
export interface DoSearchMode {
  key: 'ontology' | 'wiki' | 'rag';
  labelKey: string;
  count: number;
  participationRate: number;
}

export interface DoHotQuestion {
  rank: number;
  question: string;
  /** 提问主题的 i18n key —— 后端没这个字段（主题聚类是三期），这一列必然为空 */
  topicKey: string;
  count: number;
}

export interface DoUsageItem {
  id: string;
  name: string;
  /** 仅知识库使用情况有类型列；领域服务不传 */
  typeKey?: string;
  count: number;
  lastUsedAt: string;
}

/** 知识库下拉的可选项 —— 只带 id 与库名，够 Select 用 */
export interface DoKbOption {
  id: string;
  name: string;
}
