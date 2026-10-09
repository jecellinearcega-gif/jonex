-- ============================================================
-- 知识库：接入任务中心的两张协调表（增量 DDL，幂等可重跑）
-- 版本: 042（沿革：`038_ontology_task_center.sql` →(2026-09-29)→ `037_knowledge_task_center.sql`
--       →(2026-09-30，避开 dev 的 `037_roles_tenant_name_unique.sql`)→ `041`
--       → `043` →(2026-10-08)→ **`042`**。
--       **每次改名都不动内容**；脚本整体幂等（`CREATE ... IF NOT EXISTS` / `COMMENT`），重复执行安全）
-- 全量 DDL: **不同步**进 deploy/postgres/migrations/004_knowledge_base.sql
--   （004 与 008_task_center.sql 一样已线上发布，按纪律 DDL 一律不再动。全新卷建库后
--    那两张表不存在，必须补跑一次 `make db-migrate` —— 与 `008_task_center.sql` 同一个模式，
--    那段 ⚠️ 见 deploy/README.md「PostgreSQL 迁移」）
-- 依据: docs/platform-infra/ontology-task-center-integration.md（落地设计文档）
--       docs/superpowers/specs/2026-09-09-task-center-design.md §4.5（运行方接入约定）
--
-- 本脚本**只管知识库侧的两张表**（`knowledge_base` schema，不在 `task_center` 里）：
-- 「哪份文档被哪个任务接管」与「执行登记」。任务中心那四张预置表（四条编排链的目录 /
-- 步骤清单 / 定义 / 版本）**不在这里** —— 它们在 `migrations/008_task_center.sql` 里
-- （2026-09-29 从本脚本搬过去：`008_task_center.sql` 现在是 `task_center` schema 的唯一全量脚本，
-- 线上按「删表重建」对齐；搬进去的还有原 `update/037` 那两列）。本脚本的身份也变了：
-- 2026-09-29 从「038_ontology_task_center.sql」改名为「037_knowledge_task_center.sql」，
-- 2026-09-30 再由「037」改为「041_knowledge_task_center.sql」—— 后者是为了避开与 dev 分支
-- 带进来的 `037_roles_tenant_name_unique.sql` 撞号（同号在本仓库不致命：`make db-migrate`
-- 的迁移器按**完整文件名**记版本，不会漏跑；但两个 `037` 摆在一个目录里太容易看错）。
--
-- ⚠️ 于是本文件**不再含任何 `task_center` 的数据/结构变更** —— 改那四张预置表请改 `008_task_center.sql`。
--
-- 三条编排的形状、以及「Ontology 链 2026-09-29 由三步改两步」的来龙去脉，
-- 都写在 `008_task_center.sql` 的预置数据段里。
--
-- 两张表都是**协调信息**，不是业务数据：文档自身状态（knowledge_documents.status /
-- ontology_status）口径一个字不改。为什么不往 knowledge_documents 加列 + JSONB 块：
--   ① 对账/巡检跳过必须能下沉成 SQL。写成 `NOT EXISTS` 子查询才是**在 SQL 里排除**；
--      写成「先取一批、再在 Python 里 continue」的话，一个全是接管文档的知识库会把
--      limit=50 的扫描额度吃光，真正需要救的文档排不进来（既有对账循环恒开、30 秒一轮）。
--   ② 每步记账是一次 CAS UPDATE（WHERE document_id=:doc AND execution_id=:exec），
--      而 JSONB 列在 SQLAlchemy 里不追踪就地修改（既有代码一律整块替换赋值），
--      链上每步都要写这笔账，并发下整块替换必丢更新。
--   ③ remove_task 只拿得到 task_id（派活/删除报文里就 tenant_id / space_id / task_id
--      三件套），独立表建唯一索引即可，不动 knowledge_documents 这张热表。
--
-- 两份状态（接管行 + 文档行）的代价是「可能不一致」。对冲它的只有一条纪律，写在
-- capabilities/knowledge_base/models/task_center.py 的模块 docstring 里：
-- **凡是同时改两者的地方，必须在同一次 get_db_session()、同一次 commit 里改完。**
--
-- 全部 IF NOT EXISTS、可反复执行。**本脚本没有并回 004 那份全量装库脚本**
-- （`004_knowledge_base.sql` 已线上发布、DDL 不再动），所以全新卷建库之后必须补跑一次
-- `make db-migrate` —— 「新库与老库终态一致」由这条命令保证，不由 004 保证。
-- （⚠️ 任务中心那四张预置表不适用这条：它们现在由 `migrations/008_task_center.sql`
-- 全量给出，不走本脚本。）
-- ============================================================

