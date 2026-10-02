-- ============================================================
-- 034_repair_tenant_role_seed.sql
-- 修复新建租户失败：demo 缺预设角色模板 + 权限重构在存量库的遗留欠账
--
-- 对应全量种子: deploy/postgres/migrations/006_seed_data.sql（新库目标态）
-- 依据: docs/permissions/PERMISSIONS_REDESIGN.md §3.2 / §11.2-§11.4
--       docs/permissions/PERMISSIONS_REDESIGN_execution.md §2.3（027 原始规划）
--
-- 背景
--   账号角色由 5 个收为 3 个（平台管理员 / 租户管理员 / 普通用户）的存量库迁移脚本
--   （027_permission_codes_tenant_admin.sql、029）未随仓库保留，本机与测试环境的存量库
--   从未执行过该批数据变更，实测表现为三个问题：
--
--   1) demo 租户仍持旧结构角色，「租户管理员 / 普通用户」两个模板角色不存在
--      → capabilities/platform/services/misc_services.py::seed_tenant_roles 查不到模板
--        → 抛 InternalError → 新建租户 100% 失败（前端「平台管理 → 租户管理 → 新建租户」）
--   2) tenant:admin / space:read / space:write / audit:read 四个码全库缺失
--      → is_tenant_admin 判定恒为 false。tenant:admin 在代码中被 4 处引用：
--          capabilities/platform/auth/auth_service.py:64
--          capabilities/platform/services/admin_role_guard.py:34
--          capabilities/knowledge_base/services/space_permission_service.py:38
--          capabilities/platform/api/platform_crud.py:531
--        平台管理员有 platform:admin 上位兜底故不受影响，但**普通租户管理员的权限会整体失效**。
--        同批缺失的还有 platform:monitor:read / datasource:read / datasource:write。
--   3) engine:* / adapter:* / model:* 六个码的 scope 仍为 tenant（应为 platform）
--      → 会被「按 scope 复制租户业务码」的播种逻辑错误地复制给每个租户。
--
-- 幂等
--   全部语句可重复执行。注意 platform.roles **没有** (tenant_id, name) 唯一约束，
--   ON CONFLICT 在 role 表上无效，故角色插入一律用 NOT EXISTS 自保。
--   role_permissions 有 uq_role_permissions(tenant_id, role_id, permission_id)，
--   permissions 有 permissions_code_key(code)，这两处用 ON CONFLICT DO NOTHING。
--
-- 范围
--   本批**只做加法**（补码、补角色、补绑定）。目标态要求的删除类动作（废弃码、
--   降级角色软删、旧角色 platform 码解绑）全部留给下一批，见文末「未覆盖项」。
--   因此脚本不会撤销任何现有用户的既有权限。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 0. 备份用户-角色绑定
--    本脚本对 user_roles 只增不减，备份用于人工复核与回滚对照。
--    IF NOT EXISTS 保证只快照首次执行前的状态，重跑不覆盖。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform.user_roles_bak_034 AS
SELECT * FROM platform.user_roles;

-- ------------------------------------------------------------
-- 1. 补缺失的 7 个权限码
-- ------------------------------------------------------------
INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('tenant:admin', '租户管理员', 'tenant', 'admin',
     '租户管理员标识码（纯标识，不做业务准入）；仅平台管理员可授予', 'tenant'),
    ('space:read', '查看领域空间', 'space', 'read',
     '领域空间菜单与设置页可见（内容按资源身份过滤）', 'tenant'),
    ('space:write', '管理领域空间', 'space', 'write',
     '创建/管理领域空间、指定领域管理者（租户级能力）', 'tenant'),
    ('audit:read', '查看审计日志', 'audit_log', 'read',
     '租户级：仅本租户操作与调用记录', 'tenant'),
    ('datasource:read', '查看数据源', 'datasource', 'read',
     '平台级：统一数据源目录', 'platform'),
    ('datasource:write', '管理数据源', 'datasource', 'write',
     '平台级：数据源与连接配置', 'platform'),
    ('platform:monitor:read', '查看系统监控', 'monitor', 'read',
     '平台级：全平台服务/任务/组件运行状态', 'platform')
