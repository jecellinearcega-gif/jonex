-- ============================================================
-- 045_knowledge_info_ingest_backend.sql
-- 库级派单值：knowledge_base.knowledge_info.ingest_backend（存量库）
--
-- 背景（2026-10-09）
--   入库链路由全局 env `KB_INGEST_BACKEND` 单选，无法按库区分。本列把
--   「这个库走哪条链」下沉到库里：各库就地表态，不再共用一个全局变量。
--
-- 语义（代码侧：capabilities/knowledge_base/services/ingest_backend.py）
--   取值 `rag`（当前唯一注册的后端）；**NULL = 跟随服务端开关**
--   （存量行全 NULL ⇒ 行为与从前逐字一致）。非 NULL 即覆盖 env。
--   判据两层：库级值非 NULL 即覆盖开关；NULL 才读开关（缺省 `rag`）。
--   提交时读、写进文档行（extra_metadata.ingest_backend，粘性）；
--   改库设置只影响**下一次提交**，在途文档不受影响、无需重启任何服务。
--
-- 为什么只写 update/、不并回全量脚本
--   `004_knowledge_base.sql` 已线上发布，照先例只碰注释、不并回。
--   ⚠ 全新的库仍要跑一次 `make db-migrate`（deploy/README.md 的既有条款）。
--
-- 发版次序：**先 `make db-migrate`，再换 knowledge-base 新镜像** ——
--   反序时新代码读取 KnowledgeInfo 实体会撞上不存在的列，上传提交那一刻
--   UndefinedColumn 硬失败（比 042 那次温和，同样不值得踩）。
--
-- 回滚：`UPDATE knowledge_base.knowledge_info SET ingest_backend = NULL;`
--   （读时求值，即刻回到「跟随开关」）；或走 API 传 `null`。
--
-- 幂等: ADD COLUMN IF NOT EXISTS / COMMENT 重复执行均为空操作。
-- ============================================================

BEGIN;

ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS ingest_backend VARCHAR(32);

COMMENT ON COLUMN knowledge_base.knowledge_info.ingest_backend IS
    '[jonex] 库级入库派单值（当前唯一取值 rag）；NULL=跟随服务端开关 KB_INGEST_BACKEND（2026-10-09）';

COMMIT;
