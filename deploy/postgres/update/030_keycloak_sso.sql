-- ============================================================
-- Keycloak SSO 数据模型增量 DDL（存量库迭代，人工触发 apply.sh / make db-migrate）
-- 版本: 030
-- 变更：
--   1) platform.users 新增 external_id（Keycloak sub UUID 字符串，映射本地 int user_id）
--   2) platform.users.password_hash 由 NOT NULL 改为可空（影子用户首登无本地密码，D7/BC2）
--   3) (tenant_id, external_id) partial unique index（对齐 username 的软删唯一模式）
--   4) 新增 platform.tenant_realm_map（租户 ↔ Realm 映射，realm 唯一，PRD §3.2 / T5）
-- 全部 IF [NOT] EXISTS 幂等，可重复执行。
-- ============================================================

-- 1. users.external_id：Keycloak sub(UUID) 字符串，映射本地 int user_id（D7 映射桥梁）
ALTER TABLE platform.users ADD COLUMN IF NOT EXISTS external_id VARCHAR(64);

-- 2. users.password_hash：NOT NULL -> 可空（密码校验上移 Keycloak，影子用户首登无本地密码）
ALTER TABLE platform.users ALTER COLUMN password_hash DROP NOT NULL;

-- 3. (tenant_id, external_id) 唯一约束（partial，软删隔离；存量 external_id 全 NULL，NULL 不相撞）
CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_external_id_key
    ON platform.users(tenant_id, external_id) WHERE is_deleted = 0;

-- 4. 租户 ↔ Realm 映射表
CREATE TABLE IF NOT EXISTS platform.tenant_realm_map (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    realm VARCHAR(128) NOT NULL UNIQUE,
    issuer_url VARCHAR(512) NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    status SMALLINT DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tenant_realm_map_tenant ON platform.tenant_realm_map(tenant_id);