ON CONFLICT (code) DO NOTHING;

-- ------------------------------------------------------------
-- 2. scope 收敛：解析器/适配器/模型是系统级能力，两处功能页只有平台管理员在用
--    （D7 决策 + 2026-08-29 model 决策）。上面第 1 节用的是 DO NOTHING，
--    对**已存在**的码不会改 scope，所以这里显式 UPDATE 一次。
--    显式列码名而非 LIKE：将来新增 engine:*/model:* 之外的码时，否定式条件会过期。
-- ------------------------------------------------------------
UPDATE platform.permissions SET scope = 'platform', updated_at = NOW()
WHERE code IN ('engine:read', 'engine:write', 'adapter:read', 'adapter:write', 'model:read', 'model:write')
  AND scope <> 'platform';

-- ------------------------------------------------------------
-- 3. demo 租户补两个预设角色模板
--    is_system = 0：仅 demo 的「平台管理员」受系统角色保护，
--    租户自己的「租户管理员 / 普通用户」是普通角色，可按需调整菜单码。
--    不指定 id 由序列分配：存量库 id 2-5 / 6-9 / 10-13 已被旧结构角色占用
--    （新库 006 用的是 id 2 与 id 6），故两种库的角色 id 不同名同义，属预期。
-- ------------------------------------------------------------
INSERT INTO platform.roles (tenant_id, name, description, is_system)
SELECT v.tenant_id, v.name, v.description, 0
FROM (VALUES
    ('tenant_jonex_demo', '租户管理员',
     '租户内管理员：全部租户业务码（不含平台面码）；标识码 tenant:admin'),
    ('tenant_jonex_demo', '普通用户',
     '账号角色兜底态：只持业务菜单码；实际业务能力完全由领域/知识库资源身份决定')
) AS v(tenant_id, name, description)
WHERE NOT EXISTS (
    SELECT 1 FROM platform.roles r
    WHERE r.tenant_id = v.tenant_id AND r.name = v.name AND r.is_deleted = 0
);

-- ------------------------------------------------------------
-- 4. demo 角色-权限重配（目标态口径，见执行计划 §2.3 步骤 2）
-- ------------------------------------------------------------

-- 4.1 平台管理员 = 全部 platform 码（补齐新增的 datasource:* 与 platform:monitor:read）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'platform'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name = '平台管理员' AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 4.2 平台管理员 + 租户管理员 = 全部 tenant 码（租户管理员**不含任何 platform 码**）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'tenant'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name IN ('平台管理员', '租户管理员') AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 4.3 普通用户 = 3 个纯菜单可见码。
--     这 3 个码只控前端菜单与路由可见性，后端一律不用它们做数据准入；
--     菜单可见性不能交给资源身份，否则用户连入口都看不到。
--     space:read 在此不是笔误：普通用户靠它进入空间列表页，页面内容按资源身份过滤；
--     「能不能建空间/改设置」由 space:write 控制，普通用户没有。
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.code IN ('knowledge:read', 'service:read', 'space:read')
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name = '普通用户' AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 5. 其余租户补齐两个角色 + 权限映射（与 006「每租户预设角色播种」同构）
--    只处理这两个目标角色名，不动各租户遗留的旧结构角色 —— 本批不改现有角色权限。
-- ------------------------------------------------------------

