-- ============================================================
-- 数据运营二期：检索请求事件补 kb 列表列 + 处理任务事件聚合索引
-- 版本: 038
-- 对应全量 DDL: deploy/postgres/migrations/008_search_events.sql
-- 依据: 数据运营二期事件表设计（与全量 DDL 008_search_events.sql 的表结构一致）
--
-- 为什么需要本脚本：008_search_events.sql 已线上发布，它按
-- `CREATE TABLE IF NOT EXISTS` 建表 —— 对线上已存在的表**整块跳过**，
-- 所以后来加进 008 文件里的列/索引，线上一条都没有。此后 008 只允许
-- 改注释，一切表结构变更走本目录的增量语句（见 apply.sh）。
--
-- 编号说明：本脚本原计划编号 037，但 037 已被
-- 037_task_center_subtask_reports_task_id.sql 占用，故顺延为 038。
--
-- ── 第一部分：检索请求事件补 knowledge_base_ids ──
-- 一期该表没有 kb 列，导致「按库归因」的统计只能依赖
-- knowledge_search_history.knowledge_base_id —— 而那是**客户端上报的可选字段**，
-- 空间内多库检索时必然为空串（search_service 写入处有明确注释）。实测
-- tenant_jonex_demo 近 7 天 31 次检索里有 27 次（87%）该列为空，
-- 于是所有按库归因的指标系统性偏低。
--
-- 二期改为由入口埋点写入「权限过滤后的 kb 列表」，本列即载体。
-- 用 JSONB 数组而非关联表：一次检索的 kb 列表是**快照**，
-- 事后 kb 被删/被移出空间都不应改变历史统计口径。
ALTER TABLE knowledge_base.search_request_event
  ADD COLUMN IF NOT EXISTS knowledge_base_ids JSONB NOT NULL DEFAULT '[]';

COMMENT ON COLUMN knowledge_base.search_request_event.knowledge_base_ids IS '本次检索实际覆盖的知识库 id 列表（来自 capability 权限过滤后的 data，不是客户端上报值）；领域服务形态（只有 service_id）入口处为空数组，聚合期按 entry_service_id 反查 service_knowledge_bases 补齐';

-- GIN：聚合侧要按 jsonb 数组元素做空间过滤（EXISTS + jsonb_array_elements_text）
CREATE INDEX IF NOT EXISTS idx_sre_kbs
  ON knowledge_base.search_request_event USING GIN (knowledge_base_ids);

-- ── 第二部分：检索线聚合索引 ──
-- 一期只建了 (tenant_id, 时间列 DESC) 与单列 request 索引；
-- 二期聚合要按 request_id 关联步骤/采用、按 (request_id, target_type) 关联命中。
CREATE INDEX IF NOT EXISTS idx_sse_request_stage
  ON knowledge_base.search_step_event (request_id, stage);
CREATE INDEX IF NOT EXISTS idx_sae_request
  ON knowledge_base.search_adoption_event (request_id);
CREATE INDEX IF NOT EXISTS idx_she_request_type
  ON knowledge_base.search_hit_event (request_id, target_type);

-- ── 第三部分：处理任务事件聚合索引 ──
-- 原表只有 (tenant_id, started_at DESC) 与 asset_id。
-- 流水/异常聚合要按 (tenant_id, task_type, status) 数状态分布、
-- 按 (tenant_id, kb_id, started_at) 做空间级过滤、按 (asset_id, task_type)
-- 取「该资产该阶段的全部任务」以判 handled / retry_count。
CREATE INDEX IF NOT EXISTS idx_pte_tenant_task_status
  ON knowledge_base.processing_task_event (tenant_id, task_type, status);
CREATE INDEX IF NOT EXISTS idx_pte_tenant_kb_started
  ON knowledge_base.processing_task_event (tenant_id, kb_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pte_asset_type
  ON knowledge_base.processing_task_event (asset_id, task_type);

-- 不加唯一约束：uq_pte_task 已在 008 建好（唯一约束需要跨文件判存在，
-- IF NOT EXISTS 对 ALTER TABLE ADD CONSTRAINT 不通用，且这里没有必要）。
