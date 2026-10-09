-- ============================================================
-- 企业身份集成：identity_integration_config 新增自动同步调度水位两列（存量库增量 DDL，幂等可重跑）
-- 版本: 039
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql（identity_integration_config 段）
-- 依据: 「用户与组织同步」——auto_sync_enabled / sync_interval 此前是死配置（只有读写、
--       无任何消费者），本批补上调度器（capabilities/platform/services/identity_sync_scheduler.py）
--
-- 用途
--   * last_sync_at —— 最近一次**自动**同步的发起时刻（手动「立即同步」不写它）；
--   * next_sync_at —— 本租户下一次该自动同步的时刻，也是多副本之间的 CAS 抢占条件
--                     （`WHERE id = :id AND next_sync_at IS NOT DISTINCT FROM :expected`）。
--
--   ⛔ **不写回填**：存量行两列一律留 NULL，而 NULL 的语义是「**立即到期**」（拍板 ③，
--   不是「还没排期」）—— 已打开 auto_sync_enabled 的租户在调度器起来后的第一轮就会
--   被排期并跑一次。若在这里 UPDATE 成「now + 周期」，反而会让它们白等一个周期。
--   未打开 auto_sync_enabled 的租户两列永远是 NULL，不参与扫描（见下面索引谓词）。
--
-- 不动 migrations/
--   本文件是增量 DDL，`migrations/` 下**不新增、不修改**任何 sql（见 db-migration-discipline）。
--   新库由 `migrations/` 建表后、再按文件名序应用本目录全部脚本（`make db-migrate`），
--   两条路都能得到同样的表结构。由人工显式触发（`make db-migrate-dry` 预览 /
--   `make db-migrate` 执行），版本表 public.schema_migrations 按文件名追踪。
--
-- 时区口径（为什么是 TIMESTAMP 而不是 TIMESTAMPTZ）
--   两列与同表的 datasource_connection_verified_at、与 identity_sync_tasks.started_at
--   同档，都是 `TIMESTAMP WITHOUT TIME ZONE`，写入方一律用本机朴素时间 `datetime.now()`
--   （调度器的 now_fn 默认值）。⚠️ 不要改成写 tz-aware 值：asyncpg 对 `timestamp` 参数
--   执行朴素减法（asyncpg/pgproto/codecs/datetime.pyx::timestamp_encode 里
--   `obj - datetime(2000, 1, 1)`），带 tzinfo 会直接 TypeError。本批不改口径。
--
-- 幂等
--   ADD COLUMN / CREATE INDEX 均带 IF NOT EXISTS；重复执行不报错、不重建。无数据变更
--   （见上「不写回填」），重跑永远安全。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 加列：两列均可空（NULL = 尚未自动同步过 / 尚未排期 ⇒ 立即到期）
-- ------------------------------------------------------------
ALTER TABLE platform.identity_integration_config
    ADD COLUMN IF NOT EXISTS last_sync_at TIMESTAMP;

ALTER TABLE platform.identity_integration_config
    ADD COLUMN IF NOT EXISTS next_sync_at TIMESTAMP;

COMMENT ON COLUMN platform.identity_integration_config.last_sync_at IS
    '最近一次自动同步的**发起**时刻（调度器 CAS 抢占时写入，与本行 next_sync_at 同一次 UPDATE）；手动「立即同步」不写本列。NULL = 从未自动同步过。朴素时间（TIMESTAMP 无时区），与同表 datasource_connection_verified_at 同档';

COMMENT ON COLUMN platform.identity_integration_config.next_sync_at IS
    '下一次该自动同步的时刻 = 上次发起时刻 + max(sync_interval, 1) 小时（未配 sync_interval 时用 IDENTITY_SYNC_DEFAULT_INTERVAL_HOURS）。多副本靠本列做 CAS 抢占（IS NOT DISTINCT FROM，不是 =）。NULL = **立即到期**（首次打开自动同步 / 本列刚落地），不是「未排期」';

-- ------------------------------------------------------------
-- 2. 到期扫描的部分索引
--    谓词只放**不变列** auto_sync_enabled：datasource_url 是易变列（用户在界面上
--    随时改地址），放进索引谓词等于每次改地址都要重建索引；而它只影响「这一轮扫不扫
--    这条」，放进查询的 WHERE 里过滤即可（见调度器 _scan_statement）。
--    排序列与查询的 ORDER BY 严格对齐（next_sync_at ASC NULLS FIRST）——
--    B-tree 的默认是 NULLS LAST，这里必须显式声明，否则首轮那批（全为 NULL）
--    走不到索引序、要全表排序。
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_identity_integration_config_auto_sync
    ON platform.identity_integration_config (next_sync_at ASC NULLS FIRST)
    WHERE auto_sync_enabled = true;

COMMENT ON INDEX platform.idx_identity_integration_config_auto_sync IS
    '自动同步到期扫描的支撑索引（partial，WHERE auto_sync_enabled = true）：按 next_sync_at ASC NULLS FIRST 取一批到期租户。谓词只含不变列 auto_sync_enabled；datasource_url 非空是易变条件，留在查询 WHERE 里';