-- 一、knowledge_base.task_center_documents —— 接管行（一行 = 一份被接管的文档）
-- ------------------------------------------------------------
-- 主键取 document_id（不是自增 id）：查它的人（对账跳过、用户接管拦截、看门狗、三个
-- handler）**手上都只有文档 id**，按它直接命中最省事，也天然做到「一份文档在链路上只
-- 可能有一个位置」。代价要写清楚：**二次进链会覆盖旧行、历史不可追溯**（当前不做流水表，
-- 需要审计时另起一张，不要在这一行的列上堆）。
--
-- 不加外键：本表是协调信息。文档被**软删**（is_deleted=1）后接管行要留着给 remove_task
-- 反查；加了 ON DELETE CASCADE 的外键反而会在硬删文档（清理测试产物）时把接管行带走、
-- 让 remove_task 变成未接管（虽然结果同样是 no-op，但那是巧合而非设计）。
CREATE TABLE IF NOT EXISTS knowledge_base.task_center_documents (
    document_id         VARCHAR(64)  NOT NULL,
    tenant_id           VARCHAR(64)  NOT NULL,
    knowledge_base_id   VARCHAR(128) NOT NULL,
    space_id            VARCHAR(64)  NOT NULL,
    task_id             VARCHAR(64)  NOT NULL,
    operator_id         VARCHAR(64),
    state               VARCHAR(16)  NOT NULL DEFAULT 'submitting',
    current_step        VARCHAR(32),
    execution_id        VARCHAR(64),
    handled_execution_id VARCHAR(64),
    rag_task_id         VARCHAR(128),
    step_started_at     TIMESTAMP WITHOUT TIME ZONE,
    submit_attempts     INTEGER      NOT NULL DEFAULT 0,
    error_message       TEXT,
    created_at          TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_kb_tc_documents PRIMARY KEY (document_id)
);

-- remove_task 只拿得到 task_id，按它反查接管行。唯一：task_id 由任务中心服务端
-- uuid4 生成，一份文档一条任务，不可能两条接管行共用一个。
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_tc_doc_task
    ON knowledge_base.task_center_documents (task_id);

-- 看门狗每轮取「state='managed' 或在途」的行来盯终态。
CREATE INDEX IF NOT EXISTS ix_kb_tc_doc_state
    ON knowledge_base.task_center_documents (state);

CREATE INDEX IF NOT EXISTS ix_kb_tc_doc_kb
    ON knowledge_base.task_center_documents (knowledge_base_id);

-- 租户索引：本表**没有**「只按租户过滤」的查询（点查走主键、库级拦截走上面的
-- `ix_kb_tc_doc_kb`），性能上不是必需 —— 建它是为了与本 schema 的惯例一致
-- （`004` 里每张表都有一条 `idx_kb_*_tenant`），以及**堵住 ORM 那条自动命名的索引**：
-- `TenantMixin.tenant_id` 带 `index=True`，生成的名字是
-- `ix_knowledge_base_task_center_documents_tenant_id`，与这里不同名 → 「`create_all`
-- 先建表 + 本脚本再补」会在同一列上留下两条索引。所以模型侧改为自己声明这一列、并把
-- 索引名写进 `__table_args__`（见 models/task_center.py）；两边名字由测试钉死。
CREATE INDEX IF NOT EXISTS idx_kb_tc_doc_tenant
    ON knowledge_base.task_center_documents (tenant_id);

COMMENT ON TABLE knowledge_base.task_center_documents IS
    'Ontology 库文档的任务中心接管行：一份文档交给了任务中心就有一行，记录走到哪一步。与 knowledge_documents 的写入必须在同一事务内（见 capabilities/knowledge_base/models/task_center.py）';
COMMENT ON COLUMN knowledge_base.task_center_documents.document_id IS
    '被接管的文档 id，即 knowledge_base.knowledge_documents.id。主键——一份文档在链路上只可能有一个位置；代价是二次进链会覆盖旧行';
COMMENT ON COLUMN knowledge_base.task_center_documents.space_id IS
    '提交给任务中心时用的空间（init_task 会经 knowledge_base.spaces 校验它属于本租户）。存下来是因为「后台重新提交」与「看门狗判死」两条路都不在请求上下文里，拿不到知识库的 space_id';
COMMENT ON COLUMN knowledge_base.task_center_documents.task_id IS
    '任务中心 tasks.id（由**本侧**提前 uuid4 生成后自带提交，见 SDK 的提交重试约定——不带 task_id 服务端不做幂等去重，重发就是新建第二条任务）。唯一索引供 remove_task 反查';
COMMENT ON COLUMN knowledge_base.task_center_documents.operator_id IS
    '发起上传的 user_id（落 task_center.tasks.created_by 的语义）。**可空是合法状态**：内部链路 / 拿不到身份时为空，看门狗重新提交时原样带上';
COMMENT ON COLUMN knowledge_base.task_center_documents.state IS
    'submitting（已提交给任务中心但还没确认建没建成，看门狗会补试）/ managed（这条链在跑、或在等下一步派活）/ released（交接完成：任务中心不再管这份文档，接管行交还给既有对账）。只有三态——用户中途接管是「拦住用户、让他等」，没有任何动作会产生 revoked';
