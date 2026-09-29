-- ============================================================
-- platform.users 新增 upstream_subject（上游主体标识）列 + (tenant_id, upstream_subject) partial unique index
-- 版本: 038
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql（用户表段）
-- 依据: external_id 语义冻结为「Keycloak 本地 user_id」，另开新列承载上游主体
-- 命名对齐: 002_platform.sql:62 的 users_tenant_id_external_id_key（同类 partial unique，软删隔离）
--
-- 用途
--   「认人」的主匹配键从 external_id（跨 realm **漂移**）换成 upstream_subject（跨 realm **稳定**）。
--   同一自然人在不同租户 realm 各有 Keycloak 账号（本地 user_id 不同），但上游主体 id 相同；用前者
--   会在多租户登录时被当成两个不同的人，且上游删号重建后本地 id 必变、老用户即失联。
--
--   ⛔ external_id 不得改存上游值：capabilities/platform/auth/account_sync_service.py:173 拿它拼
--      KC Admin API 的 PUT /admin/realms/{realm}/users/{external_id} 来停用账号，且失败会被
--      :157 的 except 吞成 logger.warning —— 改错就是「KC 侧其实没停用、仍可登录」的静默安全洞。
--
-- 语义（为什么是 partial index 而非普通 unique 约束）
--   谓词 `is_deleted = 0 AND upstream_subject IS NOT NULL`，两层都必要：
--     1) `is_deleted = 0` —— 该表是软删模型，删掉再重建同名行是合法运维动作；让唯一性只覆盖
--        活跃行，与 users_tenant_id_username_key / users_tenant_id_external_id_key 的取舍完全一致。
--     2) `upstream_subject IS NOT NULL` —— 存量账号该列为 NULL（待回填）。Postgres 中多个 NULL
--        本来就不互相冲突，但显式排除让「未回填」不进入索引，且与查询谓词
--        UserRepository.get_by_upstream_subject（三元组 tenant_id + upstream_subject + is_deleted == 0）
--        严格对齐 —— 两边谓词不对齐会留下「索引不管、查询能查到」的缝隙。
--
-- 幂等
--   ADD COLUMN / CREATE INDEX 均带 IF NOT EXISTS；重复执行不报错、不重建。CONSTRAINT 式的
--   ADD CONSTRAINT UNIQUE 不支持 IF NOT EXISTS，故这里用 partial unique index 表达同一语义。
--   本文件是增量 DDL，不在 migrations/ 新增或修改任何 sql；由人工触发
--   （make db-migrate-dry 预览 / make db-migrate 执行），版本表按文件名追踪。
--
-- 前置条件（无法由脚本代替人工判断）
--   脚本自检活跃且已回填行的重复，**有重复则 RAISE EXCEPTION 中止**（不登记版本、修复后可重跑），
--   报错信息会列出冲突的 tenant_id / upstream_subject。首次执行时该列刚建、全为 NULL ⇒ 必然通过；
--   该检查服务于**重跑**场景（例如已手工回填过一批数据）。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 加列：存量行全为 NULL ⇒ 天然「待回填」态，**不覆盖任何既有值**（回填幂等的前提）
-- ------------------------------------------------------------
ALTER TABLE platform.users
    ADD COLUMN IF NOT EXISTS upstream_subject VARCHAR(64);

COMMENT ON COLUMN platform.users.upstream_subject IS
    '上游客户系统的稳定主体标识（本地场景 = Casdoor id）。跨 realm 稳定，是认人主匹配键；external_id 为 Keycloak 本地 user_id、跨 realm 漂移。两者语义不得混用。';

-- ------------------------------------------------------------
-- 2. 前置检查：活跃（is_deleted = 0）且已回填（非 NULL）的行里，(tenant_id, upstream_subject) 不得重复
--    否则第 3 步的 CREATE UNIQUE INDEX 会失败于裸的 Postgres 错误（23505 + 重复键值），
--    信息量不足以定位到具体租户/主体。这里先自检并给出中文原因与冲突清单。无重复时为 no-op。
-- ------------------------------------------------------------
DO $$
DECLARE
    dup_groups INTEGER;
    dup_detail TEXT;
BEGIN
    SELECT COUNT(*), string_agg(
               format('tenant_id=%L  upstream_subject=%L  重复 %s 行', d.tenant_id, d.upstream_subject, d.cnt),
               E'\n  ' ORDER BY d.tenant_id, d.upstream_subject)
      INTO dup_groups, dup_detail
      FROM (
          SELECT tenant_id, upstream_subject, COUNT(*) AS cnt
            FROM platform.users
           WHERE is_deleted = 0
             AND upstream_subject IS NOT NULL
           GROUP BY tenant_id, upstream_subject
          HAVING COUNT(*) > 1
      ) d;

    IF dup_groups > 0 THEN
        RAISE EXCEPTION E'platform.users 存在 % 组活跃行共用同一 upstream_subject，无法建立 (tenant_id, upstream_subject) 唯一索引。\n冲突清单：\n  %\n处置：每个 (tenant_id, upstream_subject) 只保留一行，其余行置 is_deleted = 1（软删）或清空 upstream_subject，然后重跑本脚本。\n注意先确认保留下来的那一行的 user_roles 绑定是否完整，被软删行的绑定不会自动迁移。',
            dup_groups, dup_detail;
    END IF;
END $$;

-- ------------------------------------------------------------
-- 3. (tenant_id, upstream_subject) partial unique index
--    名字对齐 users_tenant_id_external_id_key 的 `<表>_<列...>_key` 约定。
-- ------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_upstream_subject_key
    ON platform.users(tenant_id, upstream_subject)
    WHERE is_deleted = 0 AND upstream_subject IS NOT NULL;

-- ------------------------------------------------------------
-- 4. 注释留痕（与 007_comments.sql 的 COMMENT ON 风格一致，便于 \d 时看到用途）
-- ------------------------------------------------------------
COMMENT ON INDEX platform.users_tenant_id_upstream_subject_key IS
    '活跃且已回填 (tenant_id, upstream_subject) 唯一（partial，WHERE is_deleted = 0 AND upstream_subject IS NOT NULL）。支撑 UserRepository.get_by_upstream_subject 与「同一上游主体不会建重复号」的保证；未回填（NULL）与软删行不参与唯一性。';
