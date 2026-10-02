"""数据运营（Data Operations）响应 DTO。

这些 DTO 是 5 个数据运营聚合接口（overview / data-assets / knowledge-assets /
search-analytics / abnormal-assets）返回结构的代码化定义，与 `AnalyticsService`
返回的 dict 字段一一对应。
字段即**前后端契约**：改名 / 删除 / 换语义都必须与前端 `Do*` 类型同步改，
否则前端静默拿到 `undefined`，而接口本身不报错。

二期新增字段（相对一期）在注释里都标了 `[二期]`，便于前端对照增量。
"""

from typing import Dict, List, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class TrendPoint(BaseModel):
    date: str
    count: int


class AssetCategoryCount(BaseModel):
    category: str
    # [二期] 前端口径的资产类型（表格：spreadsheet → table），前端可直接用
    asset_type: Optional[str] = None
    count: int


class CitedAssetItem(BaseModel):
    asset_id: str
    file_name: str
    mime_type: Optional[str] = None
    cite_count: int
    # 所属知识库。前端「前往知识库」要跳 /apps/core-business/domain-knowledge/{kb_id}。
    # 文档被删/跨租户时富化不到，允许为空 —— 前端据此禁用按钮而不是跳到空白页
    kb_id: Optional[str] = None
    # [二期] 所属知识库名称（原型表格列）
    kb_name: Optional[str] = None
    # [二期] 资产类型（前端口径 6 类）
    asset_type: Optional[str] = None
    # [二期] 最近一次被引用时间（窗口内）
    last_cited_at: Optional[str] = None


class UnusedAssetItem(BaseModel):
    asset_id: str
    file_name: str
    # None = 从未被引用过（不是「取不到」，前端要渲染「从未使用」）
    last_cited_at: Optional[str] = None
    # [二期] 资产类型（前端口径 6 类）
    asset_type: Optional[str] = None
    # [二期] 所属知识库，供「前往知识库」跳转
    kb_id: Optional[str] = None
    # [二期] 文档最近更新时间
    updated_at: Optional[str] = None


class ProcessingTaskItem(BaseModel):
    task_id: str
    asset_id: str
    file_name: str
    task_type: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    # [二期] 资产类型（前端口径 6 类）
    asset_type: Optional[str] = None


class AbnormalAssetItem(BaseModel):
    asset_id: str
    file_name: str
    task_type: str
    status: str
    error: Optional[str] = None
    # 一期字段，语义 = occurred_at（保留，别删）
    last_failed_at: Optional[str] = None

    # [二期] 资产类型（前端口径 6 类）
    asset_type: Optional[str] = None
    # [二期] 所属知识库名称（原型表格列）
    kb_name: Optional[str] = None
    # [二期] 是否已处置：同一 (资产, 阶段) 之后有更新的成功任务
    handled: bool = False
    # [二期] 该 (资产, 阶段) 的失败次数（列表被截断后前端算不出来，故后端给）
    retry_count: int = 0
    # [二期] 失败发生时间（= finished_at / started_at / created_at 首个非空）
    occurred_at: Optional[str] = None
    # [二期] 下钻抽屉的字段包，键与前端 `dataOperations.anomaly.field.*` 一一对应
    detail: Dict[str, str] = Field(default_factory=dict)


class ProcessingPipeline(BaseModel):
    """四级漏斗（状态列 ∪ 事件表，逐级单调）。"""

    total: int
    parsed: int
    extracted: int
    compiled: int


class PipelineTaskStats(BaseModel):
    """流水任务状态分布（后端预计算，前端不算）。"""

    success: int
    failed: int
    running: int
    # 恒 0：资产建档即进入 parsing，没有等待调度的中间态
    pending: int
    # 平均耗时只统计两端时间都齐的任务，样本为空时 None
    avg_duration_ms: Optional[int] = None


class AbnormalStageCount(BaseModel):
    stage: str
    count: int


class AbnormalSummary(BaseModel):
    """异常汇总：按失败任务计数，total 恒等于 by_stage 之和（未处置口径）。"""

    total: int
    by_stage: List[AbnormalStageCount] = Field(default_factory=list)


class DataAssetsData(BaseModel):
    total_assets: int
    total_data_sources: int
    cited_assets: int
    usage_rate: float
    # [二期] 窗口内新增资产（= access_trend 之和）
    new_assets_in_window: int = 0
    # [二期] 窗口内新增数据源
    new_data_sources_in_window: int = 0
    access_trend: List[TrendPoint] = Field(default_factory=list)
    composition: List[AssetCategoryCount] = Field(default_factory=list)
    top_cited_assets: List[CitedAssetItem] = Field(default_factory=list)
    unused_assets: List[UnusedAssetItem] = Field(default_factory=list)
    # [二期] 数据处理流水（四级漏斗 + 任务状态分布 + 明细）
    processing_pipeline: ProcessingPipeline
    pipeline_task_stats: PipelineTaskStats
    processing_tasks: List[ProcessingTaskItem] = Field(default_factory=list)
    # [二期] 异常资产（汇总 + 页内清单，清单只含未处置的）
    abnormal_summary: AbnormalSummary
    abnormal_assets: List[AbnormalAssetItem] = Field(default_factory=list)


