-- ============================================================
-- 企业身份集成：新增 3 张 platform 表（存量库增量 DDL，幂等可重跑）
-- 版本: 031
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql（三张表并入 002，无独立 008）
-- 依据: PRD-2026-09-10-企业身份集成-后端版 §4 数据模型
-- ============================================================

-- 企业身份集成配置（配置聚合根，一租户一份）
CREATE TABLE IF NOT EXISTS platform.identity_integration_config (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    sso_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    login_policy VARCHAR(32) NOT NULL DEFAULT 'auto_create',
    login_field_mapping JSONB,
    sso_connection_verified_at TIMESTAMP,
    datasource_name VARCHAR(255),
    datasource_url VARCHAR(512),
    auth_type VARCHAR(32),
    token_ciphertext VARCHAR(1024),
    auto_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    sync_interval INTEGER,
    datasource_connection_verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_integration_config_tenant
    ON platform.identity_integration_config(tenant_id);

-- 同步任务（同步任务聚合根，同步头）
CREATE TABLE IF NOT EXISTS platform.identity_sync_tasks (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    sync_type VARCHAR(32) NOT NULL DEFAULT 'incremental',
    trigger VARCHAR(32) NOT NULL DEFAULT 'manual',
    user_added INTEGER NOT NULL DEFAULT 0,
    user_updated INTEGER NOT NULL DEFAULT 0,
    user_disabled INTEGER NOT NULL DEFAULT 0,
    user_enabled INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    cursor VARCHAR(1024),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_identity_sync_tasks_tenant
    ON platform.identity_sync_tasks(tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_sync_tasks_running
    ON platform.identity_sync_tasks(tenant_id)
    WHERE status = 'running';

-- 同步失败明细（弱实体，归属 sync_task 聚合，不单独带 tenant_id）
CREATE TABLE IF NOT EXISTS platform.identity_sync_failures (
    id BIGSERIAL PRIMARY KEY,
    sync_task_id BIGINT NOT NULL,
    entity_type VARCHAR(32) NOT NULL DEFAULT 'user',
    external_entity_id VARCHAR(128),
    failure_reason VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_identity_sync_failures_task
    ON platform.identity_sync_failures(sync_task_id);
