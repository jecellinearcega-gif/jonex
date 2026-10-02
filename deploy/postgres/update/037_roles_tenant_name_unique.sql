-- ============================================================
-- platform.roles 补 (tenant_id, name) partial unique index（存量库增量 DDL，幂等可重跑）
-- 版本: 037
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql（RBAC 角色表段）
-- 依据: SSO 首登 / 企业身份同步建号后需按名绑「普通用户」角色，该方法因此进入登录与同步关键链路
-- 命名对齐: 002_platform.sql:61 的 users_tenant_id_username_key（同类 partial unique，软删隔离）
--
-- 用途
--   002 建 platform.roles 时只给了 roles_pkey 与 idx_roles_tenant(tenant_id)，**没有**
--   (tenant_id, name) 唯一约束。034 头部注释第 29 行已记载这一事实（"ON CONFLICT 在 role 表
--   上无效，故角色插入一律用 NOT EXISTS 自保"），006_seed_data.sql:265 的
--   `ON CONFLICT DO NOTHING` 在 role 表上同样是空转。
--
--   后果有两层：
--     1) 数据层：靠 name 对齐的播种/复制语句（006:279/290/299、034:155/157）一旦库里有
--        同名重复行，JOIN 会**成倍放大**插入行 —— 多建角色、多绑权限、多绑用户角色。
--     2) 代码层：RoleRepository.get_by_name()（capabilities/platform/repository/role_repository.py:14-23）
--        用 result.scalar_one_or_none()。同名重复时 SQLAlchemy 抛 MultipleResultsFound。
--        该方法当前只被角色管理的 create/update（role_service.py:50/96）调用，炸掉影响面有限；
--        但同批落地的建号绑角色修复要把它放进 **SSO 首登与企业身份同步建号** 的关键链路（建号后
--        按名绑「普通用户」角色），届时任一租户存在同名角色 → 登录与同步整条链路 500。
--
--   已拍板的处置方向是**加唯一约束、不改查询**：约束在数据层把「同名重复」这个状态变成不可表达，
--   比在代码层做去重分流更省、更不容易漏调用点。
--
-- 语义（为什么是 partial index 而非普通 unique 约束）
--   带 `WHERE is_deleted = 0`，即**只对活跃行**要求 (tenant_id, name) 唯一。正确性在于：
--   该表是软删模型 —— 删掉「临时管理员」再重建一个同名角色，是合法且常见的运维动作
--   （role_service.delete 走 is_deleted 而非物理删除）。若唯一性覆盖已软删行，这个动作就会
--   被约束挡住，等于把软删语义在角色表上打折。partial 让「同名只可能有一个活着」成立，
--   同时历史软删行不影响任何新写入 —— 与 users_tenant_id_username_key 的取舍完全一致。
--
--   注意 NULL：is_deleted 列有 DEFAULT 0，但历史行若为 NULL，`NULL = 0` 不为真，该行既不进
--   索引、也不会被 get_by_name 的 `is_deleted == 0` 谓词命中 —— 两边谓词严格对齐，
--   不存在「索引不管、查询能查到」的缝隙。
--
-- 幂等
--   建索引用 IF NOT EXISTS；重复执行不会报错、不会重建。前置检查块在无重复时静默通过。
--   本文件是增量 DDL，不在 migrations/ 新增或修改任何 sql；由人工触发
--   （make db-migrate-dry 预览 / make db-migrate 执行），版本表按文件名追踪。
--
-- 前置条件（无法由脚本代替人工判断）
--   脚本会自检活跃重复行，**有重复则 RAISE EXCEPTION 中止**（不登记版本、修复后可重跑），
--   报错信息会列出冲突的 tenant_id / name。人工处置口径见脚本内 HINT。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 前置检查：活跃（is_deleted = 0）同名角色必须为空集
--    否则第 2 步的 CREATE UNIQUE INDEX 会失败于裸的 Postgres 错误（23505 + 重复键值），
--    信息量不足以定位到具体租户/角色名。这里先自检并给出中文原因与冲突清单。
--    无重复时为 no-op，可重复执行。
-- ------------------------------------------------------------
DO $$
DECLARE
    dup_groups INTEGER;
    dup_detail TEXT;
BEGIN
    SELECT COUNT(*), string_agg(
               format('tenant_id=%L  name=%L  重复 %s 行', d.tenant_id, d.name, d.cnt),
               E'\n  ' ORDER BY d.tenant_id, d.name)
      INTO dup_groups, dup_detail
      FROM (
          SELECT tenant_id, name, COUNT(*) AS cnt
            FROM platform.roles
           WHERE is_deleted = 0
           GROUP BY tenant_id, name
          HAVING COUNT(*) > 1
      ) d;

    IF dup_groups > 0 THEN
        RAISE EXCEPTION E'platform.roles 存在 % 组活跃（is_deleted = 0）同名角色，无法建立 (tenant_id, name) 唯一索引。\n冲突清单：\n  %\n处置：每个 (tenant_id, name) 只保留一行，其余行置 is_deleted = 1（软删）或改名，然后重跑本脚本。\n注意先确认保留下来的那一行的 role_permissions / user_roles 绑定是否完整，被软删行的绑定不会自动迁移。',
            dup_groups, dup_detail;
    END IF;
END $$;

-- ------------------------------------------------------------
-- 2. (tenant_id, name) partial unique index
--    名字对齐 users_tenant_id_username_key 的 `<表>_<列...>_key` 约定。
--    与 002 的 users 那条同构：同表、同谓词、同风格。
-- ------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS roles_tenant_id_name_key
    ON platform.roles(tenant_id, name) WHERE is_deleted = 0;

-- ------------------------------------------------------------
-- 3. 注释留痕（与 007_comments.sql 的 COMMENT ON 风格一致，便于 \d 时看到用途）
-- ------------------------------------------------------------
COMMENT ON INDEX platform.roles_tenant_id_name_key IS
    '活跃角色 (tenant_id, name) 唯一（partial，WHERE is_deleted = 0）。支撑 RoleRepository.get_by_name 的 scalar_one_or_none 与 SSO 首登/身份同步的按名绑角色；软删行不参与唯一性。';