class AbnormalAssetsPage(BaseModel):
    """异常资产下钻（`get_abnormal_assets_analytics`）的分页响应。"""

    items: List[AbnormalAssetItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class OntologyAggregate(BaseModel):
    entities: int
    relations: int
    active_entities: int
    activity_rate: float


class WikiAggregate(BaseModel):
    entities: int
    concepts: int
    active_pages: int
    activity_rate: float


class EntityTypeCount(BaseModel):
    entity_type: str
    count: int


class KbEntityDistributionItem(BaseModel):
    kb_id: Optional[str] = None
    kb_name: str
    count: int
    ratio: float


class UsedEntityItem(BaseModel):
    object_id: str
    canonical_name: str
    entity_type: str
    hit_count: int
    # 结构性恒 0：采用事件只记 document，实体采用无处可取
    adopted_count: int
    last_used_at: Optional[str] = None
    # [二期] 所属知识库
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None


class UnusedEntityItem(BaseModel):
    object_id: str
    canonical_name: str
    entity_type: str
    created_at: Optional[str] = None
    # 全时段最近命中时间；None = 从未命中过
    last_used_at: Optional[str] = None
    # [二期] 所属知识库
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    updated_at: Optional[str] = None


class KnowledgeAssetsData(BaseModel):
    ontology: OntologyAggregate
    wiki: WikiAggregate
    entity_type_distribution: List[EntityTypeCount] = Field(default_factory=list)
    kb_entity_distribution: List[KbEntityDistributionItem] = Field(default_factory=list)
    entity_growth_trend: List[TrendPoint] = Field(default_factory=list)
    top_used_entities: List[UsedEntityItem] = Field(default_factory=list)
    unused_entities: List[UnusedEntityItem] = Field(default_factory=list)


class PipelineRate(BaseModel):
    count: int
    rate: float


class PipelineUsage(BaseModel):
    valid_requests: int
    ontology: PipelineRate
    rag: PipelineRate
    wiki: PipelineRate


class TopQuestionItem(BaseModel):
    query: str
    count: int


class QuestionTopicItem(BaseModel):
    topic: str
    count: int


class KbUsageItem(BaseModel):
    kb_id: str
    kb_name: str
    request_count: int
    last_used_at: Optional[str] = None
    # [二期] 库类型（lightrag / openkb / jonex_wiki），前端显示库类型徽标
    kb_type: Optional[str] = None


class ServiceUsageItem(BaseModel):
    service_id: str
    service_name: str
    request_count: int
    kb_count: int
    last_used_at: Optional[str] = None


class SearchAnalyticsData(BaseModel):
    total_requests: int
    active_users: int
    avg_requests_per_user: Optional[float] = None
    searched_kbs: int
    total_kbs: int
    unused_kbs: int
    used_services: int
    request_trend: List[TrendPoint] = Field(default_factory=list)
    pipeline_usage: PipelineUsage
    top_questions: List[TopQuestionItem] = Field(default_factory=list)
    question_topics: List[QuestionTopicItem] = Field(default_factory=list)
    kb_usage: List[KbUsageItem] = Field(default_factory=list)
    service_usage: List[ServiceUsageItem] = Field(default_factory=list)


class OverviewDataAssets(BaseModel):
    total_assets: int
    total_data_sources: int
    cited_assets: int
    usage_rate: float


class OverviewKnowledgeAssets(BaseModel):
    ontology_entities: int
    ontology_relations: int
    wiki_entities: int
    wiki_concepts: int


class OverviewSearchAnalytics(BaseModel):
    total_requests: int
    searched_kbs: int
    total_kbs: int
    used_services: int
    unused_kbs: int
    active_users: int


class OverviewData(BaseModel):
    data_assets: OverviewDataAssets
    knowledge_assets: OverviewKnowledgeAssets
    search_analytics: OverviewSearchAnalytics


__all__ = [
    "AbnormalAssetItem",
    "AbnormalAssetsPage",
    "AbnormalStageCount",
    "AbnormalSummary",
    "AssetCategoryCount",
    "CitedAssetItem",
    "DataAssetsData",
    "EntityTypeCount",
    "KbEntityDistributionItem",
    "KbUsageItem",
    "KnowledgeAssetsData",
    "OntologyAggregate",
    "OverviewData",
    "OverviewDataAssets",
    "OverviewKnowledgeAssets",
    "OverviewSearchAnalytics",
    "PipelineRate",
    "PipelineTaskStats",
    "PipelineUsage",
    "ProcessingPipeline",
    "ProcessingTaskItem",
    "QuestionTopicItem",
    "SearchAnalyticsData",
    "ServiceUsageItem",
    "TopQuestionItem",
    "TrendPoint",
    "UnusedAssetItem",
    "UnusedEntityItem",
    "UsedEntityItem",
    "WikiAggregate",
]