-- 5.1 补角色（描述与 demo 模板一致，保持三处定义同源）
INSERT INTO platform.roles (tenant_id, name, description, is_system)
SELECT t.id, d.name, d.description, 0
FROM platform.tenants t
CROSS JOIN (
    SELECT name, description FROM platform.roles
    WHERE tenant_id = 'tenant_jonex_demo' AND is_deleted = 0
      AND name IN ('租户管理员', '普通用户')
) d
WHERE t.id <> 'tenant_jonex_demo' AND t.is_deleted = 0
  AND NOT EXISTS (
      SELECT 1 FROM platform.roles x
      WHERE x.tenant_id = t.id AND x.name = d.name AND x.is_deleted = 0
  );

-- 5.2 按 demo 同名角色复制权限（仅 scope='tenant' 的码）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT t.id, nr.id, rp.permission_id
FROM platform.roles nr
JOIN platform.tenants t ON nr.tenant_id = t.id AND t.is_deleted = 0
JOIN platform.roles dr ON dr.tenant_id = 'tenant_jonex_demo' AND dr.name = nr.name AND dr.is_deleted = 0
JOIN platform.role_permissions rp ON rp.tenant_id = 'tenant_jonex_demo' AND rp.role_id = dr.id
JOIN platform.permissions p ON p.id = rp.permission_id
WHERE t.id <> 'tenant_jonex_demo'
  AND nr.name IN ('租户管理员', '普通用户')
  AND p.scope = 'tenant'
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 6. 用户-角色绑定补齐（追加，不删除任何现有绑定）
-- ------------------------------------------------------------

-- 6.1 role='admin' 的租户内管理员 → 追绑「租户管理员」。
--     demo 的 admin 账号排除在外：它绑的是「平台管理员」，platform:admin 是其上位。
--     做这一步的原因：只建角色不绑人，「租户管理员」形同虚设，tenant:admin 码补了也没人生效。
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r
  ON r.tenant_id = u.tenant_id AND r.name = '租户管理员' AND r.is_deleted = 0
WHERE u.is_deleted = 0 AND u.role = 'admin'
  AND NOT (u.tenant_id = 'tenant_jonex_demo' AND u.username = 'admin')
  AND NOT EXISTS (
      SELECT 1 FROM platform.user_roles ur
      WHERE ur.tenant_id = u.tenant_id AND ur.user_id = u.id AND ur.role_id = r.id
  );

-- 6.2 兜底：任何没有任何角色绑定的有效用户 → 「普通用户」。
--     零角色用户连菜单都看不见，且现象与权限缓存问题难以区分，必须兜住。
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r
  ON r.tenant_id = u.tenant_id AND r.name = '普通用户' AND r.is_deleted = 0
WHERE u.is_deleted = 0
  AND NOT EXISTS (
      SELECT 1 FROM platform.user_roles ur
      WHERE ur.tenant_id = u.tenant_id AND ur.user_id = u.id
  );

-- ------------------------------------------------------------
-- 7. 菜单权限码对齐（改菜单码必须与接口同批，否则「接口通了但没有入口」）
-- ------------------------------------------------------------

-- 7.1 数据源管理：原先与「解析器管理」共用 engine:read，两个不同页面共享一个码
--     导致无法分别授权，改用专属的 datasource:read。
UPDATE platform.menus SET permission_code = 'datasource:read', updated_at = NOW()
WHERE name = 'navigation.dataAccess' AND permission_code <> 'datasource:read';

-- 7.2 操作日志：菜单码从 platform:audit:read 改为 audit:read（租户级）。
--     接口侧已改成双码放行（capabilities/platform/api/audit_logs.py:39），
--     但菜单码若仍是平台码，租户管理员会被过滤掉这一项 → 组头「系统运维」也被连带裁掉
--     → 「接口通了但没有入口」。平台管理员天然持全部 tenant 码，仍看得到。
UPDATE platform.menus SET permission_code = 'audit:read', updated_at = NOW()
WHERE name = 'navigation.operationLog' AND permission_code <> 'audit:read';

COMMIT;

-- ------------------------------------------------------------
-- 8. 验收断言（三条均应返回 0）
-- ------------------------------------------------------------

