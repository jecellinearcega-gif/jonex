-- ============================================================
-- 企业身份同步：启用路径独立计数列 user_enabled（存量库增量 DDL）
-- 版本: 044
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql（identity_sync_tasks 段）
--               另同步回填既有增量脚本 deploy/postgres/update/031_identity_integration.sql
-- 依据: 「企业身份同步 · 启用路径对称化」——
--       启用路径此前没有独立计数口径（`_update_existing_user` 里
--       `new_status == 1 and existing.status != 1` 只把 user_updated +1），
--       本列与 user_disabled 对称，单独记「本次同步启用（重新启用）了多少用户」。
--
-- 用途
--   计数维度补齐为 user_added / user_updated / user_disabled / user_enabled / failed_count。
--   写入方在 capabilities/platform/services/identity_integration_sync.py（另一线负责），
--   本脚本只补列。
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
    ADD COLUMN IF NOT EXISTS user_enabled INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN platform.identity_sync_tasks.user_enabled IS
    '本次同步启用（重新启用）的用户数（与 user_disabled 对称）';
