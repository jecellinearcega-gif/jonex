-- ============================================================
-- 企业身份同步：全量缺失「同轮复核 + 软删」的独立计数列 user_deleted（存量库增量 DDL）
-- 版本: 045
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql（identity_sync_tasks 段）
-- 依据: 「企业身份同步 · 全量缺失同轮复核 + 软删」（jonex-i9fe）
--       —— 全量缺失此前只记一条「待确认（不自动停用）」失败明细，整次同步必然
--       partial_failed 且每轮重算、不自愈。改为：发现缺失候选后当轮再拉一次 full 报文
--       复核；复核确认上游确已删除 ⇒ 平台软删该账号（is_deleted=1，保留数据、上游恢复
--       即原地复活）。
--
-- 用途
--   计数维度补齐为 user_added / user_updated / user_disabled / user_enabled /
--   user_deleted / failed_count。
--   ⚠️ 复活**不新增列**：上游恢复同一个人 ⇒ 原地复活并复用既有 user_enabled 口径，
--   前端数字个数不变。
--   写入方在 capabilities/platform/services/identity_integration_sync.py（同一线负责），
--   本脚本只补列。
--
-- 本表其余两条路径
--   ① `update/044_identity_sync_user_enabled.sql` 补过 user_enabled（同族先例，形状对齐）；
--   ② 缺列时本脚本会被 `update/031_identity_integration.sql`（建表脚本）落在后面，
--      存量库按时间序执行即得同样结构。
--
-- 不动 migrations/
--   本文件是增量脚本（`update/` 下）。`migrations/002_platform.sql` 是**全量**脚本，
--   本批已在它的建表段同步声明该列（新库建表即得），两条路得到同样的表结构。
--   由人工显式触发（`make db-migrate-dry` 预览 / `make db-migrate` 执行），
--   版本表 public.schema_migrations 按文件名追踪。
--
-- 幂等
--   `ADD COLUMN IF NOT EXISTS` 保证可重复执行：列已存在时为空操作，不报错。
-- ============================================================

ALTER TABLE platform.identity_sync_tasks
    ADD COLUMN IF NOT EXISTS user_deleted INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN platform.identity_sync_tasks.user_deleted IS
    '本次全量同步经复核确认上游已删、因而被平台软删的用户数（复活走 user_enabled）';