-- 8.1 没有有效用户落到零角色
SELECT '8.1 零角色用户' AS check_name, count(*) AS should_be_zero
FROM platform.users u
WHERE u.is_deleted = 0
  AND NOT EXISTS (
      SELECT 1 FROM platform.user_roles ur
      WHERE ur.tenant_id = u.tenant_id AND ur.user_id = u.id
  );

-- 8.2 每个「租户管理员」角色都持 tenant:admin
SELECT '8.2 缺 tenant:admin 的租户管理员' AS check_name, count(*) AS should_be_zero
FROM platform.roles r
WHERE r.name = '租户管理员' AND r.is_deleted = 0
  AND NOT EXISTS (
      SELECT 1 FROM platform.role_permissions rp
      JOIN platform.permissions p ON p.id = rp.permission_id
      WHERE rp.role_id = r.id AND rp.tenant_id = r.tenant_id AND p.code = 'tenant:admin'
  );

-- 8.3 本批建立的两个目标角色不得持任何 platform 码。
--     注：执行计划 §2.4 的原版断言覆盖**全部**角色，但存量库三个降级角色尚未软删
--     （系统管理员等仍持有 engine:*/adapter:*/model:*），全量版当前必然非 0。
--     该清理属下一批（见「未覆盖项」②③），本处只校验本批的目标角色。
SELECT '8.3 目标角色越权持平台码' AS check_name, count(*) AS should_be_zero
FROM platform.role_permissions rp
JOIN platform.roles r ON r.id = rp.role_id AND r.tenant_id = rp.tenant_id
JOIN platform.permissions p ON p.id = rp.permission_id
WHERE p.scope = 'platform'
  AND r.is_deleted = 0
  AND r.name IN ('租户管理员', '普通用户');

-- 8.4 每个租户都有两个模板角色（新建租户成功的前提）
SELECT '8.4 缺模板角色的租户' AS check_name, count(*) AS should_be_zero
FROM platform.tenants t
WHERE t.is_deleted = 0 AND t.id <> 'tenant_jonex_demo'
  AND EXISTS (
      SELECT 1 FROM (VALUES ('租户管理员'), ('普通用户')) AS v(name)
      WHERE NOT EXISTS (
          SELECT 1 FROM platform.roles r
          WHERE r.tenant_id = t.id AND r.name = v.name AND r.is_deleted = 0
      )
  );

-- ------------------------------------------------------------
-- 9. 未覆盖项（本批有意不做，需另行拍板）
-- ------------------------------------------------------------
-- ① 5 个后端零引用的废弃码未删：tenant:read / knowledge:write / service:write /
--    system:read / system:write。留着不构成漏洞，但会让角色权限页出现
--    「配了也没用」的勾选项。删除需先清 role_permissions 绑定。
-- ② 三个降级角色未软删（领域服务管理员 / 知识编辑者 / 观察者），
--    现有用户绑定也未迁移到「普通用户」。删它们会让 4-5 个 demo 用户的旧绑定失效。
-- ③ 旧结构角色（系统管理员等）持有的 platform 码绑定未清理 —— 即 8.3 的全量版断言。
-- ④ tenant:write 保留不删：它的删除必须在 space_permission_service 已改看
--    tenant:admin 并稳定运行后单独执行，顺序错了会让所有租户管理员判定瞬间失效。
-- ⑤ 空间/知识库资源身份迁移（space_permissions.role 的 manager/viewer →
--    space_manager/member，kb_permissions 对应改造）属 B2/B3 批次，与本脚本无关。
-- ⑥ 代码侧待订正：capabilities/platform/services/misc_services.py:362 的报错文案
--    指向 deploy/postgres/update/027_permission_codes_tenant_admin.sql 第 4 节，
--    而该文件不在仓库中（git log 该路径无历史）。本脚本即其等价实现，
--    文案应改指本文件，否则下一个排查者仍会被指向死文件。