COMMENT ON COLUMN knowledge_base.task_center_documents.current_step IS
    '当前在跑哪一步：parse / compile（现役两条编排只用这两个；改版前遗留的三步任务还会有 ingest —— 运行方代码里三个都认）';
COMMENT ON COLUMN knowledge_base.task_center_documents.execution_id IS
    '本步的受理凭证（任务中心生成），上报终态时原样带回';
COMMENT ON COLUMN knowledge_base.task_center_documents.handled_execution_id IS
    '最后一次上报过终态的那次执行编号（值来自同一句 UPDATE 里那个被清空的 execution_id，所以本步报完之后这一对恒为 (NULL, 刚报的那次)）。用途：① 审计——回答「现在这个文档状态是哪一次执行做出来的」；② mark_step_handled 的 WHERE 条件 handled_execution_id IS DISTINCT FROM :execution_id 兜住「上报与清空之间进程崩掉、下一轮又拿同一行去报」的残余窗口（第二次 rowcount=0）。看门狗自己不读这一列，也没有「handled == execution」这种分档';
COMMENT ON COLUMN knowledge_base.task_center_documents.rag_task_id IS
    '本步发给下游的任务号（三条链都发给 atomic-rag）。**每步完成后清空**——让下一轮的「有 execution_id、无 rag_task_id」天然只衡量任务中心的派活延迟，而不是下游的执行时长（两个完全不同的东西不能共用一个阈值）';
COMMENT ON COLUMN knowledge_base.task_center_documents.step_started_at IS
    '看门狗的计时基准。**不是 created_at**——那是「接管开始」的时刻，而每步的活是从「本步开始」算的，共用一个基准会把前一步的耗时算到后一步头上';
COMMENT ON COLUMN knowledge_base.task_center_documents.submit_attempts IS
    '重新提交的次数（state=submitting 卡住时看门狗会补试，用同一个 task_id，撞 1006 即 REUSED）';
COMMENT ON COLUMN knowledge_base.task_center_documents.error_message IS
    '判死原因 / 最后一次提交失败的原因，只做排障留痕';

-- 二、knowledge_base.task_execution_registry —— 执行登记（ExecutionRegistry 端口的持久实现）
-- ------------------------------------------------------------
-- 「这个 execution_id 我接过没有」。任务中心**确实会重派**同一个 execution_id（出站没够着
-- 运行方时当轮同步重发，最多 3 次；DELIVERY_UNKNOWN 的判据见 jonex_core/task_protocol/results.py）。
-- 认不出重派就会**重复干活**——重复建下游 atomic-rag 任务、重复扣配额、产出重复向量。
-- SDK 默认给的 InMemoryExecutionRegistry **进程重启即全丢、多副本之间互相看不见**，
-- 而任务一跑就是几十分钟（RAG_TASK_HARD_TIMEOUT_SEC 默认 21600 秒），重启窗口真实存在。
-- 所以本表是上生产的必要条件，知识库启动时会自检 registry.persistent。
--
-- 键只取 execution_id：它是任务中心用 uuid4() 生成的、事实上全局唯一，所以这一行
-- **没有租户过滤、也没有软删**。tenant_id / space_id / task_id / step_code 只做排障留痕。
--
-- 原子 claim 压成一条语句（实现见 repository/execution_registry_repository.py）：
--   INSERT ... ON CONFLICT (execution_id) DO UPDATE
--      SET released_at = NULL, ... WHERE ...released_at IS NOT NULL
--   RETURNING execution_id;
-- 返回行 = NEW（领到了），0 行 = ALREADY_TAKEN（别人正持有）。
-- **「已释放的可再占」是重发安全性的前提**：重发时 execution_id 不变，我们必须能重新接下它，
-- 否则重发全被自己人挡住、子任务永远卡在 running。
CREATE TABLE IF NOT EXISTS knowledge_base.task_execution_registry (
    execution_id VARCHAR(64) NOT NULL,
    tenant_id    VARCHAR(64),
    space_id     VARCHAR(64),
    task_id      VARCHAR(64),
    step_code    VARCHAR(32),
    claimed_at   TIMESTAMP WITHOUT TIME ZONE,
    released_at  TIMESTAMP WITHOUT TIME ZONE,
    claim_count  INTEGER     NOT NULL DEFAULT 1,
    created_at   TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_kb_tc_execution_registry PRIMARY KEY (execution_id)
);

COMMENT ON TABLE knowledge_base.task_execution_registry IS
    '任务执行登记（ExecutionRegistry 端口的持久实现）：这个 execution_id 接没接过。认不出重派就会重复干活，而默认的内存实现重启与多副本都失效';
COMMENT ON COLUMN knowledge_base.task_execution_registry.released_at IS
    'NULL = 当前被持有；非空 = 已释放、可以被重新领取。已释放的可再占是重发安全性的前提';
COMMENT ON COLUMN knowledge_base.task_execution_registry.claim_count IS
    '领过几次（含重派重新领的）。只做观测，不参与任何判定';

