-- ============================================================
-- 044_task_center_jonex_wiki.sql
-- Jonex Wiki 库的文件处理编排（存量库）
--
-- 对应全量脚本: deploy/postgres/migrations/008_task_center.sql
--   （同名内容也在 deploy/postgres/update/036_task_center.sql —— 那份是 008 的整本重放）
--   新库由它直接播种；本脚本只服务「当初应用的那一版里还没有这几行」的存量库。
--
-- 为什么存量库要单独补
--   Jonex Wiki（kb_type=jonex_wiki）本轮接进任务中心，上传 / 重新解析 / 重新编译
--   改由任务中心派活驱动。运行方提交时按定义 id 建任务
--   （`definition-file-processing-jonex-wiki`，见
--   capabilities/knowledge_base/services/task_runner_service.py 的
--   `definition_for_kb_type`）—— **这一行不存在，上传就报「找不到编排定义」**。
--
--   这几行数据其实早就写在 008 的预置段里，但存量库到底是在哪一次应用的、
--   `public.schema_migrations` 版本表又长期不可信（见 deploy/README.md），
--   所以这里幂等地再插一遍：**已经有的环境跑一遍就是空操作**。
--
-- 有意不做的事
--   不建表、不改列、不删任何东西。知识库侧那两张协调表
--   （knowledge_base.task_center_documents / task_execution_registry）早在
--   042_knowledge_task_center.sql 里，本脚本不碰。
--   也**不重写 008 / 036** —— 它们已经发布过，改动只从本文件走。
--
-- 幂等: 全部 ON CONFLICT DO NOTHING。重复执行是空操作。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 运行方（知识库）
--    步骤清单靠 runner_code 指回它；缺了这一步任务派不出去。
--    正常存量库里已有，这里只为「预置段整段没跑过」的极端情况兜底。
-- ------------------------------------------------------------
INSERT INTO task_center.runner_definitions (id, code, name, capability_id) VALUES
  ('runner-knowledge-base', 'knowledge_base', '知识库', 'business.knowledge_base.v1')
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 2. 内置任务清单 + 步骤清单
--    两步：parse（文件解析）→ compile（知识编译），runner_code = knowledge_base。
--    parse 的 output_schema 声明 document_id，供下游 `@parse_1.outputs.document_id` 取参。
-- ------------------------------------------------------------
INSERT INTO task_center.task_catalog (id, code, name, description) VALUES
  ('catalog-file-processing-jonex-wiki', 'file_processing_jonex_wiki', '文件处理（Jonex Wiki）', '上传文件 → 文件解析 → 知识编译')
ON CONFLICT DO NOTHING;

INSERT INTO task_center.task_catalog_steps
  (id, task_catalog_code, step_code, name, runner_code, max_concurrency, timeout_seconds, input_schema, output_schema) VALUES
  ('step-file-processing-jonex-wiki-parse', 'file_processing_jonex_wiki', 'parse', '文件解析', 'knowledge_base', 10, 25200,
   '{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}}',
   '{"document_id":{"type":"string","required":true}}'),
  ('step-file-processing-jonex-wiki-compile', 'file_processing_jonex_wiki', 'compile', '知识编译', 'knowledge_base', 10, 25200,
   '{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}}',
   '{}')
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 3. 平台级任务定义（scope=system ⇒ tenant_id / space_id 都为 NULL，由 CHECK 强制）
--    与另三条 wiki / ontology 链的区别只在**名字**（定义名就是任务中心页面上的
--    「这条任务属于哪类库」）与第一步的产物形状 —— 编排本身逐字同形。
-- ------------------------------------------------------------
INSERT INTO task_center.task_definitions
  (id, scope, tenant_id, space_id, task_catalog_code, name, description, current_version_no) VALUES
  ('definition-file-processing-jonex-wiki', 'system', NULL, NULL, 'file_processing_jonex_wiki', '文件处理（Jonex Wiki）', '上传文件后自动解析并编译 Jonex Wiki', 1)
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 4. 定义版本（published，两步串行 parse_1 → compile_1）
--    数据怎么传：下游的 document_id 取上一步产出（`@parse_1.outputs.document_id`），
--    knowledge_base_id 没有任何一步产出它、全程取任务入参。
--    ⚠️ 这一行与 008 里的**逐字一致**；改编排请新增版本号，别原地改已发布的 config。
-- ------------------------------------------------------------
INSERT INTO task_center.task_definition_versions
  (id, scope, tenant_id, definition_id, space_id, version_no, status, config, published_at) VALUES
  ('version-file-processing-jonex-wiki-v1', 'system', NULL, 'definition-file-processing-jonex-wiki', NULL, 1, 'published',
   '{"input_schema":{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}},"entry":"parse_1","steps":[{"node_id":"parse_1","step_code":"parse","depends_on":[],"next_steps":["compile_1"],"sort":1,"timeout_seconds":25200,"bind_inputs":{"document_id":"@task.inputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}},{"node_id":"compile_1","step_code":"compile","depends_on":["parse_1"],"next_steps":[],"sort":2,"timeout_seconds":25200,"bind_inputs":{"document_id":"@parse_1.outputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}}]}',
   CURRENT_TIMESTAMP)
ON CONFLICT DO NOTHING;

COMMIT;
