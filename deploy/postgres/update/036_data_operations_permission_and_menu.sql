-- ============================================================
-- 036_data_operations_permission_and_menu.sql
-- 数据运营：补权限码 analytics:read + 平台管理分组菜单项
--
-- 对应全量种子: deploy/postgres/migrations/006_seed_data.sql（新库目标态）
--
-- 背景
--   数据运营页（/apps/platform-management/data-operations，三个 tab 落在 ?tab=）此前
--   只有路由、没有菜单、也没有权限码 → 侧边栏/hosted 菜单树里都没有入口，
--   角色权限页里也无法把它单独授权给某个角色。
--
--   本批做两件加法：
--     1) 新码 analytics:read（scope='tenant'）：数据运营看的是**本租户**的检索与资产
--        统计（4 个 data-operations/* 接口在无 domain_space_id 时要求租户管理员），
--        所以是租户级码 —— 平台管理员与租户管理员都拿到，普通用户拿不到。
--     2) 平台管理分组（menus.id=5）下新增菜单项「数据运营」，排在该分组的第 1 位
--        （即「账号与权限」之前），原有三项顺延一位。
--
--   边界（2026-09-23 决策）: 本批**不动后端判权**。analytics 聚合动作的准入仍是
--   tenant:admin / platform:admin（或带 domain_space_id 时的空间成员），
--   analytics:read 目前只控菜单可见性与角色授权 —— 它不是接口防线。
--
-- 幂等
--   可重复执行：permissions 有 permissions_code_key(code)、menus 有主键、
--   role_permissions 有 uq_role_permissions(tenant_id, role_id, permission_id)，
--   一律 ON CONFLICT DO NOTHING；菜单重排用「当前值 → 目标值」的条件更新，
--   第二次执行时条件已不成立，天然成为空操作。
--
-- 范围
--   只做加法 + 菜单排序。不改任何现有权限码，不撤销任何已有授权。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 权限码 analytics:read（租户级）
-- ------------------------------------------------------------
INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('analytics:read', '查看数据运营', 'analytics', 'read',
     '租户级：数据运营页可见（内容为本租户检索与资产统计）', 'tenant')
ON CONFLICT (code) DO NOTHING;

-- ------------------------------------------------------------
-- 2. 菜单项：平台管理分组下新增「数据运营」，排在账号与权限之前
--    id 29 = 现有最大 id(28) 之后的下一个（006 里同一行用同一 id）
-- ------------------------------------------------------------
INSERT INTO platform.menus (id, parent_id, name, path, icon, app_id, sort_order, permission_code) VALUES
    (29, 5, 'navigation.dataOperations', '/apps/platform-management/data-operations',
     'BarChartOutlined', NULL, 1, 'analytics:read')
ON CONFLICT (id) DO NOTHING;

-- 3. 平台管理分组原有子项顺延一位（条件更新：只在仍是旧值时改，重跑即空操作）
UPDATE platform.menus SET sort_order = 2, updated_at = NOW()
WHERE id = 25 AND parent_id = 5 AND sort_order = 1;   -- 账号与权限

UPDATE platform.menus SET sort_order = 3, updated_at = NOW()
WHERE id = 9 AND parent_id = 5 AND sort_order = 2;    -- 提示词与模板

UPDATE platform.menus SET sort_order = 4, updated_at = NOW()
WHERE id = 26 AND parent_id = 5 AND sort_order = 3;   -- 系统运维

-- ------------------------------------------------------------
-- 4. 授权：与 006 的两段播种逻辑同口径（收敛到新库目标态）
--    4a. demo 的平台管理员 + 租户管理员 ← 全部 scope='tenant' 码
--        （存量库里其余租户码早已授过，这里实际只会补上 analytics:read）
-- ------------------------------------------------------------
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'tenant'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name IN ('平台管理员', '租户管理员') AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 4b. 其余租户：按 demo 模板复制（仍只复制 scope='tenant' 码）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT t.id, nr.id, rp.permission_id
FROM platform.roles nr
JOIN platform.tenants t ON nr.tenant_id = t.id AND t.is_deleted = 0
JOIN platform.roles dr ON dr.tenant_id = 'tenant_jonex_demo' AND dr.name = nr.name AND dr.is_deleted = 0
JOIN platform.role_permissions rp ON rp.tenant_id = 'tenant_jonex_demo' AND rp.role_id = dr.id
JOIN platform.permissions p ON p.id = rp.permission_id
WHERE t.id <> 'tenant_jonex_demo' AND p.scope = 'tenant'
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 5. 菜单显式 id 插入不推进序列 → 立即对齐（同 006 收尾）
-- ------------------------------------------------------------
SELECT setval('platform.menus_id_seq', (SELECT COALESCE(MAX(id), 0) FROM platform.menus));

COMMIT;

-- ------------------------------------------------------------
-- 自查（可单独执行）
--   SELECT code, name, scope FROM platform.permissions WHERE code = 'analytics:read';
--   SELECT id, parent_id, name, sort_order, permission_code, is_deleted
--   FROM platform.menus WHERE parent_id = 5 OR id = 5 ORDER BY sort_order;
--   SELECT r.name, count(*) FROM platform.role_permissions rp
--   JOIN platform.roles r ON r.id = rp.role_id AND r.tenant_id = rp.tenant_id
--   JOIN platform.permissions p ON p.id = rp.permission_id
--   WHERE p.code = 'analytics:read' GROUP BY r.name;
-- ------------------------------------------------------------
