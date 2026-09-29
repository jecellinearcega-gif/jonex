-- ============================================================
-- 企业身份集成：identity_integration_config 新增租户级 idp_hint_alias（存量库增量 DDL，幂等可重跑）
-- 版本: 036
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql（identity_integration_config 段）
-- 依据: 需求「SSO 登录跳过 Keycloak 中间授权页」§三.3（实现须能表达租户级覆盖值）
--
-- 用途：Keycloak 的 Client-suggested Identity Provider 特性（authorize URL 上的
-- kc_idp_hint=<alias>）让 Keycloak 303 直达企业 IdP，跳过它自己的登录表单。
-- alias 的平台级默认值走 env（KEYCLOAK_IDP_HINT_DEFAULT_ALIAS，默认 casdoor），
-- 本列是**租户级覆盖**：两者在 service 层用 `or` 合并（有覆盖值用覆盖值，
-- 否则回落平台默认）。
--
-- NULL 语义 = 用平台默认值。默认态不落库、不需要任何逐租户动作：
-- 存量租户与本列新增后未配置过的租户，一律走平台默认，零人工配置即可登录。
-- 空串不表达「清空之外的任何含义」—— 写入路径把空串归一成 NULL（见
-- capabilities/platform/services/identity_integration_config.py::upsert）。
--
-- 列宽 VARCHAR(64) 与 Keycloak alias 实际约束、DTO 白名单字符集
-- `^[a-zA-Z0-9._-]{1,64}$` 三者对齐。
-- ============================================================

ALTER TABLE platform.identity_integration_config
    ADD COLUMN IF NOT EXISTS idp_hint_alias VARCHAR(64);

COMMENT ON COLUMN platform.identity_integration_config.idp_hint_alias IS
    '租户级 kc_idp_hint alias 覆盖值（Keycloak IdP alias，如 casdoor）；NULL = 用平台默认值 KEYCLOAK_IDP_HINT_DEFAULT_ALIAS，无覆盖值时不落库、无需逐租户配置；空串由写入路径归一为 NULL';
