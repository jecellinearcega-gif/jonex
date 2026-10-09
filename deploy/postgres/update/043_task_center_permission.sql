-- ============================================================
-- 043_task_center_permission.sql
-- 任务中心两个权限码（存量库）
--
-- 对应全量种子: deploy/postgres/migrations/006_seed_data.sql
-- 新库由 006 直接播种（插码 + 授权都在里面），本脚本只服务已跑过 006 的存量库。
--
-- 为什么存量库要单独补
--   这两个码 scope='tenant'，而 006 里两段授权块都是**按 scope 驱动**的
--   （demo 的管理员角色、以及各租户按 demo 模板复制），新库跑 006 时会被自动覆盖。
--   但存量库当初跑 006 时这两个码还不存在，ON CONFLICT DO NOTHING 也不会回头补 ——
--   所以这里把「插码」和「授权」一起做一遍。
--
-- 有意不做的事
--   任务中心入口码从借用的 platform:task:read 换成 task_center:read，只被授了前者的
--   自定义角色会**失去入口**。这里**不**把旧码的授权复制到新码：那会把「任务调度」
--   与「任务中心」两个页面的授权悄悄绑在一起，与本次「分开」的目标相反。
--   存量库若确有此类角色，人工补授。
--
-- 幂等: ON CONFLICT DO NOTHING。重复执行是空操作。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 两个新码
--    resource 用 task_center 而非 task，与「任务调度」的 task_schedule 分开 —— 两个页面。
--    分组不落数据：角色权限页按现有兜底口径把它们归入「业务功能」。
-- ------------------------------------------------------------
INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('task_center:read', '查看任务中心', 'task_center', 'read', '租户级：任务中心页面与两个只读接口', 'tenant'),
    ('task_center:write', '管理任务中心', 'task_center', 'write', '租户级：停止任务（task.stop）', 'tenant')
ON CONFLICT (code) DO NOTHING;

-- ------------------------------------------------------------
-- 2. 预置授权（照 034 / 036 的两段播种写法）
-- ------------------------------------------------------------

-- 2a. demo 的平台管理员 + 租户管理员 ← 全部 scope='tenant' 码
--     （存量库里其余租户码早授过，这里实际只会补上 task_center:read/write）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'tenant'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name IN ('平台管理员', '租户管理员') AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 2b. 其余租户：按 demo 模板复制（仍只复制 scope='tenant' 码）
--     demo 的「普通用户」没拿到新码，所以这里也不会把它复制出去 —— 正是想要的。
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
-- 3. 自检（只告警、不中断迁移）
-- ------------------------------------------------------------
-- ⚠️ **匹配 0 行也算成功**（`INSERT … SELECT` 的语义），所以这里必须吼一声：
-- 上面 2a 是**按角色名**找角色的。若这个库里没有叫「平台管理员」/「租户管理员」的角色
-- （角色名不同，或别的环境还没建它），迁移会照常跑完、照常登记进版本表，而
-- `task_center:read/write` **谁都没绑上** —— 那两个只读接口与四个写接口对谁都回 3006，
-- 而且**没有任何报错线索**。
-- 这里**只 RAISE WARNING、不中断迁移**：让跑迁移的人当场看见，再照抬头核对角色名。
DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM platform.role_permissions rp
        JOIN platform.permissions p ON p.id = rp.permission_id
       WHERE p.code = 'task_center:read'
  ) THEN
    RAISE WARNING '043: task_center:read/write 没有任何角色持有 —— 请核对是否存在名为「平台管理员」/「租户管理员」的角色（口径见本脚本抬头）';
  END IF;
END $$;

COMMIT;
