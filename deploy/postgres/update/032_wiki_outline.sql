-- ============================================================
-- Wiki 阅读目录：新增 1 张 knowledge_base 表（存量库增量 DDL，幂等可重跑）
-- 版本: 032
-- 对应全量 DDL: deploy/postgres/migrations/004_knowledge_base.sql（wiki_outlines 段）
-- 依据: docs/jonex_wiki/wiki-reader-experience.md §4 数据模型
--
-- 用途：为 llm-wiki（kb_type=jonex_wiki）知识库提供面向读者的**有序目录**
-- （Topic/Section/Entry，LLM 起草 + LLM 按指令调整）。目录是纯展示投影，
-- 不参与检索；entry 的 path 必须落在引擎页面清单内（越界在服务层丢弃）。
-- 留档：每次保存递增 outline_version，旧 active 转 archived；
-- partial unique index 只约束 active。
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.wiki_outlines (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,

    outline_version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL DEFAULT 'active',   -- active / archived
    source VARCHAR(32) NOT NULL DEFAULT 'llm_draft',  -- llm_draft / llm_adjust

    model VARCHAR(128),
    instruction TEXT,

    page_count INTEGER NOT NULL DEFAULT 0,
    catalog_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    outline JSONB NOT NULL DEFAULT '{}'::jsonb,
    catalog_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,

    edited_by VARCHAR(128),
    generated_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_wiki_outline_active
    ON knowledge_base.wiki_outlines (tenant_id, knowledge_base_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_wiki_outline_kb
    ON knowledge_base.wiki_outlines (tenant_id, knowledge_base_id, outline_version);
