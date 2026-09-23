# RAG-Anything Jonex（jonex）改动点清单

> 本文件记录Jonex 平台对 vendored RAG-Anything（`Reference/Rag-anything/`）源码的改动点，
> 便于后续升级 RAG-Anything 时快速定位、重新 apply。**代码改动均以 `# [jonex]` 注释标记**，
> 可全局搜索 `[jonex]` 定位。约定与 `Reference/LightRAG/JONEX_CHANGES.md` 一致。

## 一、注释规范

- 单行改动：行尾加 `# [jonex]`。
- 代码块改动：块首 `# ── [jonex] <说明> ───`，块尾 `# ── [jonex] end ───`。
- 新增整类/整文件：类/文件头注释标明 `# [jonex] 新增`。

## 二、改动总览

| 分类 | 目的 |
|------|------|
| 解析器扩展 | 新增内网自建 MinerU（`mineru_selfhost`）解析器，去云化 + 去本地 GPU |
| 健壮性 | 多文件名/字段归一化去重与安全化 |
| 依赖分层 | 把 `mineru[core]` 从核心依赖降为 optional extra `local`，支撑 atomic-rag 镜像瘦身 |
| 查询契约补齐 | v1 退役：`handle_query` / `TaskManager.query` / `HttpLightRagClient.query` + `_query_headers` 透传 `only_need_context` / `user_id` / `trace_id`（补全 v1 六头计量维度） |
| 视频解析后端 | 新增 `mineru` video_analysis backend，视频整条委托 MinerU；修活 `video_analysis_binding` 死配置（设计见 `docs/superpowers/specs/2026-09-20-raganything-mineru-video-backend-design.md`） |

> 说明：仓库中 `mineru_online`（mineru.net 云 API 解析器）、`raganything/asr/*`、
> `raganything/video_analysis/*`（含腾讯 MPS 后端）、`resilience`/`callbacks` 等模块亦为Jonex相关定制/新增，
> 本清单自「MinerU 内网自建接入」起开始系统记录；对应设计见
> `docs/mineru-selfhost-parser-execution-plan.md`。

---

## 三、解析器扩展（feature）

### (A) 新增 `MineruSelfHostParser`（内网 mineru-api 解析器）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/parser.py` | `class MineruSelfHostParser(MineruParser)` | `# ── [jonex] 新增：内网自建 MinerU (mineru-api) 解析器 ───` |

要点：
- 对接内网自建的 **MinerU 官方 `mineru-api`（FastAPI）** 服务（默认 `http://127.0.0.1:8000`；实际内网地址在 gitignored 的 `deploy/.env` 配），
  区别于 `MineruOnlineParser`（mineru.net 云 v4 签名上传契约）。
- 契约：`POST /tasks`（multipart 上传）→ 轮询 `GET /tasks/{id}`（`pending/processing/completed/failed`）
  → `GET /tasks/{id}/result`（`results.<stem>.content_list` 为 **JSON 字符串**）。
- 仅用标准库（`urllib` / `http.client`），**不依赖本地 mineru 包、不引入第三方 HTTP 库**。
- 复用父类 `MineruParser._FIELD_ALIASES` 做字段归一化；`return_images` 控制是否落盘图片并重写路径。
- 关键方法：`_post_multipart` / `_submit_task` / `_poll_task` / `_fetch_result_item`
  / `_build_content_list` / `_ascii_safe_filename` / `_dump_images`。

环境变量（parser 侧直接读 `os.environ`）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `MINERU_SELFHOST_BASE_URL` | `http://127.0.0.1:8000` | 内网 mineru-api 地址（实际值在 `deploy/.env`） |
| `MINERU_SELFHOST_BACKEND` | `pipeline` | `pipeline` / `hybrid-engine` 等 |
| `MINERU_SELFHOST_LANG` | `ch` | pipeline/hybrid 后端 OCR 语言 |
| `MINERU_SELFHOST_POLL_INTERVAL` | `5` | 轮询间隔（秒） |
| `MINERU_SELFHOST_POLL_TIMEOUT` | `1800` | 轮询总超时（秒） |
| `MINERU_SELFHOST_RETURN_IMAGES` | `false` | 是否拉取解析图片并落盘 |

### (B) 解析器注册（`mineru_selfhost` 接入内置解析器）

| 文件 | 位置（均带 `# [jonex]`） | 说明 |
|------|------|------|
| `raganything/parser.py` | `_BUILTIN_NAMES` | 加入 `mineru_selfhost`（禁止被 `register_parser` 覆盖） |
| `raganything/parser.py` | `SUPPORTED_PARSERS` | 加入 `mineru_selfhost` |
| `raganything/parser.py` | `list_parsers()` 映射 dict | 加入 `"mineru_selfhost": "MineruSelfHostParser"` |
| `raganything/parser.py` | `get_parser()` 分支 | `if parser_name == "mineru_selfhost": return MineruSelfHostParser()` |
| `raganything/parser.py` | `get_parser` docstring / `main()` argparse help / 顶部 description | 文案补 `mineru_selfhost` |

### (C) `_FIELD_ALIASES` 提取为类常量（去重，健壮性）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/parser.py` | `MineruParser._FIELD_ALIASES`（类常量，`# [jonex]`） | 原为 `_read_output_files` / `_read_any_output_files` 内 2 处重复局部 dict |
| `raganything/parser.py` | `_read_output_files` / `_read_any_output_files` | 改为引用 `cls._FIELD_ALIASES` |

### (D) 中文/非 ASCII 文件名安全化（健壮性）

- `MineruSelfHostParser._ascii_safe_filename()`：上传 multipart 时把非 `[A-Za-z0-9._-]` 文件名字符
  替换为 `_`（保留扩展名，MinerU 只按扩展名判类型），避免中文文件名在 `filename="..."` 头里被服务端
  错误解码；单文件任务结果按 `results` 首条兜底匹配，不依赖文件名。

### (E) 单测

| 文件 | 说明 |
|------|------|
| `tests/test_custom_parser.py` | `list_parsers` 数量断言 4→5 / 5→6；`test_register_rejects_builtin_name` 元组补 `mineru_selfhost`；新增 `TestMineruSelfHostParser`（注册 / 环境变量 / `_ascii_safe_filename` / `_build_content_list` / `_fetch_result_item`，全离线） |

---

## 四、依赖分层（build）：`mineru[core]` 降为 optional extra `local`

目的：让 atomic-rag 镜像在 online/selfhost 模式下可去掉 mineru 及其重传递依赖。**三处元数据一致修改**，
保证无论 pip 采用 pyproject 还是 setup.py 都生效：

| 文件 | 改动 |
|------|------|
| `pyproject.toml` | `[project].dependencies` 移除 `mineru[core]`；`[project.optional-dependencies]` 新增 `local = ["mineru[core]"]`（`# [jonex]`） |
| `setup.py` | `extras_require` 新增 `"local": ["mineru[core]"]`（`# [jonex]`） |
| `requirements.txt` | 移除 `mineru[core]` 行，加 `NOTE(jonex)` 说明其移入 `local` extra |

安装约定（由 `deploy/docker/atomic-rag.Dockerfile` 的 `RAG_PROFILE` 控制，见下）：
- `full`：`pip install -e "/opt/raganything[all,local]"`（含本地 mineru CLI）。
- `slim`：`pip install -e "/opt/raganything[image,text]"`（online/selfhost，无 mineru）。

> 安全依据：全仓无任何 `import mineru`，本地解析走 `mineru` CLI 子进程；online/selfhost 从不调用本地 mineru。

---

## 五、平台侧配套改动（不在本目录，登记以便追溯）

| 文件 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/lightrag_adapter.py` | 支持 `RAG_PARSER=mineru_selfhost`；记录 `self._parser_name`；CLI 专用 kwargs（backend/source）仅对 `RAG_PARSER=mineru` 注入；错误文案补三种模式 |
| `deploy/docker/atomic-rag.Dockerfile` | 新增 `ARG RAG_PROFILE=full`，第 2 层按 profile 条件安装 raganything（full=`[all,local]` / slim=`[image,text]`） |
| `deploy/docker/atomic-rag-requirements.txt` | 移除 `sentence-transformers`（全仓零引用；它与 whisper 是 torch 的唯二来源） |
| `deploy/docker-compose.yml` | `atomic-rag.build.args` 新增 `RAG_PROFILE: ${RAG_PROFILE:-full}` |
| `deploy/.env` / `deploy/.env.example` / `.env.local.example` | MinerU 段重构为三选一 + `MINERU_SELFHOST_*` + `RAG_PROFILE` 说明 |

详见 `docs/mineru-selfhost-parser-execution-plan.md`。

---

## 六、升级 RAG-Anything 时的重放清单

1. 全局搜索 `[jonex]` 定位 `raganything/parser.py` 内的：`MineruSelfHostParser` 整类、`_FIELD_ALIASES` 类常量、
   4 处注册点、docstring/CLI 文案。
2. 重放依赖分层：确认新版 `pyproject.toml` / `setup.py` / `requirements.txt` 是否仍把 `mineru[core]` 列为核心依赖；
   若是，重新降级到 `local` extra。
3. 跑 `tests/test_custom_parser.py` 校验解析器注册契约。
4. atomic-rag 侧按 `RAG_PROFILE=slim/full` 重建镜像并做 selfhost 端到端解析验证（见执行计划 §11）。

---

## 七、P1-4 结构感知切分（2026-08-05）

### 目的

对文档解析后的 `content_list` 做结构感知预分段：
- 版本清单/变更日志条目（`New in … X.Y.Z`）强制同 chunk
- 表格行回填文档/表标题上下文前缀，提升短行嵌入判别力
- 标签 `block_type`（list_entry/table_row/heading/text）供检索期过滤

### 改动点

| 文件 | 行 | 改动 |
|------|-----|------|
| `raganything/utils.py` | 导入 | 新增 `import os, re` |
| `raganything/utils.py` | `_classify_block_type()` | 新增函数，检测 block 结构类型 |
| `raganything/utils.py` | `_enrich_table_row_text()` | 新增函数，为表行回填标题上下文 |
| `raganything/utils.py` | `structure_aware_chunk()` | 新增函数，结构感知增强入口 |
| `raganything/utils.py` | `separate_content()` | 调用 `structure_aware_chunk()` 标注 `block_type` |

### 配套

| 文件 | 改动 |
|------|------|
| `Reference/LightRAG/lightrag/lightrag.py` | `ainsert_custom_chunks` 元数据合并新增 `block_type`/`entity_hint`/`text_idx`/`char_start`/`char_end` |
| `deploy/.env.rag` | 新增 `RAG_STRUCTURE_AWARE_CHUNK=false` |
| `docs/technology-kb-retrieval-quality-rootcause-and-optimization-plan.md` | 方案 §8 P1-4 |

### 升级重放

1. 全局搜索 `P1-4` 定位新增代码
2. 确认 `RAG_STRUCTURE_AWARE_CHUNK` 开关行为
3. 重建 `atomic-rag` 镜像验证


## 七、v2 LightRAG 关系响应契约归一化（fix）

| 文件 | 改动 |
|------|------|
| `raganything/service/http_lightrag_client.py` | `get_relationships()` 在 HTTP 客户端边界把 LightRAG 的 `src_id` / `tgt_id` 补齐为平台稳定契约 `source_entity` / `target_entity`，同时保留原字段，修复 v2 本体关系定型和 untyped fallback 均因端点字段缺失而得到 0 条关系的问题。 |
| `tests/test_service/test_http_lightrag_client.py` | 新增关系响应归一化回归测试，覆盖原始字段、已有规范字段及不修改上游响应对象。 |

升级重放：若新版 LightRAG 关系端点仍返回 `src_id` / `tgt_id`，保留该客户端边界映射；若上游已返回 `source_entity` / `target_entity`，当前兼容逻辑会优先使用规范字段。


## 八、v2 chunk 命名空间 token 注入回归修复（fix）

**现象**：v2 入库某文档后 LightRAG 图中该文档实体数为 0（`Task ... completed entities=0`），
本体阶段读不到候选实体 → `无候选实体` → 对账重试 3 次耗尽 → `ontology_status=FAILED`
（`reconciliation_service.py:481 Ontology retry limit reached (3/3)`）。

**根因**：v1 `LightRAGAdapter` 在 `upload_text` 前给每个 chunk 文本末尾注入
`<!--yx:{md5(tenant|kb|doc)[:8]}-->` 命名空间 token，使内容 hash 带上 (tenant, kb, doc)
隔离维度，避免 LightRAG 按 chunk 内容全局去重把跨文档/跨 KB 的相同文本合并（合并后只有首篇
文档抽到实体、其余文档图为空）。v2 迁移到 `PushChunksStage` 后**遗漏该 token**，
`_collect_text_chunks` / `_collect_multimodal_chunks` 上传的是原始文本；而消费侧
`task_manager.py` 本体抽取仍按 `<!--yx:[a-f0-9]{8}-->` 过滤该 token，证明契约期待其存在。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | 新增 `_inject_ns_token(text, tenant_id, kb_id, document_id)`；`_collect_text_chunks` 文本/表格 chunk 与 `_collect_multimodal_chunks` 的 summary/video_frame/audio_segment chunk 追加该 token 后再入队上传（对齐 v1）。新增 `import hashlib`。 |

**验证**：`py_compile stages.py` 通过；需重建 atomic-rag 镜像生效。存量已 `entities=0` 的文档
需删除后重新入库（或清 LightRAG 该文档 chunk + 抽取缓存）才能重跑抽取，仅重置 `ontology_status`
无效（图仍为空）。

升级重放：若新版仍走 `PushChunksStage`，保留 `_inject_ns_token` 注入；消费侧过滤正则与
`hexdigest()[:8]` 长度需保持一致。


## 九、v2 ⇄ v1 行为补齐 #4/#5/#6 (2026-07-16)

补齐 v2 HTTP 模式相对 v1 `lightrag_adapter.py` 缺失的三项健壮性/成本/检索正确性行为。
详见 `docs/atomic-rag-v2-v1-parity-fixes-execution-plan.md`。

### (A) #6 逐 chunk 严格确认 doc_id/track_id

区分 TIMEOUT 与硬失败，默认 `RAG_REQUIRE_DOC_IDS=true`（与 v1 对齐）。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.__init__` 读取 `RAG_REQUIRE_DOC_IDS`（默认 true）；§4 track_status 轮询分类 terminal failed 为 `terminal_hard_failed`；§5 严格模式下硬失败/超时分别返回错误（`LightRAG 入库部分失败：X/Y` / `RAG_PUSH_TIMEOUT`）；ctx 写入 `total_chunk_count`/`failed_chunk_count`/`timeout_chunk_count` |
| `raganything/pipeline/base.py` | `PipelineContext` 新增 `total_chunk_count`/`failed_chunk_count`/`timeout_chunk_count`/`duplicated_chunk_count`/`total_pushed_count` |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 失败分支持久化 `ctx.collected_doc_ids`→`task.lightrag_doc_ids`；透传 `timeout_chunk_count`；`_resume_track_polling` 区分 terminal failed vs still_pending → `timeout_chunk_count` |
| `raganything/service/models.py` | `TaskInfo` 新增 `timeout_chunk_count`/`duplicated_chunk_count`/`total_pushed_count` |

### (B) #5 全 duplicated 幂等守卫（跳过本体抽取）

所有 chunk 内容重复 → 跳过本体抽取（省 LLM 成本），设 `ontology_status="completed"`。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage._push_one` 收集 `result.status == "duplicated"` 到 `duplicated_indices`；execute §5 计算 `duplicated_chunk_count`/`total_pushed_count` 写入 ctx |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 本体触发前检查 `all_duplicated`（`total_pushed>0 and duplicated==total_pushed`）→ 跳过 `_run_ontology_extraction`、置 `ontology_status="completed"` |

### (C) #4 parse 解析阶段瞬时错误自动重试

仅对瞬时网络/SSL 错误指数退避重试，硬失败快速返回。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | 新增 `_TRANSIENT_PARSE_ERROR_MARKERS` + `_is_transient_parse_error()` 辅助函数（对齐 v1）；`ParseStage.__init__` 读取 `RAG_PARSE_RETRY_MAX`（默认 3）/`RAG_PARSE_RETRY_BASE_SEC`（默认 2.0）；原有 try/except NotImplementedError 替换为 `_parse_with_retry()` 方法，primary/fallback 均套重试循环，每次重试前检查 `ctx.cancel_event` |

### 新增环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `RAG_REQUIRE_DOC_IDS` | `true` | 严格要求逐 chunk 确认 doc_id |
| `RAG_PARSE_RETRY_MAX` | `3` | 解析瞬时错误最大重试次数（含首次） |
| `RAG_PARSE_RETRY_BASE_SEC` | `2.0` | 解析重试指数退避基数 |

升级重放：三处改动均以 `# [jonex]` 标记，集中在 `stages.py`/`base.py`/`task_manager.py`/`models.py`。


## 十、v2 batch_track_status pending 脏残留误判超时修复（2026-07-16）

**现象**：文档入库成功（collected_doc_ids 有值），但 PushChunksStage 报 `RAG_PUSH_TIMEOUT: 600.0s`，
而实际轮询几秒就退出了，并未等待 10 分钟。

**根因**：`batch_track_status` 的 `pending` dict 在循环外声明、循环内不清理。一个 chunk 第 1 轮
还在 `processing`（写进 `pending`），第 2 轮才 `completed`（写进 `terminal`）——但没有任何地方把它从
`pending` 里删掉。结果它同时留在 `terminal` 和 `pending`。`stages.py` §5 按 `pending_track_ids`
判超时，把已完成的 chunk 误判为 timed_out → 严格模式直接报 `RAG_PUSH_TIMEOUT`。

**修复**：终态 track 进 `terminal` 时同步从 `pending` 剔除（`pending.pop(tid, None)`），一行核心改动。

| 文件 | 改动 |
|------|------|
| `raganything/service/http_lightrag_client.py` | `batch_track_status` 轮询循环内 `terminal[tid] = result` 后追加 `pending.pop(tid, None)`（`# [jonex]`） |
| `tests/test_service/test_http_lightrag_client.py` | 新增 `TestBatchTrackStatusNoDirtyPending`：跑真实 `batch_track_status` 实现（mock `_poll_one_track`），验证 processing→completed 转换后 pending 不含已完成 track |

升级重放：若新版 `batch_track_status` 重构轮询逻辑，需确保终态 track 不会残留在 pending/未完成集合中。


## 十一、v2 batch_track_status 并发控制（2026-07-16）

**现象**：V2 `batch_track_status` 用 `asyncio.gather` 一次性并发所有 remaining track_ids，
大文档数百 chunk 时同时建立数百 HTTP 连接，无任何限流机制。V1 有 `RAG_TRACK_POLL_CONCURRENCY=8` 来控制。

**修复**：`HttpLightRagClient.__init__` 读取 `RAG_TRACK_POLL_CONCURRENCY`（默认 8），
`batch_track_status` 轮询循环内用 `asyncio.Semaphore` 包装 `_poll_one_track`，限制同时进行的
track_status 查询数。

| 文件 | 改动 |
|------|------|
| `raganything/service/http_lightrag_client.py` | `__init__` 新增 `self._track_poll_concurrency`；`batch_track_status` 用 semaphore 限流（`# [jonex]`） |

### 新增环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `RAG_TRACK_POLL_CONCURRENCY` | `8` | track_status 轮询并发上限 |

升级重放：若新版 `batch_track_status` 重构轮询逻辑，保留 semaphore 限流避免连接风暴。


## 十二、parser.py 拆包后 CLI 顶部 description 文案补回（review, 2026-07-17）

**背景**：`MineruSelfHostParser` 原在单体 `raganything/parser.py`（commit `243dd4c`）实现，
后 `parser.py` 重构为 `raganything/parsers/` 包（`mineru.py` / `registry.py` / `__init__.py`）。
本次 review 逐方法比对拆包后实现与 `243dd4c` 原始实现，核心类、`_FIELD_ALIASES`、4 处注册点、
`__init__` 导出、`--parser` argparse help 均已完整保留，**仅顶部 `ArgumentParser(description=...)` 漏补**
（见上文 §三(B) 记录的「顶部 description」项，拆包时只补了 `--parser` help，遗漏了顶部 description）。

| 文件 | 位置 | 改动 |
|------|------|------|
| `raganything/parser.py` | `main()` 内 `argparse.ArgumentParser(description=...)` | description 由 `"...MinerU online, Docling, or PaddleOCR"` 补为 `"...MinerU online, MinerU self-host, Docling, or PaddleOCR"` |

纯 CLI 帮助文案，不影响运行时解析行为。`py_compile` `parser.py` / `parsers/{mineru,registry,__init__}.py` 全部通过。

升级重放：全局搜索 `[jonex]` 及本节，确认 `parser.py` 顶部 description 与 `--parser` help 两处文案都含 `mineru_selfhost` / `MinerU self-host`。


## 十三、P3 push_chunks 阶段信号 + VLM base64 caption 适配器（2026-07-23）

批次 2-A + 2-C：透出 P3 推送入图阶段信号供 kb-service 对账置 INGESTING；
修复图片 VLM 描述调用约定不匹配（base64 vs file://）。

详见 `docs/kb-doc-status-ingesting-and-patrol-plan.md` §4、§6、§12。

### (A) PushChunksStage 发 on_push_chunks_start → current_step="push_chunks"

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.execute` 起始 dispatch `on_push_chunks_start`（`# [jonex] Callback: push_chunks start`） |
| `raganything/callbacks.py` | `ProcessingCallback` 新增 `on_push_chunks_start` 空实现（`# [jonex] 批次 2-A`） |
| `raganything/service/task_manager.py` | `ProgressTrackingCallback` 新增 `on_push_chunks_start` 处理器：`current_step="push_chunks"`、`progress=0.92`（`# [jonex] 批次 2-A`） |

信号值定稿为 `"push_chunks"`，经 `get_task_status` 内存实时透出，kb-service 对账据此置 `DocStatus.INGESTING`。

### (B) VLM base64 caption 适配器

| 文件 | 改动 |
|------|------|
| `raganything/models/adapters.py` | 新增 `base64_caption_adapter(bound)`：签名 `(prompt, image_data=base64, system_prompt=...)` → `data:` image_url（`# [jonex] 批次 2-C`） |
| `raganything/models/__init__.py` | 导出 `base64_caption_adapter` |
| `raganything/service/model_factory.py` | `build_vlm()` 返回值从 `Callable` 改为 `dict {"func": ..., "bound": ...}` |
| `raganything/processor_builder.py` | `_create_image` 优先用 `_vlm_bound` + `base64_caption_adapter`，fallback 到原有 `_vlm_func` |
| `raganything/raganything.py` | 新增 `vlm_bound` 字段；`_init_processors_via_builder` 传递 `vlm_bound` 到 builder |
| `atomic-rag-server-v2.py` | 解构 `build_vlm()` 返回 dict，传 `vlm_bound` 到 `RAGAnything` |

升级重放：若新版 `PushChunksStage.execute` 重构，保留起始 dispatch `on_push_chunks_start`；
若新版 `build_vlm()` 签名变化，确认 `vlm_bound` 仍能从 `model_factory` 传递到 image processor factory。

### (C) 平台侧配套改动

| 文件 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/lightrag_adapter_v2.py` | `_assemble_stack` 解构 `build_vlm()` 返回 dict，传 `vlm_bound` 到 `RAGAnything` |
| `atomic-rag-server-v2.py` | 同上，解构 `build_vlm()` 返回 dict，传 `vlm_bound` 到 `RAGAnything` |

## 十四、chunk_id 透传 + 单片直查（RAG fallback 召回明细 / 查看单个 Chunk）（2026-07-24）

> 关联设计：`docs/rag-fallback-recall-detail-and-chunk-lookup-plan.md`（§3.2、§9 P9~P11）。
> 关联 LightRAG 侧改动：`Reference/LightRAG/JONEX_CHANGES.md` §十。
> 生效前提：vendored 改动，须**重建 atomic-rag 镜像**。所有改动带 `# [jonex]` 标记。

### (A) chunk_id 透传（v2 生产检索路径）

**背景**：LightRAG references 透出 `chunk_ids` 后，recalls 的 `chunk_id` 仍恒 null。根因是 v2 生产路径
（REMOTE：kb-service → atomic-rag → raganything）组装 references 时丢弃了 `chunk_ids`（此前只改了
v1 `jonex_core/.../lightrag_adapter.py::LightRAGServerClient.query()`，生产不走）。

| 文件 | 位置 | 说明 |
|---|---|---|
| `raganything/service/task_manager.py` | `_query_via_http`（约 L692-716） | `parse_file_source` 组装 `refs` 的循环中透传 `chunk_ids`：`parsed["chunk_ids"]=chunk_ids; parsed["chunk_id"]=chunk_ids[0]`（本平台 file_source 按 chunk 唯一，取首个） |

### (B) 单片直查 `get_chunk`（按 chunk_id 直连 LightRAG text_chunks）

| 文件 | 位置 | 说明 |
|---|---|---|
| `raganything/service/http_lightrag_client.py` | `get_chunk_by_id` | 新增：`GET /documents/chunks/{chunk_id}`（带 workspace header），返回单片 chunk |
| `raganything/service/task_manager.py` | `get_chunk_by_id` | 新增：调 http_client；`httpx.HTTPStatusError` 404 → None |
| `atomic-rag-server-v2.py` | `@ActionRegistry.register("get_chunk")` | 新增 action handler `handle_get_chunk`；None → 40405 |

### (C) 既有缺陷 TODO：`get_document_chunks`（"查看文档 Chunk 列表"接口）

| 文件 | 位置 | 说明 |
|---|---|---|
| `raganything/service/task_manager.py` | `get_document_chunks` docstring | 打 `# [jonex][TODO]`：该实现对已入库文档恒返回空/结构不符（查 `/documents/paginated` 返回文档级 `{documents}` 而非 text_chunks，键名/层级不符 `{doc_id,total,chunks}` 契约，且 doc_id 语义不匹配——LightRAG 用 `doc-<md5>`、KB id 仅在 file_path 的 `doc=` 锚点）。修复方案见该 TODO 与计划 §9(P11)。单片直查已由 (B) 解决、不受影响 |

**平台侧配套**（不在本目录，登记以便追溯）：

| 文件 | 改动 |
|---|---|
| `jonex_core/capability/atomic/rag/client.py` | `RemoteRAGClient.get_chunk_by_id` 发 action `get_chunk`，40405→None |
| `jonex_core/capability/atomic/rag/lightrag_adapter.py` | v1 兼容路径 `LightRAGServerClient.query()` 解析 `chunk_ids`（生产不走） |
| `capabilities/knowledge_base/services/document_service.py` | `get_chunk` 改直查 `get_chunk_by_id`（不经 get_doc_chunks）+ `doc=` 锚点归属校验 + 清理 `<!--yx:...-->` 标记；`get_document_chunks` 补 TODO |
| `capabilities/knowledge_base/services/search_service.py` | `_rag_fallback_multi` recalls 明细带 `chunk_id`（来自透传） |

升级重放：(A) 保留 `_query_via_http` 的 chunk_ids 透传；(B) 保留 `get_chunk_by_id` 全链路 + action 注册；
(C) get_document_chunks 若重构，按 TODO 用 `doc=` 锚点枚举 text_chunks 返回契约结构。


---

## 二十一、P0 `_converge_delete` 收敛循环（2026-08-06）

> **取代本文件"四、(2) 2-A"与"(4) review-fix"。**
> 线上事故根因：`_run_cleanup` 删一次撞 busy（31s 5 次重试耗光）→ `_poll_old_ids_gone`
> 只 poll 不重删（空等一件从未启动的删除）→ 必然超时 → 文档卡死 60 分钟判 failed。
> 详见 `docs/rag-reparse-strict-cleanup-busy-plan.md`。

### 修复思路

旧 "删一次 → 只 poll" 改为 **"每轮重新发起 delete + 读一致性确认"**：
只要 pipeline 一空闲，下一轮 delete 立刻被受理，不再被 31s 的 busy 重试窗口卡死。

```python
async def _converge_delete(self, task, old_ids):
    deadline = time.monotonic() + RAG_CLEANUP_MAX_ELAPSED  # 默认 1800s
    while time.monotonic() < deadline:
        remaining = old_ids & set(await self._list_doc_ids_by_document(task))
        if not remaining:
            return set()               # 收敛成功
        try:
            await self._http_client.delete_docs(list(remaining), ...)
        except Exception:
            pass                        # busy → 等下一轮重发
        await asyncio.sleep(RAG_CLEANUP_POLL_DELAY)  # 默认 5s
    return remaining                    # 超预算 → 交 KB 3-A 兜底
```

### 改动清单

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | 新增 `_converge_delete(task, old_ids)` | 收敛循环：每轮 `delete_docs` + 查询确认；deadline 按 `per_doc_sec * n` 缩放（`min(1800, max(300, 300 + 2*n))`）；`delete_retry_interval` 控制重发频率避免对 LightRAG 持续轰炸 |
| 同上 | `_reparse_delete_all_old` | 用 `_converge_delete` 替换旧的 `_run_cleanup` + `_poll_old_ids_gone` |
| 同上 | `_resume_cleanup` | 同上（容器重启续删路径） |
| 同上 | ~~`_run_cleanup` / `_poll_old_ids_gone`~~ | **已删除**（共 ~114 行死代码） |
| `raganything/service/http_lightrag_client.py` | `delete_docs` docstring | 更新注释：`_poll_old_ids_gone` → `_converge_delete` |
| `capabilities/knowledge_base/services/document_service.py` | `reparse_document` | **P1 幂等**：同一 doc 已有 in-flight RAG 任务时拒绝重复提交，从源头消除 cleanup ↔ index 竞争 |

### 新增环境变量

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `RAG_CLEANUP_MAX_ELAPSED` | 1800 | 收敛循环最长运行时间（秒） |
| `RAG_CLEANUP_POLL_DELAY` | 5 | 每轮 poll 间隔（秒），默认从旧值 2 提升到 5 |

### 设计要点

- **首次立即发起 delete**（不是等第一轮 poll），`last_delete_attempt = -delete_retry_interval`
- **间隔控制**：`delete_retry_interval = max(poll_delay * 3, 10.0)`，不对同一批持续轰炸
- **进度更新**：每轮将 `remaining` 写入 `task.delete_pending_ids` 供 KB 对账观察
- **fail-open**：delete 失败不抛异常，下一轮重试；查询失败 `continue` 等下一轮
- **超预算**：返回残留集合 → 调用方 `_fail_task` + KB 3-A 动态超时兜底

### KB 侧 P1 幂等（同批次）

`document_service.py` 的 `reparse_document` 入口：若 `doc.rag_task_id` 对应的 RAG 任务
处于 `created/queued/processing`，直接 `ResourceConflictError` 拒绝，不创建第二个任务。
查询失败 fail-open（不阻塞正常 reparse）。

---

## 四、reparse_strict 补偿 cleanup 卡死修复（bugfix，TODO：rag-reparse-strict-cleanup-busy-plan）

> 背景与根因见 `docs/rag-reparse-strict-cleanup-busy-plan.md`。现象：reparse_strict 推送时几个
> chunk 失败 → 严格模式整体失败 → 补偿删除本次全部新 doc（如 258 个）；逐个删撞 LightRAG
> 单 workspace busy 锁 + 每删触发 O(N) KG rebuild，删不完 → 任务卡 `current_step=cleanup`
> → KB 对账 P0-J 守卫无限期 keep ingesting → 文档永久卡死。本次做 1-A/2-A/3-A 三项。

### (1) 1-A：push 阶段终态 failed chunk 纳入有界重推

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.__init__` | 新增 `self._retry_terminal_failed`（env `RAG_PUSH_RETRY_TERMINAL_FAILED`，默认 true） |
| `raganything/pipeline/stages.py` | `PushChunksStage.execute` per-chunk 重试循环 | track 终态 `failed` 的 chunk 不再立即判永久硬失败，复用 per-chunk 重试预算（`RAG_TRACK_PER_CHUNK_MAX_RETRIES`）与超时 chunk 一起重推，预算耗尽后才落 `terminal_hard_failed` |

要点：降低「几个 chunk 抖动即整体失败 → 全量回滚」的放大；不重复累加计数、尊重 cancel_event、
关闭开关即回退旧行为。

### (2) 2-A：cleanup 整批删除 + 轮询窗口随量缩放 ⚠️ 已被 P0 取代

> **该条目已被 P0 `_converge_delete` 收敛循环取代（2026-08-06）。**
> `_run_cleanup` 和 `_poll_old_ids_gone` 已从 `task_manager.py` 中删除，替换为
> `_converge_delete`：每轮重新发起 `delete_docs` + 读一致性确认，而非"删一次→只 poll"。
> 详见下方 [二十一、P0](#二十一p0-converge_delete-收敛循环2026-08-06)。

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/http_lightrag_client.py` | 新增 `delete_docs(doc_ids: list)` | 一次 DELETE 传全部 doc_ids，LightRAG 单 busy 会话内连删；含 busy 退避重试（`RAG_DELETE_BUSY_RETRIES`/`RAG_DELETE_BUSY_DELAY`） |
| ~~`raganything/service/task_manager.py`~~ | ~~`_run_cleanup`~~ | ~~改为对每个 pending field 调一次 `delete_docs` 整批删除~~ → 已删除，由 `_converge_delete` 取代 |
| ~~`raganything/service/task_manager.py`~~ | ~~`_poll_old_ids_gone`~~ | ~~轮询窗口随 len(old_ids) 缩放~~ → 已删除，由 `_converge_delete` 取代 |

### (3) 3-A 前置：暴露 cleanup 进度

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/models.py` | `TaskInfo.cleanup_total` | 新增字段：进入 cleanup 时的初始待删总量 |
| `raganything/service/task_manager.py` | `_reparse_converge_old` / `_reparse_compensate` | 进入 cleanup 时设置 `task.cleanup_total` |
| `atomic-rag-server-v2.py` | `handle_get_task_status` 返回体 | 新增 `cleanup_total` / `cleanup_pending_count`，供 KB 对账按删除量算动态超时 |

> KB 侧配套改动（不在本文件）：`capabilities/knowledge_base/services/reconciliation_service.py`
> `_handle_failed` 的 cleanup 守卫改为按量动态超时判死
> （`RECONCILE_CLEANUP_BASE_SEC`/`RECONCILE_CLEANUP_PER_DOC_SEC`/`RECONCILE_CLEANUP_CEIL_SEC`）。

### 新增环境变量（保守默认，行为向后兼容）

| 变量 | 默认 | 作用 |
|------|------|------|
| `RAG_PUSH_RETRY_TERMINAL_FAILED` | true | 1-A：终态 failed chunk 是否重推 |
| `RAG_CLEANUP_POLL_PER_DOC_SEC` | 2 | 2-A：cleanup 轮询窗口随删除量缩放系数 |

### (4) review-fix：cleanup poll 超时残留不再乐观置 done/COMPLETED ⚠️ 已被 P0 取代

> **该条目已被 P0 `_converge_delete` 收敛循环取代（2026-08-06）。**
> 旧的 `_run_cleanup` + `_poll_old_ids_gone` 删一次→只 poll 的模式已被彻底移除，
> `_reparse_delete_all_old` 和 `_resume_cleanup` 均改为调用 `_converge_delete`。
> 详见下方 [二十一、P0](#二十一p0-converge_delete-收敛循环2026-08-06)。

<details>
<summary>旧描述（仅供参考）</summary>

> Review 指出：`_run_cleanup` 整批受理即乐观清空 pending，若随后 `_poll_old_ids_gone` 超时仍有
> 旧 doc 残留，原逻辑仍会把 `current_step` 切 `done` 并（调用方）置 COMPLETED → 旧数据残留却暴露
> READY（违反 P0-3 读一致性）。

| 文件 | 位置 | 说明 |
|------|------|------|
| ~~`raganything/service/task_manager.py`~~ | ~~`_poll_old_ids_gone`~~ | 已删除 |
| ~~`raganything/service/task_manager.py`~~ | ~~`_reparse_converge_old`~~ | 已删除 |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` reparse_strict 分支 | P0 后保持：converge 未收敛 → `_fail_task`（保持 cleanup）→ 交 KB 3-A 兜底 |
| `raganything/service/task_manager.py` | `_resume_cleanup` | P0 后保持：续删残留 → `_fail_task`（保持 cleanup），由 `_converge_delete` 引擎驱动 |

效果：旧 doc 未确认删净时任务落 FAILED 且 `current_step=cleanup` 保留 → KB 对账 3-A 动态超时兜底
（据 `cleanup_total` 计时）→ 文档最终 FAILED、前端可见可重传；残留 orphan 交离线/清库处理。

</details>

---

## 十五、P0-1 dup-failed 三态判定（2026-07-29）

> 根因：LightRAG 内容去重产生的 `dup-*` 文档被 track_status 标为 `failed`，
> 但 atomic-rag stages 不区分「真失败」与「dup-failed」，全部计入 hard_failed →
> strict_push 整体失败 → reparse 补偿删全量（如 2/1878 误判触发回滚 809）。
> 修法三件套：1) client 透出 metadata + 新增 doc status 查询能力；
> 2) stages 轮询层识别 dup 并按原件状态三态判定；
> 3) dup_wait 保留轮询而非立即放过。

### 15.1 TrackStatus 透出 metadata + 新增 get_document_status

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/http_lightrag_client.py` | `TrackStatus` dataclass | **新增** `doc_metadata: dict \| None = None` 字段，承载 LightRAG track_status 响应中文档的 `metadata`（含 `is_duplicate`、`original_doc_id`） |
| 同上 | `_parse_track_status()` | 提取 `documents[].metadata`；failed 时填入第一个 failed doc 的 metadata |
| 同上 | 新增 `get_document_status(doc_id, *, tenant_id, kb_id)` | GET `/documents/{doc_id}` → doc info dict（含 `status`）或 None；**依赖 LightRAG vendored 同路径端点**（`Reference/LightRAG/JONEX_CHANGES.md` §10.3）。None 返回附带 WARNING 日志防静默 no-op |
| 同上 | `doc_exists()` | 同样改用 GET `/documents/{doc_id}`（原 HEAD 同一不存在的路径） |

### 15.2 stages 轮询层 dup-failed 三态判定

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | 新增 `_extract_dup_original_id(status)` | 从 `TrackStatus.doc_metadata.is_duplicate`(优先) 或 `error` 文本匹配 "Content already exists" 提取 `original_doc_id` |
| 同上 | 新增 `_query_doc_status(http_client, doc_id, ...)` | 异步查 LightRAG 原件当前状态 → `"processed"` / `"pending"` / `"processing"` / `"failed"` / None |
| 同上 | `PushChunksStage.run()` terminal 收集处 | 三态判定：原件 processed → 良性成功（不计 hard_failed）；原件 pending/processing → **保留在 polling 集合继续轮询**（等待原件完成，靠 per_chunk_timeout 收口）；原件 failed → 落入 hard_failed；原件查不到（None）→ 保守落入 hard_failed 并打 WARNING |

### 15.3 依赖关系

依赖 LightRAG vendored `GET /documents/{doc_id}` 端点。未部署该端点时，`get_document_status` 恒返回 None，dup 三态判定静默退化为 hard_failed（旧行为）。日志中会有 WARNING 提示。


---

## 十六、② push 超时确认前复查真实 doc 状态（治本假 RAG_PUSH_TIMEOUT，2026-07-29）

> 现象（线上 doc 495a5358 f_040_3M_2018_10K.pdf，1878 chunk）：`lightrag_doc_status`
> **processed=2537 / failed=7 / pending=0**（内容其实全跑完），但 PushChunksStage 报
> `RAG_PUSH_TIMEOUT: 15/1878 chunks 轮询超时未确认（1800.0s）` → strict 回滚 530。
>
> 根因：超大文档长尾 chunk 抽取慢，被 `per_chunk_timeout`(900s) 误判超时 → 1-A 重推
> （round1 306 + round2 87）→ 重推内容重复 → 新 track_id 对应 dup，**永不返回 completed**
> → 到全局 `RAG_TRACK_TIMEOUT_SECONDS`(1800s) 仍有 15 个未确认 → strict 假失败回滚。
> **内容其实已 processed，只是 track 确认滞后/重推变 dup。**
>
> 修法：strict 判定 `RAG_PUSH_TIMEOUT` 前，对每个 timed_out chunk **按内容复算 doc_id 直接查
> LightRAG 真实状态**，已 `processed` 则判为已确认（收集 doc_id、移出 timed_out），消除假失败。

### 16.1 改动

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | import | `from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding`（新增后者） |
| 同上 | 新增 `_expected_doc_id(chunk)` | 复算 doc_id：`compute_mdhash_id(sanitize_text_for_encoding(chunk["text"]), prefix="doc-")`，口径与 LightRAG `apipeline_enqueue_documents` 一致（`Reference/LightRAG/lightrag/lightrag.py:1447`） |
| 同上 | `PushChunksStage.execute` §5 分类，`timed_out` 计算后、`confirmed_count` 前 | 新增复查块：`if require_doc_ids and timed_out:` 遍历 timed_out，`_query_doc_status` 查该 doc；`processed` → `ctx.collected_doc_ids.append(did)` + 移出 timed_out；打 INFO 日志「② 超时确认复查——N 个超时 chunk 实际已 processed 判为已确认」 |

### 16.2 要点与依赖

- 复用 §十五 的 `_query_doc_status` + LightRAG vendored `GET /documents/{doc_id}` 端点；端点未部署时
  `_query_doc_status` 恒 None → 不会误确认（保守：仍判超时，退化为旧行为）。
- 仅在 **strict（require_doc_ids）** 且存在 timed_out 时触发；查询量 = 最终 timed_out 数（通常很小）。
- doc_id 复算口径必须与 LightRAG 同版一致（`sanitize_text_for_encoding` + `compute_mdhash_id(prefix="doc-")`），
  升级 LightRAG 时若改了内容规整/哈希，需同步本函数。

### 16.3 验证

- `py_compile Reference/Rag-anything/raganything/pipeline/stages.py` 通过。
- 需重建 `atomic-rag` 镜像生效：`docker compose build atomic-rag && docker compose up -d atomic-rag`。
- 场景：重跑该大文档 → 日志出现「② 超时确认复查——N 个超时 chunk 实际已 processed」，
  strict 不再因假超时回滚，文档最终 READY。

升级重放：改动集中在 `stages.py`，以 `# [jonex] ②` / `_expected_doc_id` 标记。


---

## 十八、raganything 入库计量 contextvar 透传（Gap A，2026-07-30）

> 关联设计：`docs/llm-usage-log-metering-dimension-gaps-fix-plan.md` §4.1、§11.2。
> raganything 直连 llm-gateway 的多模态描述 / summary LLM 与 embedding 调用缺 doc_id/trace_id
> 维度。采用 contextvar 方案：任务入口 set → driver + fallback 闭包每次调用现算注入。
> 所有改动带 `# [jonex]` 标记。

### 18.1 新增 contextvar 模块

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/jonex_metering_ctx.py` | **新增文件** | `set_ingest_ctx(tenant_id, kb_id, doc_id, trace_id)` / `get_ingest_ctx()` / `reset_ingest_ctx(token)` / `build_ingest_headers()` — 参照 LightRAG `jonex_metering.py` 的 contextvar 模式。`build_ingest_headers()` 从 contextvar 构造 X-Jonex-* 头 dict（scene 固定 `raganything_ingest`） |

### 18.2 任务入口 set/reset

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 内 `PipelineCtx` 构造完成后、try 前 | `set_ingest_ctx(tenant_id=task.tenant_id, kb_id=task.kb_id, doc_id=task.document_id, trace_id=task.task_id)` |
| 同上 | `finally` 块最前 | `reset_ingest_ctx(_ingest_token)` |

contextvar 在 `asyncio.create_task` 时复制进子 task，每任务独立 context，并发入库不串租户。

### 18.3 model_factory 计量头改造

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/model_factory.py` | `_build_metering_headers` | 新增 `doc_id` 参数；优先读 `get_ingest_ctx()` contextvar，parameter 作静态兜底；新增 `X-Jonex-Doc-Id` 头 |
| 同上 | `_metered_llm` | `_build_metering_headers()` **移入 `_llm` 闭包内每次现算**（原 build 时烘焙 → 不能随任务变化 doc_id/trace_id） |
| 同上 | `_metered_embedding` | 同上，`_build_metering_headers()` 移入 `_embed` 闭包内每次现算 |

> `_bind_with_overrides` 的 `extra_headers` 保持 build 时 tenant/kb 静态烘焙，per-task
> doc_id/trace_id 由 driver 层 contextvar overlay 覆盖（见 §18.4）。

### 18.4 driver 级 contextvar 叠加（覆盖 registry 主路径）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/models/drivers/openai.py` | `complete()` headers 构造 | lazy-import `build_ingest_headers`，`headers = {..., **spec.extra_headers, **_build_ingest_h()}` — contextvar 覆盖 bake 时静态值 |
| `raganything/models/drivers/anthropic.py` | `_send_request()` headers 构造 | 同上，且补回被遗漏的 `**spec.extra_headers`（此前该 driver 完全没发计量头） |

> **设计要点**：`_bind_with_overrides` 是 registry **主路径**（fallback 只在 bind 失败时用），
> 仅改 fallback 闭包（§18.3）而不改 driver 等于没改。driver 级一处改动同时覆盖 registry + VLM
> +（理论上）所有经 driver 的调用。

### 18.5 生效前提与验证

- **需重建 atomic-rag 镜像**：`docker compose build atomic-rag && up -d`。
- 验证：入库一篇文档 → `metering.llm_usage_log` 的 `scene=raganything_ingest` 行
  `doc_id`/`trace_id` 非空，`request_id` 不再 `auto:`（前缀=task_id）。
- 风险：若 raganything 多模态/summary 调用走线程池而非 `create_task`，contextvar 可能丢失
  → doc/trace 仍空但不报错（回退 static 兜底）。需真链路验证 LLM 与 embedding 行维度齐全。

升级重放：全局搜索 `[jonex]` 定位 `jonex_metering_ctx.py` 整文件、`task_manager.py` 的
`_ingest_token`/`set_ingest_ctx`/`reset_ingest_ctx`、`model_factory.py` 的 `get_ingest_ctx` /
闭包内 `_build_metering_headers`、`drivers/openai.py` `_build_ingest_h` /
`drivers/anthropic.py` `_build_ingest_h`/`spec.extra_headers`。


## 十九、§13.4 纵深防御：动态超时窗口 + 超时不重推在途 chunk（2026-07-29）

> 配合第十六节 ②，从"源头"减少超大文档长尾被误判超时→重推变 dup 的浪费。
> 均为纯防御、向后兼容（floor/仅放大；查状态后才决定重推）。1-B 最小回滚仍列 backlog（改一致性契约，单独评审）。

### 17.1 item1 — 轮询窗口按 chunk 数动态下限（floor）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.__init__` | 新增 `self._track_scale_per_chunk`(env `RAG_TRACK_SCALE_PER_CHUNK_SEC`,默认 2)、`self._track_scale_ceil`(env `RAG_TRACK_SCALE_CEIL_SEC`,默认 10800) |
| 同上 | `execute` `# ── 4.` 前 | 方法级计算 `eff_track_timeout`/`eff_per_chunk_timeout` = `clamp(base, SCALE×total_chunks, CEIL)`；保持 per-chunk = min(track×0.8, …) < global；`SCALE=0` 关闭回退固定值；放大时打 INFO |
| 同上 | `batch_track_status` 调用 | `max_wait_seconds`/`per_track_timeout_seconds` 改用 `eff_*` |
| 同上 | §5 `RAG_PUSH_TIMEOUT` 文案 | 秒数改用 `eff_track_timeout` |

效果：1878 chunk → track≈3756s(63min)、per_chunk≈3004s（原固定 1800/900 会把长尾误判超时）；小文档（floor 不触发）保持 1800/900 不变。**仅放大不缩小**，不会让任何场景更早超时。

### 17.2 item2 — 超时 chunk 重推前查真实 doc 状态

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | `execute` 重推段（`repush_idx` 构建，替换原 `for _t in timeout_tids` 无脑重推） | 对每个 timeout chunk 复算 doc_id 查状态：`processed`→收集 doc_id 判确认（不重推）；`pending/processing/preprocessed`→原 track 重新入 `new_pending_ids` 继续轮询（不重推、不造 dup）；`failed`/查不到→才 repush。打 INFO「已确认 N、继续轮询 M、待重推 K」 |

复用第十六节 `_expected_doc_id` + `_query_doc_status`。与 ②（最终分类兜底确认）互补：item2 在每轮重推前就拦截在途 chunk，避免制造 dup churn；② 在最终判定前再兜一次。

### 17.3 新增环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `RAG_TRACK_SCALE_PER_CHUNK_SEC` | `2` | 每 chunk 追加的轮询窗口秒数；0=关闭（回退固定 `RAG_TRACK_*`） |
| `RAG_TRACK_SCALE_CEIL_SEC` | `10800` | 动态窗口绝对上限秒（3h，< HARD 6h） |

### 17.4 验证

- `py_compile stages.py` 通过；需重建 `atomic-rag` 镜像生效。
- 场景：重跑超大文档 → 日志「§13.4 动态超时窗口 chunks=… track=…s」+「item2 超时复查——已确认/继续轮询/待重推」；
  长尾在放大窗口内首轮确认、重推与 dup 显著减少；strict 不再假超时回滚。

升级重放：改动集中在 `stages.py`，`# [jonex] §13.4` 标记。1-B 最小回滚未实现（backlog）。


## 二十、v2 分阶段耗时埋点（ingest_timing + reconcile_timing 增补，2026-07-30）

> 关联设计：`docs/ingestion-timing-metrics-design.md` §11。
> 补齐 v2 raganything pipeline 三处缺口：push_chunks 永不 close、worker 侧缺 ingest_timing 日志、
> 对账侧缺 pipeline_version 维度。所有改动带 `# [jonex] §11` 标记。

### 20.1 Gap A：push_chunks stage 永远不 close（task_manager.py）

**现象**：`ProgressTrackingCallback.on_document_complete` 先 push "done" 再 `_close_stage()`，
实际关了 "done" 而非 push_chunks。最耗时的推送/抽取段 `ended_at` 恒为 None → `elapsed_seconds` 无值。

**修复**：先 `_close_stage()` 关 push_chunks（由 `on_push_chunks_start` 打开、从未有 `_complete`
来关），再 push "pipeline_done" 再 close "pipeline_done"。管道中更早的 parse/text_insert/multimodal
由各自的 `on_*_complete` 逐一 close，不会被误关。

> **P1-2 改名**：stage key 从 `"done"` 改为 `"pipeline_done"`，避免 timeline 中
> push_chunks → done → ontology_extract 的"done 夹在中间"语义混乱。
> `ProgressDetail.step_name` 仍保留 `"done"`（前端契约不变）。

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `ProgressTrackingCallback.on_document_complete` | 先 `self._close_stage()` 再 `self._push_stage("pipeline_done")` 再 `self._close_stage()` |

### 20.2 Gap B：ontology 抽取已有 timeline（无需额外改动）

`_run_ontology_extraction` 早在 v1 就已手动 append `StageTiming(stage="ontology_extract")`，
并在 finally 内 close（带 `ended_at` / `elapsed_seconds`）。本节复查确认 Gap B 不存在，无需修改。

### 20.3 v2 ingest_timing 结构化日志（task_manager.py）

新增模块级 helper `_log_ingest_timing_v2(task, status, force_ontology_only, error)`：遍历
`task.timeline` 内白名单 stage 的 `elapsed_seconds`，摊平成 `{stage}_ms` + `worker_total_ms`
双写到 message（供 grep）+ extra（供 JSON 聚合），含 `pipeline_version=v2` 维度。

**P0 缺口 D 兜底 close**：失败/cancel 路径下回调不触发，当前活跃 stage 的 `ended_at` 为 None。
`_log_ingest_timing_v2` 开头遍历 timeline 把所有 `ended_at is None` 的 stage 就地 close（以
`datetime.now(timezone.utc)` 为 ended_at），确保"卡在哪个阶段"数据不丢。

**P1-1 worker_total_ms 口径**：`_QUEUE_KEYS = {"created", "queued"}` 排除排队等待时间，
`worker_total_s` 仅累加非排队 stage，对齐 v1「worker 从取出任务到结束」语义。created_ms /
queued_ms 仍保留在 extra 中供排查。

**P2 error 字段**：`error` 参数传递给 `_log_ingest_timing_v2`，失败/cancel 路径写
`error=getattr(task, "error_message", "")` 或 `error=str(e)[:200]`，成功路径传空字符串。
message 和 extra 均包含此字段。

白名单 `_V2_TIMING_STAGES` = `{created, queued, parse, text_insert, multimodal, push_chunks,
ontology_extract, pipeline_done}`。

调用点覆盖 `_execute_pipeline_http` 全部 4 个终端路径与 `_run_ontology_only` 全部 2 个路径：

| 路径 | status | force_ontology_only | error |
|------|--------|---------------------|-------|
| `_execute_pipeline_http` doc_id 成功 | `completed` | False | (空) |
| `_execute_pipeline_http` doc_id=None | `failed` | False | `task.error_message` |
| `_execute_pipeline_http` except TaskCancelledError | `cancelled` | False | `"task_cancelled"` |
| `_execute_pipeline_http` except Exception | `failed` | False | `str(e)[:200]` |
| `_run_ontology_only` 成功 | `completed` | True | (空) |
| `_run_ontology_only` CONFIG_INVALID | `failed` | True | `task.error_message` |

失败路径在 re-raise 前打日志：P0 兜底 close 保证未关闭 stage 有 ended_at → elapsed_seconds 非零；
P1-1 口径只计 worker 内耗时；P2 error 字段直写 message + extra。遵循 §3.4 A 方案甲双写约定。

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | 模块级 `_V2_TIMING_STAGES` + `_log_ingest_timing_v2()` | **新增**：v2 timeline → ingest_timing 日志摊平 helper |
| 同上 | `_execute_pipeline_http` success/failure/cancel/exception | 各路径插 `_log_ingest_timing_v2` |
| 同上 | `_run_ontology_only` success/CONFIG_INVALID | 各路径插 `_log_ingest_timing_v2(force_ontology_only=True)` |

### 20.4 对账侧 v2 兼容（reconciliation_service.py）

| 文件 | 位置 | 说明 |
|------|------|------|
| `capabilities/knowledge_base/services/reconciliation_service.py` | 模块级 `_V2_STAGE_KEYS` | **新增**：v2 stage 白名单 `{created,queued,parse,text_insert,multimodal,push_chunks,ontology_extract,pipeline_done}` — `_normalize_stage_timings` v2 list 形态仅保留白名单内 stage，其余静默跳过 |
| 同上 | 模块级 `_QUEUE_KEYS` | **新增**：`{created, queued}` — `_normalize_stage_timings` v2 分支排除排队键不算入 `worker_total_ms`（P1-1，与 task_manager 口径一致） |
| 同上 | `_normalize_stage_timings` v2 分支 | 加入白名单过滤 + 排队键排除 |
| 同上 | 模块级 `_detect_pipeline_version()` | **新增**：list → `v2`，dict → `v1`，None/empty → None |
| 同上 | `_handle_completed` reconcile_timing 日志 | 新增 `pipeline_version` 字段（message 内嵌 + extra 结构化），便于大盘按管道版本聚合 |

### 20.5 验证

- `py_compile` 所有改动文件通过。
- 需重建 `atomic-rag` 镜像并重启 knowledge-base-service 生效：
  ```bash
  docker compose build atomic-rag && docker compose up -d atomic-rag knowledge-base-service
  ```
- 验证要点（见设计 §11.6）：
  - 入库一篇文档 → `docker logs jonex-atomic-rag | findstr ingest_timing` 出现
    `pipeline_version=v2` 且 `push_chunks_ms` / `ontology_extract_ms` 非空
  - `reconcile_timing` 日志含 `pipeline_version=v2` 及各阶段耗时
  - 失败场景：`status=failed` 仍含已完成阶段耗时

### 新增环境变量（無）

复用已有 `INGEST_TIMING_ENABLED`（`jonex_core/common/timing.py`），`_log_ingest_timing_v2`
内部调用 `timing_enabled()` 检查。关闭 → 不产生任何 timing 日志，行为向后兼容。

升级重放：全局搜索 `# [jonex] §11` 定位 `task_manager.py` 的 Gap A 修复 + `_log_ingest_timing_v2`
+ 6 个调用点，以及 `reconciliation_service.py` 的 `_V2_STAGE_KEYS` / `_detect_pipeline_version`
+ `_handle_completed` 的 `pipeline_version` 字段。

---

## [jonex] OpenKB 链路：解析产物落共享卷 + task status 暴露路径

> 目的：让 knowledge_base 的 `pipeline_type=openkb` 分支能拿到「解析出的 markdown + 图片」，交给 OpenKB 容器编译。解析产物原本只落在容器本地 `parser_output_dir`（jonex-rag-storage），openkb 读不到；这里额外把产物写到共享卷 `jonex-rag-inputs`（atomic-rag 挂 `/app/inputs`，openkb 挂 `/app/data/inputs`，同卷不同挂载点）。

改动点（均标 `# [jonex]`）：

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | 新增方法 `TaskManager._write_openkb_artifact(task, ctx)` | 把 `ctx.content_list`（MinerU blocks，img_path 已是绝对路径）序列化为 `content.md`（text/table/equation/image），图片复制到 `assets/`，写到 `{RAG_INPUTS_DIR:-/app/inputs}/parsed/{document_id}/`；相对路径 `parsed/{document_id}/content.md` + `assets` 记入 `task.result_summary.extensions` 与 `task.storage`。best-effort，异常不影响主链路；`RAG_OPENKB_ARTIFACT_ENABLED`（默认 on）可关。 |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 内 `task.result_summary = summary` 之后 | 调用 `self._write_openkb_artifact(task, ctx)`。 |
| `atomic-rag-server-v2.py` | `handle_get_task_status` 返回 data | 顶层新增 `parsed_markdown_path` / `assets_dir`（取自 `task.result_summary.extensions`），供 KB 对账读取。 |

新增环境变量：

- `RAG_INPUTS_DIR`（默认 `/app/inputs`）：共享 inputs 卷在 atomic-rag 容器内的挂载点，OpenKB 产物写此目录下 `parsed/{document_id}/`。
- `RAG_OPENKB_ARTIFACT_ENABLED`（默认 `true`）：总开关。

消费侧（非本仓）：`capabilities/knowledge_base/services/reconciliation_service.py::_dispatch_openkb_compile` 读 task status 的 `parsed_markdown_path`/`assets_dir`，换成相对 inputs 卷根路径后交 `atomic.openkb.v1 compile_parsed_document`；OpenKB adapter 用 `OPENKB_INPUT_ROOT`（`/app/data/inputs`）解析该相对路径读到同一份文件。

### 追加：`execution_mode=parse_only`（OpenKB KB 级互斥）

为让 `pipeline_type=openkb` 的文档「只解析、不写 LightRAG/Neo4j」，新增 parse-only 执行模式：

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 分支 | `task.execution_mode == "parse_only"` → 调 `_run_parse_only`，跳过 multimodal/push_chunks/ontology。 |
| `raganything/service/task_manager.py` | 新增 `_run_parse_only(task, handle)` | 调 `self._pipeline_executor.parse_document(...)` 得 content_list（复用解析缓存），写 `_write_openkb_artifact`，置任务 COMPLETED。不入库、不抽本体。 |

`execution_mode` 已由 `handle_insert`(atomic-rag-server-v2.py L361) → `CreateTaskRequest` → `task.execution_mode` 原样透传，无需改 server。

消费侧（非本仓）：`knowledge_base/services/document_service.py` 对 `pipeline_type=openkb` 文档以 `execution_mode="parse_only"` 调 `insert`；`client.py`(Remote/Local/Mock/abstract) 与 `lightrag_adapter_v2.insert` 已加 `execution_mode` 透传；`reconciliation_service._handle_completed` 对 openkb 文档标 `ontology_status=READY`，避免本体重试循环。

### 修复：handle_insert 透传 execution_mode

`atomic-rag-server-v2.py::handle_insert` 的 `CreateTaskRequest` 此前**未透传 `execution_mode`**（仅 retry handler 透传），导致 `pipeline_type=openkb` 文档虽由 knowledge_base 传了 `execution_mode=parse_only`，到 atomic-rag 后仍按 `full` 执行（照常入 LightRAG + 抽本体）。已补 `execution_mode=params.get("execution_mode", "full") or "full"`，使 parse_only 生效（只解析、落 OpenKB 产物、跳过入库/本体）。

> 相关无关联但同批定位的部署缺陷（不在本仓）：
> - `deploy/docker/openkb-source.Dockerfile`：capability 依赖此前经 `uv pip install`（UV_SYSTEM_PYTHON）落到 `/usr/local`，运行时用 `/app/.venv` → sqlalchemy 等缺失致 openkb 容器崩溃循环。改为 `uv pip install --python /app/.venv/bin/python` 并补 sqlalchemy/greenlet/asyncpg/neo4j/bcrypt。
> - `jonex_core/capability/locator.py::_normalize_atomic_id`：0 点短名（`openkb`）漏 `.v1`，致 `get_openkb_client("atomic.openkb.v1")` 回退 LOCAL。改 `count(".") <= 1` 统一补 `.v1`。

### 补充：OpenKB 产物图片引用路径对齐

`_write_openkb_artifact` 序列化 image 块时,markdown 引用从 `assets/<name>` 改为 `images/<document_id>/<name>`。原因:knowledge_base 的 openkb adapter 会把 `assets_dir` 复制到 `wiki/sources/images/<document_id>/`,而 source md 位于 `wiki/sources/<document_id>.md`,故相对引用须为 `images/<document_id>/<name>` 才能在 OpenKB Wiki 中正确显示(符合 OpenKB 短文档图片约定)。adapter 侧同步移除了「图片引用未重写」的 warning。
## §12 `2026-08-03` — 多模态 chunk 页码缺失修复（方案⑧ 配套）

### 背景

MinerU 解析产出中，图片 item 的 `page_idx` 在顶层 dict（与文本 item 同源），
但 `separate_content` 将 item 原样放入 `multimodal_items[].original`，只创建了空 `item_info={}`。
`_collect_multimodal_chunks` / VLM 上下文窗口从 `item_info.get("page_idx")` 取 → `None` →
`_build_file_source` 不写 `page=` → `to_location` 退化为 `type=chunk`（无页码）。
同一份文档的文本 chunk 正确拿到了 `page=11`，证明 MinerU 本身产出了页码；只是多模态链路读错了字段。

### 改动

**`raganything/pipeline/stages.py`** — `_collect_multimodal_chunks` L1653
- `page = item_info.get("page_idx")` 之后加 `original.get("page_idx")` 兜底

**`raganything/modalprocessors.py`** — VLM 上下文窗口 `current_page` L185
- 同步加 `original.get("page_idx")` 兜底

### 影响

修复前：PDF 图片引用一律退化为 `type=chunk`（无页码）。
修复后：图片引用与文本引用同源，`type=page` + `page_no` 正确，前端可跳转页码。

### 重建

改动在 vendored `Reference/Rag-anything/**` → 需重建 `atomic-rag` 镜像 + 重抽目标 KB。

升级重放：全局搜索 `# [jonex] §12` 定位两处 `page_idx` 兜底。


## §13 `2026-08-10` — COS 文件存在性校验 + error_code 透传 + 失败日志（P0-1 / P0-a）

### 背景

线上文档 `0d1fe634` 的 task 创建瞬间失败 `FILE_NOT_FOUND`：
`_validate_and_enqueue` 对所有任务做 `os.path.exists(task.file_path)`，但 COS 后端的
`file_path` 是 storage_key 标识符而非本地路径 → 必返回 False → 误判 FILE_NOT_FOUND。

同时 `_fail_task` 只写 task 字段不写日志 → KB 对账 `_finalize_failure` 读 `status_info.error`
(`task.error_message`) 时可能拿到空值 → 生成误导性「任务确认丢失」文案掩盖真实错误。

此外 `get_task_status` 响应不包含 `error_code` 字段 → KB 侧 P1-5 确定性终态短路
（FILE_NOT_FOUND / OBJECT_FETCH_FAILED 直接落 FAILED 跳过 3 拍确认）永远不触发。

### 改动

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `_validate_and_enqueue` L1100 | `# [jonex] P0-1`：COS 后端（`storage_backend=="cos"` 且 `storage_key` 非空）跳过 `os.path.exists` 本地路径检查，文件由 pipeline 阶段从 COS 按需下载；非 COS 仍走旧逻辑 |
| 同上 | `_fail_task` L2240 | `# [jonex] P0-1`：补 `logger.error`（含 task_id / error_code / message），防止 KB 对账拿不到真实错误文案 |
| `atomic-rag-server-v2.py` | `handle_get_task_status` L299 | `# [jonex] P0-a`：响应 `data` 新增 `"error_code": task.error_code.value if task.error_code else None`，供 KB 侧 P1-5 确定性终态短路使用 |

### KB 侧配套（不在本目录，登记以便追溯）

| 文件 | 改动 |
|------|------|
| `capabilities/knowledge_base/services/reconciliation_service.py` | `_handle_failed` P1-5：`error_code` 匹配 + `error_msg` 文本前缀兜底确定性终态直接落 FAILED |
| 同上 | P0-2/P0-3/P0-新/P0-b/P1-c/P1-d/P1-4 `death_verdict` 全链路整改 |

### 重建

需重建 `atomic-rag` 镜像：`docker compose build atomic-rag && docker compose up -d atomic-rag`。

升级重放：全局搜索 `# [jonex] P0-1` / `# [jonex] P0-a` 定位三处改动。

---

## §14 `2026-08-12` — 音频全局摘要 NameError 修复（prompt_overrides 透传）

### 背景

parse_only 多模态文本化（openkb 视频/音频产物空洞修复）调用 audio
`generate_description_only` 时，vendored 代码抛 `NameError: name 'prompt_overrides' is not defined`
（`_recursive_mapreduce` 内部 `_final_synthesize(..., prompt_overrides=prompt_overrides)`，
但 `_recursive_mapreduce` 签名没有该参数）。full 模式隐性存在（摘要丢但 chunks 在，
stages 兜底）；parse_only 由 `_textualize_multimodal` 的 except 兜底捕获，但会丢转写正文。

### 改动点（`raganything/modalprocessors.py`）

| 位置 | 改动 |
|------|------|
| `AsrModalProcessor.generate_description_only`（~1779） | 调用 `_recursive_mapreduce` 时透传 `prompt_overrides=prompt_overrides`（`# [jonex]` 标记） |
| `AsrModalProcessor._recursive_mapreduce` 签名（~1982） | 新增参数 `prompt_overrides=None`（`# [jonex]` 标记） |

### 配套（不在本目录，登记以便追溯）

- `task_manager.py` `_textualize_multimodal` except 分支：ASR 已写 `_audio_segments`
  时用转写正文兜底（不丢已付费用的正文），只记 `{t}_summary_failed` warning——
  vendored 其他缺陷也不丢正文。

### 重建

需重建 `atomic-rag` 镜像。

升级重放：全局搜索 `# [jonex] 修复 NameError：_final_synthesize 需要该参数` 定位两处改动。

## §15 `2026-08-13` — chunk 类型与表格元数据写入 file_source（ctype 贯通）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` 改动 36~38，§17.2 第 3 步）
需要查询侧能区分 chunk 类型（表格明细行 / 表格摘要 / 文本 / 图像 …）以做双路召回与低质过滤。
LightRAG 的 `InsertTextRequest` 只有 `text` + `file_source` 两个字段，无独立元数据通道，
故把 `ctype` / `table_sig` / `table_cols` 编码进 `file_source`（与 `row_start` 等锚点同一旁路机制）。

### 改动点（`raganything/pipeline/stages.py`）

| 位置 | 改动 |
|------|------|
| `_build_file_source` 签名（~1062） | 新增 kw-only 参数 `ctype` / `table_sig` / `table_cols`；拼入 `parts`（`ctype=` / `table_sig=` / `table_cols=`，`table_cols` 值内的 `|` 替换为空格防破坏分隔结构） |
| `_collect_text_chunks` 表格分支（~1742） | 表格明细行 chunk 打 `ctype="table_row"` |
| `_collect_text_chunks` 通用文本分支（~1805） | 打 `ctype=t`（与 chunk `type` 字段一一对应） |
| `_collect_multimodal_chunks` 摘要分支（~1854） | `content_type == "table"` → `ctype="table_summary"`（表格摘要与明细行区分）；其余模态沿用 `content_type` |
| `_collect_multimodal_chunks` 视频帧（~1875）/ ASR 段（~1905） | 分别打 `ctype="video_frame"` / `ctype="audio_segment"` |

### 配套（不在本目录，登记以便追溯）

- `jonex_core/common/file_source_util.py` `parse_file_source` 白名单新增 `chunk_type`（读 `ctype`）、
  `table_sig`、`table_cols`（改动 36）；旧 chunk 无此键 → `None`，向后兼容。
- 单测：`tests/unit/test_file_source_ctype.py`（往返契约、`|` 转义、旧格式兼容）。

### 重建

需重建 `atomic-rag` 镜像。

升级重放：全局搜索 `ctype=` / `table_sig=` 定位 `_build_file_source` 及 5 个写入点改动。

## §16 `2026-08-13` — 表格 L1 网格化 + L2 表头推断（§table-grid-v2）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` §3 L1/L2，§17.2 第 6 步）：

- T1：旧 `_HTMLTableRowExtractor` 的 rowspan 按列索引占位 + 本行 cell 纯 append，
  例外行整体右移（「AU6、12月份合约」跑到 col_6/col_7，与列名脱钩）；
  colspan 只在首格有值、其余留空，多级表头拼不到父级名。
- T2：首行即表头。审批权限表首行是「总体原则+300 字」合并行 → 全表 col_N 兜底
  （线上 118/1063 chunk 带 `col_`）；摘要链路（`format_table_body`）基于同一错误表头。

### 改动点

| 位置 | 改动 |
|------|------|
| `utils.py` 新增 `_HTMLTableGridExtractor` | 两遍扫描网格填充：colspan 向右复制、rowspan 向下复制、交叉格 4 格同值；cell 落位改为「下一个未被占用列」；输出严格 n_cols 矩形 grid + 每行 `has_merge`（合并格信号，供 L2 区分多级表头与数据行） |
| `utils.py` 新增 `normalize_table_grid()` | 三元返回 `(header, data_rows, meta)`；meta = `{header_levels, notes, n_cols}`；HTML / list / markdown 三种输入统一矩形化 |
| `utils.py` 新增 L2 私有函数 | `_is_pure_number` / `_forms_numeric_sequence`（严格单调定步长，≥3）/ `_table_has_date_semantic`（星期/周X 正则）/ `_is_prose_cell`（>TABLE_HEADER_MAX_LEN 或句末符 → 表前说明；顿号须叠加超长，避免误杀「交易品种、合约」短表头）/ `_is_header_row` / `_compose_header`（多级拼接 `-`，占位符 `—/-//` 跳过）/ `_infer_table_header` |
| `utils.py` 新增 `_table_grid_v2_enabled()` | 开关读取：未设置默认 true（走新路径）；显式 false 回退旧路径（§5 灰度表） |
| `utils.py` `format_table_body` | 切到 `normalize_table_grid`（开关分叉）；notes 渲染为表后「【表前说明】」段落——摘要链路与明细行基于同一份推断表头（§13.5） |
| `utils.py` `normalize_table_rows` | markdown 分支改用共享的 `_extract_markdown_rows`；**行为不变**（旧路径回滚语义） |
| `pipeline/stages.py` `_collect_text_chunks` 表格分支 | 按开关切 `normalize_table_grid`（meta 暂未消费，步 10 用）；`PushChunksStage.__init__` 启动打印开关生效值 |
| `tests/test_table_grid_v2.py` | 新增 18 用例：T1 复现（rowspan 例外行）、colspan 右复制、colspan×rowspan 交叉、不规则行宽、多级表头+300 字 notes、年份序列/孤立数字/重复值、短顿号表头保留、markdown/list 输入、空表边界、format_table_body 一致性、开关回滚、旧函数不变 |

### 表头推断规则摘要（实现比文档字面多两条防误杀）

1. 表前说明行（prose）→ notes 剔除，继续扫描；
2. 上一表头行**满行且无合并格** → 下一行判数据（防姓名/文本数据行被吃进表头）；
3. 数字判定：孤立数字 → 度量值；定步长单调序列（≥3）→ 合法表头，但**序列行只做首层表头**
   （`2023|2024|2025` 表头 + `100|120|130` 逐年数据是常见形态）；单格数字仅在整表存在
   日期/星期语义时判表头（Excel 序列号单列形态）；
4. 行内非空值重复率 > 50% → 数据行。

### 新增环境变量

| 变量 | 默认 | 语义 |
|------|------|------|
| `RAG_TABLE_GRID_V2` | `true`（未设置即生效） | 网格化+表头推断总开关；显式 false 回退旧路径 |
| `TABLE_HEADER_MAX_LEN` | `40` | 表头长文本剔除阈值（字符） |

⚠️ 变量由 **atomic-rag（raganything）进程读取**，须配在 `deploy/.env`（atomic-rag 的
`env_file=.env`）与本地调试的 `.env.local` / `.env.local.example`；**不要**放进
`.env.rag*`（lightrag 容器专用，atomic-rag 读不到）。

### 重建

需重建 `atomic-rag` 镜像。回滚：`RAG_TABLE_GRID_V2=false` 后重启即可（旧函数保留）。
升级重放：全局搜索 `table-grid-v2` 定位全部改动点。

## §17 `2026-08-13` — 表格专用切块预算 + 可观测统计（§table-grid-v2 O1/O4）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` §9.2 O1/O4，§17.2 第 7 步）：

- C1 双重切分：平台按 12000 字符打包后 LightRAG 再按 1200 token 硬切，表头只留在子块 0、
  row 锚点错位（线上 37% doc 被二次切分、60% chunk 恰为 1200 token）。
- C4 静默降级：表格归一化失败无告警无计数（线上 118/1063 chunk 带 `col_` 无人知晓）。

### 改动点

| 位置 | 改动 |
|------|------|
| `pipeline/stages.py` `PushChunksStage.__init__` | 读 `RAG_TABLE_CHUNK_MAX_CHARS`（默认 900）+ `RAG_LIGHTRAG_CHUNK_SIZE`（默认 1200）；`_table_chunk_body_budget = max(200, table_max - 64)`；惰性加载 `_load_table_tokenizer()`（lightrag `TiktokenTokenizer("gpt-4o-mini")`，与 LightRAG 二次切分同口径，加载失败则断言跳过）；启动日志打印预算/阈值/开关 |
| `pipeline/stages.py` `_load_table_tokenizer()` | 新增：tiktoken 惰性加载，失败返回 None 不阻断推送 |
| `pipeline/stages.py` `_count_col_unnamed()` | 新增：`col_N` 占位列名计数 |
| `pipeline/stages.py` `_collect_text_chunks` | 签名新增 `stats` 入参；表格分支：① 按 `RAG_TABLE_CHUNK_MAX_CHARS` 预算切分（**文本链路仍用 `RAG_CHUNK_MAX_CHARS`，零变化**）；② tokenizer 实测每个表格 chunk，超 `RAG_LIGHTRAG_CHUNK_SIZE` → WARNING + `oversize_table_chunks` 计数；③ 归一化失败 fallback 打 WARNING + `tables_fallback` 计数（不再静默） |
| `pipeline/base.py` `PipelineContext` | 新增 `table_stats: dict` 字段（O4 统计载体） |
| `service/task_manager.py` | push 完成后把 `ctx.table_stats` 写入 `result_summary.extensions["table_stats"]`（KB 侧可读 `cols_unnamed` 留文档级告警） |
| `tests/test_pipeline/test_push_chunks_stage.py` | 新增 `TestTableGridStats` 6 用例：计数口径、col_N 统计、小预算多 segment、超限断言计数/不阻断 |
| `scripts/calibrate_table_chunk_budget.py` | 新增校准脚本：内置样本（中文/数字/长文本三形态）或外部样本文件 → char→token 比率 P95 → `floor(CHUNK_SIZE / p95 × 0.9)` 反推预算。内置样本实测 p95≈1.28 → 建议 844；900 配置值下 900×1.28≈1151 < 1200 仍安全。容器内可用真实表格 chunk 样本重新校准 |

### 新增环境变量

| 变量 | 默认 | 语义 |
|------|------|------|
| `RAG_TABLE_CHUNK_MAX_CHARS` | `900` | 表格专用切块预算（字符），仅表格分支生效；文本链路不变 |
| `RAG_LIGHTRAG_CHUNK_SIZE` | `1200` | 超限断言阈值（token），须与 `.env.rag` 的 `CHUNK_SIZE` 一致 |

⚠️ 同为 atomic-rag 进程读取，配置位置同 §16（`deploy/.env` / `.env.local`，勿放 `.env.rag*`）。

### 重建

需重建 `atomic-rag` 镜像。回滚：`RAG_TABLE_CHUNK_MAX_CHARS` 调回 12000 即恢复旧切分粒度（表格二次切分随之恢复）。
升级重放：全局搜索 `table-grid-v2 O1` / `table_stats` 定位全部改动点。

## §18 `2026-08-13` — md 内嵌表格探测（§table-grid-v2 O2）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` §9.2 O2，§17.2 第 8 步）：

- C2：md 文件里内嵌的 HTML `<table>` 在 content_list 中 `type=="text"`，从未走表格分支，
  被 LightRAG 按 1200 token 从标签中间硬切（实测 chunk 0 结尾是 `<td rowspan="6">…<`、
  chunk 1 开头是裸 `>\n</tr>`）。通用知识库 10 个 `解析_md_*.md` 文档都是这种形态。

### 改动点

| 位置 | 改动 |
|------|------|
| `utils.py` 新增 `extract_embedded_tables()` | text block 拆分为交替 text/table 段：pass 1 按 `<table>…</table>`（IGNORECASE/DOTALL）切；pass 2 对 text 段扫连续 ≥3 行 markdown 管道行成表段；纯空白 text 段不产出；无表格时返回单 text 段 |
| `pipeline/stages.py` `_collect_text_chunks` 重构 | 拆成调度器 + `_emit_table_chunks`（表格分支，含 O4 统计/超限断言，支持 `table_idx_override`）+ `_emit_text_chunks`（文本分支，`ctype`/`table_idx`/`with_char_offsets` 参数化）。text 分支接入 O2 探测：多段时表格段走表格分支（table_idx 递增）、前后文本单独成 chunk（拆段后 char 偏移置空，只保留 page 锚点）；单段走原路径。表格 fallback 复用 `_emit_text_chunks`（ctype="table"） |
| `tests/test_table_grid_v2.py` | 新增 `TestEmbeddedTables` 6 用例：HTML 中间表/纯文本/管道表/不足 3 行/多表/HTML+管道共存 |
| `tests/test_pipeline/test_push_chunks_stage.py` | 新增 O2 集成 2 用例：text block 内嵌 HTML 表 → table_row + 前后 text 各成 chunk（裸 HTML 不再混入文本）；管道表 → 表头推断识别年份序列 |

### 与后续步骤的衔接

拆段后的前文 text chunk 即 L4.1 `_resolve_table_caption` 的 prev_block 候选（步 10）；
O2 与 L3（xlsx 双路合并）共用 `_emit_table_chunks` 入口。

### 重建

需重建 `atomic-rag` 镜像。回滚：`RAG_TABLE_GRID_V2=false`（探测与网格化同开关）。
升级重放：全局搜索 `extract_embedded_tables` / `_emit_table_chunks` 定位全部改动点。

## §19 `2026-08-13` — xlsx 双路合并 + Excel 序列号启发式（§table-grid-v2 L3）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` §3 L3，§17.2 第 9 步）：

- T3：mineru_online 对 xlsx 不做前置格式化，拿不到 `number_format`——线上
  「2025年4月.xlsx」表头是 `45761 / 45762`（Excel 日期序列号），日期→星期→
  菜品三维对应彻底丢失。

### 改动点

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/spreadsheet_normalizer.py`（**新增，平台侧**） | openpyxl `data_only=True` 读取；逐格按 `number_format` 渲染：日期 → `2025-04-14（周一）`、百分比 `0.065` → `6.5%`、货币 → `¥1,280.00`；每个 sheet → HTML `<table data-jonex-native="1">`（指纹供合并幂等），全空行分隔多表、`table_idx` 递增；item 字段与 MinerU 完全对齐（`table_body`/`table_caption` list/`page_idx`=sheet 序号/`img_path`=""）；任何失败返回 `[]` 回退 MinerU 全量；`merge_native_tables()` 双路合并（MinerU 表格项丢弃计数 + 图像/公式项保留，按 page_idx 稳定排序，幂等） |
| `pipeline/stages.py` `ParseStage` | 开关 `XLSX_NATIVE_NORMALIZE`（未设置默认 true）；cache 命中与解析完成两条路径都执行合并（幂等，合并后重算 doc_id）；`mineru_table_dropped` 写入 `ctx.table_stats`；`__init__` 打印生效值 |
| `pipeline/stages.py` `_merge_xlsx_native_tables()` | 新增：跨包 import 平台侧 normalizer，ImportError 时静默回退（上游升级不炸） |
| `utils.py` `_normalize_cell_value()` | 新增 L3 兜底轨：纯整数落在 `[1, 100000]` 且同表存在日期/星期语义信号 → 转 `YYYY-MM-DD（周X）`；守门只用日期信号（窗口刻意放宽覆盖历史档案年份）；小数/百分号/窗口外一律不转 |
| `utils.py` `normalize_table_grid` | L1 矩形化后、L2 表头推断前执行 cell 规范化（规范化先于推断——「已被 L3 规范化成日期的单元格按序列规则判定」）；命中数计入 `meta["dates_heuristic"]` |
| `pipeline/stages.py` `_emit_table_chunks` | `meta["dates_heuristic"]` 累进 `stats["dates_heuristic"]`（table_stats 透出） |
| 依赖 | `openpyxl>=3.1.0,<4.0.0` 加入 `pyproject.toml` + `deploy/docker/atomic-rag-requirements.txt`（容器镜像） |
| 测试 | `tests/unit/test_spreadsheet_normalizer.py` 新增 6 用例（渲染/字段对齐/合并丢弃与幂等/坏文件回退）；`test_table_grid_v2.py` 新增 `TestExcelSerialDates` 4 用例（L5 用例 8/9 + 守门边界） |

### 新增环境变量

| 变量 | 默认 | 语义 |
|------|------|------|
| `XLSX_NATIVE_NORMALIZE` | `true`（未设置即生效） | xlsx 双路合并总开关；显式 false → 全量交 MinerU |

⚠️ 配置位置同 §16：`deploy/.env`（atomic-rag env_file）/ `.env.local`，勿放 `.env.rag*`。
⚠️ 跨包依赖：vendored ParseStage → 平台侧 `jonex_core.spreadsheet_normalizer`（容器内
jonex_core 已 COPY 到 /app；ImportError 兜底回退）。**上游升级重放时保留 try/except import 结构。**
⚠️ 不支持旧版 .xls（openpyxl 仅 xlsx）——主轨返回空时回退 MinerU 全量，不丢数据。

### 重建

需重建 `atomic-rag` 镜像（新增 openpyxl 依赖）。回滚：`XLSX_NATIVE_NORMALIZE=false`。
升级重放：全局搜索 `xlsx` / `spreadsheet_normalizer` / `_normalize_cell_value` 定位全部改动点。

## §20 `2026-08-13` — 单行上下文头 + 表标题来源链 + 分组切分 + 摘要中文化（§table-grid-v2 L4.1/L4.3/O3/L4.2 入库侧）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` §3 L4.1/L4.3、§9.2 O3、§3 L4.2 入库侧，§17.2 第 10 步）：

- T5：表格行 chunk 缺主体上下文（「顾雅文」行无「招商期货」字样 → 主体消歧失败）；
- C3：字符预算切分把逻辑分组（如交易所下的品种组）切开；
- T4：摘要 prompt 未切中文 → 线上 104/1063 英文摘要，且摘要内容含统计性臆测。

### 改动点

| 位置 | 改动 |
|------|------|
| `utils.py` `_valid_caption()` | 新增：caption 有效性校验（None/[]/""/空白/字面量 "None"/"none"/"null"/"N/A"/展平空/超 60 字 → 无效） |
| `utils.py` `pack_rows()` | 新增 `caption` 入参：每段正文前缀单行「表格明细：【表】标题」，头部 ≤ 8% 预算（超长截断前 30 字 + …）；新增 O3 分组感知——`_detect_group_column`（变化次数 ≤ 行数一半的列，只看前 8 列）作分组键，段在分组边界优先断开，组内续块标题追加「（续：分组值）」；`caption=None` 时行为与旧版一致 |
| `pipeline/stages.py` `_resolve_table_caption()` | 新增：表标题 4 级来源链（有效 table_caption → 向前 ≤3 block、≤60 字、非句末、无换行的 text block → sheet 名（预留）→ 文件名兜底）；返回 `(caption, source_level)` |
| `pipeline/stages.py` `_table_signature()` | 新增：列名签名（md5 前 8 位，table_sig 旁路） |
| `pipeline/stages.py` `_emit_table_chunks` | caption 解析 + `caption_source` 统计；`table_sig`/`table_cols`/`notes` 写 file_source（正文只保留单行标题，元数据不占 embedding）；`content_list`/`item_idx` 入参（O2 拆段与主路径共用） |
| `pipeline/stages.py` `_build_file_source` | 新增 `notes` 参数（值内 `|` 转义空格） |
| `pipeline/stages.py` `_collect_multimodal_chunks` | 表格摘要正文前缀「表格概览：」（与明细行「表格明细：」在 embedding 空间区分，与 ctype 双保险） |
| `jonex_core/common/file_source_util.py` | `parse_file_source` 白名单新增 `notes`（读 `notes` 键） |
| `jonex_core/.../lightrag_adapter_v2.py` | `_assemble_stack` 调 `set_prompt_language(RAG_PROMPT_LANG)`（默认 zh），进程级全局，须在 RAGAnything 构造前调用；失败回退 en 打 WARNING |
| `prompts_zh.py` | 收紧 `table_prompt` / `table_prompt_with_context`：必须列主体名 + 列名清单 + 行数；禁止统计性推断/趋势猜测/臆测 |
| 测试 | `test_table_grid_v2.py` 新增 7 用例（L5 用例 10/12、O3 分组断开与续块、caption 无效全枚举）；`test_push_chunks_stage.py` 新增 L4.1 集成 + `TestResolveTableCaption` 8 用例（4 级优先级、防误抓：句末/距离/超长/换行） |

### 新增环境变量

| 变量 | 默认 | 语义 |
|------|------|------|
| `RAG_PROMPT_LANG` | `zh` | 解析/摘要 prompt 语言；进程级全局，影响所有模态（§6 风险表：dev 全模态回归后再上生产） |

⚠️ 配置位置同 §16（`deploy/.env` / `.env.local`）。L4.3 锚点（row_start/row_end/table_idx）代码已支持，
属存量数据问题，随第 13 步 reparse 回填。

### 重建

需重建 `atomic-rag` 镜像。回滚：`RAG_TABLE_GRID_V2=false`（caption 头/签名随网格化同开关）。
升级重放：全局搜索 `_resolve_table_caption` / `_table_signature` / `表格明细：` / `表格概览：` 定位全部改动点。

## §21 `2026-08-13` — 废弃 structure_aware_chunk 死代码（§table-grid-v2 O5，单独提交）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` §11.4 O5，§17.2 第 11 步）：

- `structure_aware_chunk`（utils.py）只被 `separate_content` 调用，**不在推送链路上**
  （推送走 `_collect_text_chunks`）——`RAG_STRUCTURE_AWARE_CHUNK=true` 已配置却毫无效果；
- `_enrich_table_row_text` 只认 markdown `#{1,3}` 标题，认不出 docx/xlsx 的普通文本标题；
- 保留两套表格路径只会在排查时误导（本次根因分析就在这上面绕过弯）。

### 改动点（**单独提交，便于 git revert**）

| 位置 | 改动 |
|------|------|
| `utils.py` | 删除 `structure_aware_chunk` / `_enrich_table_row_text` / `_classify_block_type` / `_LIST_ENTRY_PATTERNS` / `_STRUCTURE_AWARE_ENABLED`；`separate_content` 中删除调用行（`_text_meta` sentinel 本身保留——`extract_text_metadata` 仍消费它）；原位留废弃说明注释 |

表标题回填能力已收敛到 L4.1（§20 的 `_resolve_table_caption` + pack_rows caption），
标题来源不限 markdown heading。

`RAG_STRUCTURE_AWARE_CHUNK` 变量已在批次 1 从 `deploy/.env.rag*` 移除（死代码 +
配置位置错误双重原因），本步为代码侧删除。无新增环境变量。

### 重建

需重建 `atomic-rag` 镜像。回滚：`git revert` 本提交即可（删除与 L1/L2 切换不同提交）。
升级重放：上游升级后若 `separate_content` 附近出现 `structure_aware_chunk` 调用，勿恢复。

## §22 `2026-08-13` — reparse 新旧格式残留校验（§table-grid-v2 O7，平台侧）

### 背景

表格治理方案（`docs/table-parsing-retrieval-governance-plan.md` §11.3 O7，§17.2 第 12 步，
**必须在首次全量 reparse 之前就位**——否则第一次切换就可能留下新旧格式混存且无人察觉）：

- 表格格式一变，`content_doc_id`（按文本 hash）全部改变；`reparse_strict` 会先删旧 doc
  再推新，但删除失败时新旧格式会在向量库共存，检索混入旧格式碎片，很难察觉。

### 改动点（均在平台侧，vendored 零改动）

| 位置 | 改动 |
|------|------|
| `capabilities/knowledge_base/services/document_service.py` | 新增 `_is_stale_table_chunk(content, file_path)` 格式指纹：新格式必带 `table_sig=`（L2 兜底 `col_N` 不误判）；旧格式 = 无 `table_sig` 且（`col_` 占位 或 `<table/<td/<th` 硬切残片）。新增 `scan_stale_chunks()`（按 `doc=` 锚点扫 LightRAG chunk 列表，返回 total/stale_chunk_count/stale_chunks 明细）、`purge_stale_chunks()`（逐 chunk_id delete 定点清理，并发上限 5，失败不阻断） |
| `capabilities/knowledge_base/services/reconciliation_service.py` | `_handle_completed` READY 收尾时执行残留扫描（openkb 跳过；失败不影响收尾）：残留 > 0 → `extra_metadata.stale_chunks_detected`（count + chunk_ids 前 20 + 时间戳，前端文档详情可见）；无残留 → 清掉旧 key（reparse 成功后旧警告不残留，与 multimodal_warnings 同机制） |
| `capabilities/knowledge_base/capability.py` | 注册 `scan_stale_chunks` / `purge_stale_chunks` 两个 action（`document_id` 参数） |
| `tests/unit/test_stale_chunk_scan.py` | 新增 7 用例：新格式 col_N 兜底不误判（关键回归）/ 正常新格式 / 旧 col_ 占位 / 硬切残片 / 纯文本 / 空 file_path / 摘要 chunk |

### 无新增环境变量

扫描随对账 READY 收尾自动执行（读操作）；purge 为手动入口（`purge_stale_chunks` action）。

### 重建

kb-service 镜像（`capabilities/` 平台侧改动）。回滚：无破坏性变更，扫描为只读。
升级重放：全局搜索 `stale_chunk` / `_is_stale_table_chunk` 定位全部改动点。

## §23 `2026-08-13` — 用户 review 四点修正（O4-bis / 文档回写 / 分隔符 / compose 声明）

### 1. B 型错位可观测信号（O4-bis，中等，已修）

MinerU 的 rowspan 标注不完整（只标部分列）时自有格左移——按 HTML 语义正确解析，
但业务语义错位（如合约代码落进「品种类型」列）。它比旧实现更隐蔽：列数正确、
无 col_N，`col_` 归零后完全无信号。

| 位置 | 改动 |
|------|------|
| `utils.py` `_detect_suspect_left_shift()` | 新增：指纹 = 原始 td 数 < n_cols + 左侧密集（前部连续非空 ≥2）+ 中间空洞（长度 ≥2 的连续空段且其后仍有非空）；缺尾列（空在最右端）不算 |
| `utils.py` `_HTMLTableGridExtractor` | `close()` 时计算 `suspect_left_shift_rows` |
| `utils.py` `normalize_table_grid` | meta 新增 `suspect_left_shift` 计数（list/markdown 输入同口径） |
| `pipeline/stages.py` `_emit_table_chunks` | 累进 `stats["suspect_left_shift"]`（table_stats 透出） |
| 测试 | `TestSuspectLeftShift` 7 用例（中间空洞/缺尾列不误判/满行/稀疏前缀/小空洞/meta 透出） |

已纳入 §17.2 步 13 验收观察项（抽样复核，非硬性门槛——判据为启发式）。

### 2. pack_rows meta 参数（文档回写，非缺陷）

文档改动 41 原写「`pack_rows` 新增 `caption` / `meta` 可选入参」，实施时只加了
`caption`——notes/table_sig/table_cols 由调用方直接写 file_source 旁路，不经
pack_rows。与改动 23「元数据不占 embedding」一致。已回写文档 §3 L4.1（示例代码 +
实施回写注）与改动清单 41。

### 3. table_cols 分隔符（已修）

原 `"|".join(header)` 在 `_build_file_source` 里 `.replace('|', ' ')` 后边界不可区分
（列名含空格时）。改为 join 用 **unit separator `\x1f`**（与键值分隔符 `|` 和可见
标点都无冲突）；`parse_file_source` 白名单注释同步；展示侧按需 `split("\x1f")`。

### 4. compose environment 声明（已补）

6 个新变量（`RAG_TABLE_GRID_V2` / `TABLE_HEADER_MAX_LEN` / `RAG_TABLE_CHUNK_MAX_CHARS` /
`RAG_LIGHTRAG_CHUNK_SIZE` / `XLSX_NATIVE_NORMALIZE` / `RAG_PROMPT_LANG`）补进
`docker-compose.yml` atomic-rag 的 `environment:` 段，统一 `${VAR:-default}` 形式，
与既有变量风格一致（防后续补声明时写固定值覆盖 .env）。

### 重建

需重建 `atomic-rag` 镜像（vendored + compose 双改动）。升级重放：全局搜索
`suspect_left_shift` / `\x1f` 定位全部改动点。

## §24 `2026-08-14` — 本地接口测试修复三则（L2 判据 / 接口透出 / O7 路由）

### 1. L2 重复率判据误杀分组重复表头（本地 2025年4月.xlsx 实测发现）

线上菜单表头行「星期一/总数量/剩余数量 × 5 天」被「重复率 > 50% → 数据行」
判据整体误杀 → 全 col_N、数据从「午」行开始（整个早餐段被当表头区丢掉）。
本地用真实解析产物复现并修复：

| 改动 | 说明 |
|------|------|
| 重复率判据改为「**无合并格信号**的行且 ≤2 个不同值」 | 有合并格的重复行是分组表头（「审批授权情况×2+决策会议」）；「星期一/总数量/剩余数量」set=3 不命中；全「是」/「是-否」交替数据行仍判数据 |
| 表头区深度上限 **2 层** | 多级表头现实中罕见超过 2 级；无上限时「部分空的二级表头行 + 文本数据行」级联误判（真实菜单表三级拼接回归） |

验证：真实解析产物 `星期一-2025-03-31（周一）/总数量/剩余数量/…` 表头正确，
数据从「早/中式/肉包」开始（此前 col_0..col_16）；序列号→日期转换命中 4 格。
新增回归用例 `test_grouped_repeating_header_kept`。

### 2. get_document_chunks 接口透出表格元数据（kb-service）

`enriched` 只解析了时间轴/页码，file_source 里的 `ctype/table_sig/table_cols/
row_start/row_end` 未透出（接口实测 chunk_type 全 None）。已补进
`document_service.get_document_chunks` 的 enriched 字段。

### 3. O7 的 gateway HTTP 路由缺失

`scan_stale_chunks` / `purge_stale_chunks` 只注册在 capability action 层，
gateway 无路由（改动 #29 的落点 api/documents.py 不存在）。已补：
`GET /api/v1/knowledge-base/documents/{id}/stale-chunks` +
`POST /api/v1/knowledge-base/documents/{id}/purge-stale-chunks`
（`api_gateway/routes/knowledge_base.py`）。

### 重建

`atomic-rag`（L2 判据）+ `knowledge-base-service`（接口透出）+ `gateway`（O7 路由）三个镜像。

## §25 `2026-08-14` — 表头判定分层架构（H1/H2 + 8 形态压测基线）

### 决策（增补十）

表头判定最终分层：① 免调用直采（`<th>`/单行单列表）→ ② 模型主判（第 14 步，
合并进 Row-as-Object 表级 LLM 调用，边际成本为零）→ ③ 代码硬约束校验 →
④ 规则 fallback（现有 `_infer_table_header` 保留，llm-gateway 故障降级）→
⑤ 签名哈希缓存。规则路线按已知两张表反向拟合，版式无法区分「星期一（维度名）」
与「肉包（实例值）」——模型提供语义、代码提供保障、缓存提供确定性。

### 本轮改动

| 项 | 改动 |
|------|------|
| H1 空首行 | 全空行 `continue` 跳过继续扫描（此前 `break` 使「表前空行」之后表头整区丢失） |
| H2 连续/交替重复 | 重复判据细化：交替（`A,B,A,B`，runs ≥ len-1）→ 数据行；连续聚块（`Q1,Q1,Q2,Q2`/`期货×N`，L1 forward-fill 产物）→ 放行。中间态（`是×3+否`）放行判表头，首行被吃的代价可接受（C7 取舍） |
| H3 二级表头准入 | 撤回（复杂启发式，模型化后不需要） |
| 8 形态压测基线 | `TestTableFormStress` 8 用例：C1 菜单表 **xfail**（模型化前已知失败，第 14 步移除）、C2 续块/C3 空首行/C4 单级取舍/C5 标准/C6 年份序列/C7 无表头中文数据/C8 审批表（notes+两级拼接）全部断言通过 |

`normalize_table_grid` 新增可选 `has_merge` 入参（list 输入无法推导合并信号，
测试与未来调用方显式传入）。

### 重建

`atomic-rag` 镜像。模型化（第 14 步）前规则 fallback 维持现状。

## §26 `2026-08-14` — file_source 超长致 reparse 删除失败（P0，线上实测）

### 现象

本地 reparse 实测：`reparse 旧数据删除未收敛（LightRAG 删除未完成）`，LightRAG 端：

```
ERROR: Failed to delete file kb=...|table_cols=col_0col_1...|trace=:
[Errno 36] File name too long: '/app/data/inputs/kb=...|table_cols=...'
```

### 根因

§20 引入的 table_cols/notes 旁路使 file_source 膨胀（菜单表 14 列列名清单），
而 **LightRAG 删除文档时把 file_path 当真实文件路径**去 inputs 目录删文件——
超过 Linux 255 字节文件名上限 → [Errno 36] → 删除失败 → converge delete 不收敛
→ 任务失败。旧格式 file_source 短（~150 字符）从未触线。

### 修复

| 位置 | 改动 |
|------|------|
| `pipeline/stages.py` `_build_file_source` | `table_cols` 截断 60 字符、`notes` 截断 40 字符（`_truncate_source_field`）；构建后总长防御：> `_FILE_SOURCE_MAX_CHARS`(240) 时从 table_cols 段榨空间再截。完整列名在 chunk 正文里已有（self-describing 格式），旁路截断不丢信息；`table_sig`（检索对齐键，hash 不膨胀）不受影响 |
| `tests/test_pipeline/test_push_chunks_stage.py` | `TestBuildFileSource` 新增 2 用例：超长元数据截断至 ≤240 且签名/类型保留；短元数据不截断 |

### 重建

`atomic-rag` 镜像。修复后失败的 reparse 需重试（旧 doc 已部分删除，reparse_strict
按差集继续收敛）。

## §27 `2026-08-14` — purge-stale 孤儿 chunk 清理链（O7 补全）

### 现象

本地 purge 实测：`DELETE /documents/delete_document` 对残留 chunk 返回
「Document chunk-xxx not found」——reparse 删除未收敛的残留 chunk 在 **doc_status
表无记录**，LightRAG 的 doc 级删除第一步查 doc_status 就直接 404 返回，
text_chunks/向量永不清理。扫描可见（`GET /documents/chunks?doc_id=` 按 file_path
锚点查 text_chunks）但删不掉——死锁状态。

### 修复（四层贯通）

| 层 | 改动 |
|------|------|
| `Reference/LightRAG/lightrag/api/routers/document_routes.py` | 新增 `DELETE /documents/chunks/{chunk_id}`：按 chunk_id 直删 `text_chunks` / `chunks_vdb`（向量）/ `full_docs` 三层存储，不依赖 doc_status，分层尽力而为 |
| `Reference/Rag-anything/raganything/service/http_lightrag_client.py` | 新增 `delete_orphan_chunk()`（调新端点，404 视为已清理） |
| `Reference/Rag-anything/atomic-rag-server-v2.py` | 新增 action `delete_orphan_chunk`（handler 透传 task_manager 的 http_client） |
| `jonex_core/capability/atomic/rag/lightrag_adapter_v2.py` | 新增 `delete_orphan_chunk()`（dispatch 新 action） |
| `jonex_core/capability/atomic/rag/client.py` | `RAGClient` ABC / `RemoteRAGClient` / `MockRAGClient` 各加 `delete_orphan_chunk` |
| `capabilities/knowledge_base/services/document_service.py` | `purge_stale_chunks`：doc 级 delete 返回 False（not found）→ 改走 `delete_orphan_chunk` 兜底 |

### 重建

`lightrag`（vendored 端点）+ `atomic-rag`（客户端/action）+ `knowledge-base-service`
（purge 兜底）三个镜像。

## §28 `2026-08-14` — 步 13 本地灰度 reparse 实测与修复

### 实测结论

本地 KB（013fd0d4…，5 篇）全量 force reparse + 校验全部通过：

| 指标 | 结果 |
|------|------|
| 检索端到端 | 菜单题（28 题行 2）答案与参考答案一致（T3+L2 根治实证） |
| T3 日期列名 | 47/47 含 `2025-04-14（周一）-星期一` 形态（修复前 45761） |
| L2 表头推断 | col_2+ 占位 0/47（仅行标签列 col_0/col_1 合理兜底） |
| row_start 覆盖 | 47/47（100%，T6 修复生效） |
| 中文摘要 | 10/10（主体名+列名清单+行数） |
| O2 内嵌表格 | md 系列 38 个 table_row（旧链路是裸 HTML 硬切残片） |
| O7 残留 | 5 篇 stale 全部归零（10 个真残留经孤儿清理链清除） |

### 实测暴露并修复的 4 个问题（§24~§27 之外）

1. **file_source 超长**（§26）：table_cols 膨胀使 LightRAG 按 file_path 删文件时
   [Errno 36] File name too long → reparse 删除不收敛。截断修复后重试成功。
2. **O7 指纹 2 处误报**（§24 后续）：摘要文本描述 col_N 清单、native fallback
   HTML 被误判；豁免后 xlsx 归零。
3. **doc 级删除 fire-and-forget**：`delete_document` 端点立即返回
   `deletion_started`，后台 not found 无感知——purge 不能以其返回值作判据。
4. **孤儿 chunk 死锁**（§27）：残留 chunk 在 doc_status 无记录 → doc 级删除
   404 → 新增 `DELETE /documents/chunks/{chunk_id}` 端点四层贯通，purge 主路径
   改为孤儿清理（同步、真实结果）。

### 服务器待办

通用知识库（75fd9cf8…，23 篇）全量 reparse + CP-3 重跑 28 题对比快照 B，
待服务器部署本批次镜像后执行（`scripts/reparse_kb_docs.py` /
`scripts/run_28qa_eval.py` 已就绪）。

## §29 `2026-08-14` — Row-as-Object 表格对象抽取 + 表头判定模型化 + O6（步 14）

### 背景

增补二 §10.2：表格是天然关系型数据，一行 = 一个对象、列名 = 属性名，
不需要 LLM 猜。治 P1（属性全空）/ P2（200 截断）/ P3（同名合并污染）。
增补十分层架构第 ②③⑤ 层在此落地：表头判定模型化合并进表级 LLM 调用
（每表一次，与对象类型/主键判定信息互补）。

### 改动点

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/prompts/table_object.py`（**新增**，平台侧） | `TABLE_OBJECT_TYPE_PROMPT`（输出 header_row_indices/note_row_indices/levels + object_type + pk_columns + qualifier_columns，严格 JSON）、`TABLE_COLUMN_MAPPING_PROMPT`（列名→标准属性名，可选） |
| `jonex_core/capability/atomic/rag/table_object_extractor.py`（**新增**） | `infer_pk_columns`（词表匹配按唯一率取最佳 → 高基数非度量兜底 → 复合主键追加）、`detect_group_column`（forward-fill 分组列）、`TableVerdict` + `_validate_verdict`（行号连续/从 0/≤总行数/不重叠/层级≤3）、`analyze_table`（LLM 主判 → 校验 → 规则 fallback → 缓存 `hash(表标题+列名签名)`）、`build_objects`（行→实例：canonical_name=主键（限定值）、aliases 支持两种问法、attributes 全列结构化、source_chunks 带 row 区间+table_sig）、`build_group_relations`（分组列→包含关系） |
| `raganything/service/task_manager.py` | `_merge_table_objects`：常规链路（ctx.content_list 可用）对每张表重跑网格化 → 表级判定 → 对象+分组关系并入 `ExtractionResult`（`extraction_method=row_as_object`）；O6 同名去重（表格对象优先于 LightRAG 文本实体，含别名占位）；失败仅降级日志不阻断本体 |
| `jonex_core/capability/atomic/rag/ontology_extractor.py` | O6：`_is_table_chunk_entity`（file_path ctype=table_row/table）→ LightRAG 表格实体丢弃，不占 200 截断名额、不造成同名污染（开关 `TABLE_OBJECT_FILTER_LIGHTRAG_TABLE_ENTITIES`，默认 true） |
| `tests/unit/test_table_object_extractor.py`（**新增**） | 22 用例：主键识别 5 种形态、forward-fill 例外行独立实例（行 25 修复）、别名双问法、主键空行跳过、source_chunks 行区间、分组关系、LLM 校验 7 判据、缓存确定性、O6 过滤 4 用例 |

### 新增环境变量

| 变量 | 默认 | 语义 |
|------|------|------|
| `TABLE_OBJECT_MAX_ROWS` | `2000` | 每表对象实例行数上限（超出只进向量不进图） |
| `TABLE_OBJECT_FILTER_LIGHTRAG_TABLE_ENTITIES` | `true` | O6：表格 chunk 的 LightRAG 实体丢弃 |

### 重建

`atomic-rag` 镜像（vendored task_manager + jonex_core 新模块）。表格对象随
本体抽取自动生效（无需 reparse——下次抽取/重抽即产出；存量文档走
`retry_ontology_extract` 或 reparse 回填）。

## §30 `2026-08-14` — 批次 4 本体与检索质量（步 15~18：S2/S8/L4.2 检索侧/S1+S7）

### 步 15 S2 真 stub 过滤（search_service）

- `_is_stub()`：**「且」不是「或」**——desc 空 **且** attrs 空（`{}`/None/JSON 字符串形态均视为无）才判 stub；Row-as-Object 表格对象（desc 空但 attrs 丰富）不误杀（关键回归）；
- `_filter_stub_facts()`：邻域事实/图查询模板两来源统一过滤，过滤名单进 reasoning `stub_filtered`（不静默丢弃）；全部被过滤自然走 INSUFFICIENT → 降级 RAG，不凑数。
- 测试 `test_stub_filter.py`（8 用例）。

### 步 16 S8 低质 chunk 过滤（search_service，方案 A 流程 doc 聚合之前）

`_filter_low_quality_chunks()` / `_low_quality_reason()` 四类判据（均有实测依据）：
1. 残缺 HTML 碎片（`>`/`</` 开头，或表格标签无闭合且 <200 字——C2 实测）；
2. 英文表格摘要（ctype=table_summary 且 ASCII 占比 >80%——T4 实测 104/1063）；
3. 列名全失效表格行（ctype=table_row 且 col_ 键占比 ≥50%——T2 过渡期兜底）；
4. 空壳 chunk（去 yx 命名空间标记后 <20 字）。
过滤名单进 reasoning；开关 `RAG_LOWQ_CHUNK_FILTER_ENABLED`（默认 true）。
测试 `test_low_quality_chunk_filter.py`（14 用例）。

### 步 17 L4.2 检索侧双路召回配额（search_service，S8 之后、doc 聚合之前）

`_apply_dual_path_quota()`：table_row 取 top-⌈max(8, MAX_CONTEXT/900)⌉、
table_summary 取 top-⌈row/3⌉（§16.3 影响 2 分档校准），其余 ctype 不受配额；
存量 chunk 无 ctype 归「其他」（向后兼容）；保持原相对序。
开关 `RAG_DUAL_PATH_QUOTA_ENABLED`（默认 true，依赖 ctype 存量数据，reparse 后生效）。
测试 `TestDualPathQuota`（4 用例：摘要不挤掉明细行 / text 不受限 / 无 ctype 兼容 / 保序）。

### 步 18 S1+S7 双向校验与裁决（§14.2/§15.4）

| 位置 | 改动 |
|------|------|
| `jonex_core/common/ontology_llm.py` | `arbitrate_answers()`：对称呈现两侧答案+证据（各截 3000 字），裁决偏向**证据类型**（结构化属性 > 自由文本 > 摘要；带锚点 > 无锚点）而非来源类型；严格 JSON `{verdict, confidence, reason, answer}`；temperature=0；计量场景头 `ontology_arbitration` |
| `capabilities/knowledge_base/services/search_service.py` | `_assess_ontology_confidence()`：四维判定（结构化属性/自由文本/hop/多候选冲突——冲突=同一属性键跨事实值不一致，行10 场景）→ high/mid/low；`_ontology_slot_hit()`：S7 反向触发（attributes 键或值与 query 词元重叠，治行19）；`_cross_verify()`：统一裁决入口——ontology 高置信不跑对侧（保持单路延迟）、中/低置信跑 RAG 对侧；rag 侧命中本体结构化槽位 → 反查本体事实（answer_from_facts）→ 裁决；异常/超时返回 None 回退原答案（绝不因校验故障丢答案）；分歧记 reasoning `STAGE_ARBITRATION` |
| `capabilities/knowledge_base/dtos/reasoning.py` | 新增 `STAGE_ARBITRATION = "arbitration"` |
| 开关 | `ONTOLOGY_ARBITRATION_ENABLED=true`、`ONTOLOGY_ARBITRATION_TIMEOUT=8` |

测试 `test_ontology_arbitration.py`（16 用例：置信三档、slot 命中矩阵、高置信不跑对侧、裁决异常/超时回退、S7 反向裁决采纳本体）。

### 修复的连带问题

- `search_service.py` 缺失模块级 `import json`（S2/S7 的 attributes JSON 字符串解析静默吞 NameError）——补齐。

### 重建

`atomic-rag`（本体抽取链路）+ `knowledge-base-service`（检索/裁决）+ `gateway`（如无改动可跳过）。

## §31 `2026-08-17` — review 两则：rerank 开关降级 + 裸 except 收敛

### 问题 1：RAG_PRELLM_RERANK_ENABLED=false 使方案 B 降级（已修）

容器实测该开关为 false：rerank 不调用 → reps 无 relevance 分 → 融合退化为
主体分四档、「主体全不中时 λ=0 → 全部同分 → 完全无序」，L4.2 配额内选哪
13 个按原始顺序——与 P0-2 困境同形。不是代码 bug，是开关组合降级。

| 处置 | 内容 |
|------|------|
| 开 rerank | `RAG_PRELLM_RERANK_ENABLED=true`（`/v1/rerank` 已容器内实测可用：llm-gateway 正常返回 relevance_score） |
| 相关性兜底 | LightRAG references 无自带相似度字段——分组前按**召回序位置衰减**（`_order_score = 1 - idx/n`）标注；融合时 rel 缺失：λ>0 保持旧语义（纯主体分排序，主体命中的排前）；**λ=0（主体全不中）时用位置衰减**避免完全无序；组内代表/组内排序第二键同兜底 |
| 测试 | `test_review1_no_rerank_order_score_fallback` / `test_review1_rerank_score_takes_priority` |

### 问题 2：宽泛 except 隐患（已修）

本次新增 8 个裸 `except Exception`（异常信息完全丢弃——`import json` NameError
能潜伏正是此模式）。统一改为分级模式：

```python
except (NameError, AttributeError, ImportError):
    raise                              # 代码 bug，不掩盖
except Exception as exc:
    logger.debug("…降级: %s", exc)     # 业务降级，但可见
```

涉及 7 处（ontology_extractor._is_table_chunk_entity、search_service 的
_ontology_slot_hit/_assess_ontology_confidence/_facts_evidence/_refs_evidence/
_apply_dual_path_quota/_low_quality_reason）。table_object_extractor 原本全
`as exc` 无需改。

### 重建

`knowledge-base-service`（融合兜底 + except 分级）+ 重启使 env 开关生效。

## §32 `2026-08-19` — 表格单行超预算按单元格边界切分（C1-bis / 文档增补十一 §19）

行内切分替代 `_split_long_text` 盲切：兜底阈值此前误用文本链路的 12000
（应 900），且 `_split_long_text` 对无换行的表格行退化为精确字符位截断，
把单元格切碎（语义破坏与 lightrag-server 按 token 硬切同级）。设计规格见
`docs/table-parsing-retrieval-governance-plan.md` §19.1-19.9。

| 位置 | 改动 |
|------|------|
| `raganything/utils.py` | 新增 `split_row_by_cells(row_text, budget, *, header_block, caption_line) -> [(seg_text, cell_start, cell_end)]`：切点取「不超过 budget 的最后一个 `` | ``」（fmt_row 两种格式共用的单元格分隔符），cell 序号 0-based 右开；header_block/caption_line 计入每段预算并统一作前缀；单格自身超预算才退化字符硬切（WARNING，调用方以「非尾段段末不含 `` | ``」特征计数 cells_hard_cut）；`fmt_row`/`make_header_block` 闭包提为模块级函数（pack_rows 行为等价）；新增 `DEFAULT_COLUMN_THRESHOLD=6`；`_render_caption_line` docstring 补 continuation 两类语义（O3 分组值 / 行内切分格式化行号） |
| `raganything/pipeline/stages.py` | 表格分支兜底判断改 `self._table_chunk_max_chars`（900）+ `row_end - row_start == 1`（单行段）；切分改调 `split_row_by_cells`，caption 无条件补（`continuation=f"第 {row_start + 1} 行"`，1-based）、markdown 格式（≤6 列）补 header_block；多行段超文本预算仍走 `_split_long_text`（O1 断言观测形态）；`table_stats.oversize_rows` 计数 + 前 5 条采样日志（§19.6 数据形态 vs T1 归一化缺陷判别）、`cells_hard_cut` 计数；`_build_file_source` 新增 `cell_start`/`cell_end` kwarg（写入条件跟随 row 区间），总长防御新增「仍超限则丢弃 cell_* 段」兜底 |
| `jonex_core/common/file_source_util.py` | `parse_file_source` 显式字段表新增 `cell_start`/`cell_end`（旧 chunk 无键 → None）；`to_location` 的 `table_row` 分支透出格区间（优先级不变，仍在 page 之前） |
| `capabilities/knowledge_base/dtos/reference.py` | `SourceLocation` / `ParsedRef` 新增 `cell_start`/`cell_end` |
| `capabilities/knowledge_base/services/search_service.py` | 新增 `_merge_same_row_locations()`：`_build_references` 聚合时按 `(table_idx, row_start, row_end)` 合并同行子段为一个 location（cell 区间取 min/max 并集，text 取第一个非空；部分子段丢锚点时退回纯行级）——同一行不再出现多条重复引用 |

测试：

- `Reference/Rag-anything/tests/test_table_row_chunking.py` `TestSplitRowByCells`（8 用例：按格切 / self-describing 不补列名 / markdown 补列名 / caption 无条件补 / 单格超预算硬切 / cell 区间连续 / 前缀计入预算 / 空行短行）
- `tests/unit/test_file_source_ctype.py`（3 用例：cell roundtrip / 旧 chunk 兼容 / to_location 透出）
- `tests/unit/test_same_row_reference_merge.py`（5 用例：同行合并 / 未切分透传 / 非表格不动 / 锚点部分丢失回退 / 不同行不合并）

token 比率前置项已实测（§19.1.1/§19.8）：836 字全中文正文 + caption + header_block + ns_token 合计 645 < 1200 token，无需下调 `RAG_TABLE_CHUNK_MAX_CHARS`。

### 重建

`atomic-rag`（stages/utils 改动必须重建镜像）+ `knowledge-base-service`（引用合并/DTO）。reparse 后验证 `oversize_table_chunks == 0`。

## §33 `2026-08-19` — Row-as-Object 三子项：列名 TBox 映射 / 层级+引用关系 / 改动 18 upsert（文档 §10.2）

阶段 3 补齐增补二 §10.2 的剩余规格：LLM 职责「每表两次」的第二处调用、
三类关系中的层级/引用两类、以及表格对象的增量 upsert 键。

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/table_object_extractor.py` | ① `TableVerdict` 新增 `column_mapping`（原列名→标准属性名），随表级判定一起缓存；`analyze_table` 判定成功后追加 `_llm_column_mapping`（`TABLE_COLUMN_MAPPING_PROMPT` + compiled schema 属性词表，失败只丢映射不阻塞）；新增 `std_attribute_name()` 消费入口；新增 `_compose_header_parts()`（多级表头父级链拆解，`_compose_header_columns` 改为其派生）；② `build_objects` 消费映射（attributes 同时写原列名键与标准名键，S7 槽位两种问法都可达）与新参数 `header_parts`（父级链长度>1 的列记入嵌套属性组：`审批授权情况-部门负责人` → `{审批授权情况: {部门负责人: …}}`，叶子名过映射，单级表头列保持扁平） |
| `jonex_core/capability/atomic/rag/ontology_extractor.py` | `ExtractedEntity` 新增 `table_sig`/`pk_value`（改动 18 upsert 键随 ont_data 透传） |
| `Reference/Rag-anything/raganything/service/task_manager.py` | `_merge_table_objects` 接收 `compiled_schema` 提取 tbox 属性词表传入 `analyze_table`；表头判定应用后计算 `header_parts` 传入 `build_objects`；`ExtractedEntity` 补 `table_sig`/`pk_value`；`task.ontology_data` 序列化透传两键 |
| `capabilities/knowledge_base/repository/ontology_graph_repository.py` | ③ 新增 `_row_object_merge_key()`：`extraction_method=row_as_object` 且带 table_sig+pk_value 时 `merge_entity` 改按 `(tenant_id, kb_id, table_sig, pk_value)` MERGE（键不含 entity_type/canonical_name，类型判定或限定值变化时最新一版覆盖旧版；跨文档同模板自然合并，doc_ids 累积）；新增 `find_reference_hits()`：候选值批量精确命中（canonical_name/aliases，非 stub，多命中时 canonical 优先+confidence 降序） |
| `capabilities/knowledge_base/services/reconciliation_service.py` | ② 新增 `_table_reference_candidates()`/`_build_table_reference_relations()` 纯函数 + `_handle_completed` 接线：实体写图**前**（`delete_by_document` 之前）批量查引用命中 → 建边（源=行实体、目标=命中正式实体、关系类型=列标准属性名、source_chunks 沿用行锚点、自引用/超长值跳过、候选值上限 3000），并入关系写入循环复用 merge_relation 端点解析（两端正式实体不捏造 stub）；失败降级只丢关系。⚠️ 本条曾误记为「实体全部写图后批量查引用命中」，2026-09-12 订正：查命中在 delete **之前**是有意设计（命中口径 = 本文档写入前的 KB 图），以代码为准，见 §52 |
| `capabilities/knowledge_base/services/search_service.py` | 属性组嵌套兼容：`_ontology_slot_hit` 内层键/值参与槽位命中；S1 confidence 冲突检测叶子键/值参与（避免 dict repr 误判冲突） |

测试：

- `tests/unit/test_table_object_extractor.py`（+13 用例：`_compose_header_parts` 拆解/占位符跳过、`std_attribute_name`、映射消费双键、属性组嵌套/叶子映射/单级扁平）
- `tests/unit/test_table_reference_relations.py`（新建 14 用例：upsert 键判定、候选值收集/去重/超长排除、引用建边关系类型、属性组叶子关系类型、自引用/别名自引用跳过、miss 无边、非表格实体不动、去重）

### 重建

`atomic-rag`（task_manager 改动必须重建镜像）+ `knowledge-base-service`（仓储/对账/检索）。reparse 后验证：表格实体跨文档重跑不翻倍（upsert 生效）、引用关系边数 > 0（KB 内有可命中实体时）。

## §34 `2026-08-19` — 阶段 4：S1/S7 裁决三缺口 + S2 stub 占比告警 + O4 header_levels（文档 §14.2/§14.3/§15.4/§18.3）

阶段 4 补齐双向校验裁决的统一入口缺口、stub 抽取管线异常的可观测信号，
以及表头层级分布观测（改动 52、改动 15 残余项）。

| 位置 | 改动 |
|------|------|
| `capabilities/knowledge_base/services/search_service.py` | **§15.4 裁决三缺口**：① 新增 `_norm_answer_text()`（全角数字/点/斜杠转半角、去空白、小写）+ `_answers_agree()`（归一化相等 → 一致；数值尾零规范化后集合一致 + 非数值结构一致 → 仅数字格式差异判一致；同数值不同主体判不一致，宁裁决不误放）——两路都有答案且一致 → 免裁决（collector 记 `cross_verified=true` skipped 步骤），省一次 LLM；② 一方无结果 → 保留现有答案（collector 记 unverified + 两侧有无），不裁决；③ S7 分支接入 S8 低质判据：RAG 作答且命中 chunk 存在低质信号（html_fragment/empty_shell/english_table_summary/all_col_placeholder，经 `_low_quality_reason`）→ 即使无槽位命中也触发裁决（detail 记 `slot_hit`/`lowq_signals`） |
| `capabilities/knowledge_base/repository/ontology_graph_repository.py` | **改动 52（S2）**：新增 `stub_entity_stats(tenant_id, kb_id)` → `{total, stub, ratio}`（单条 Cypher 聚合 `coalesce(e.stub,false)`，total=0 时 ratio=0） |
| `capabilities/knowledge_base/services/reconciliation_service.py` | 新增常量 `_STUB_ENTITY_RATIO_WARN_THRESHOLD=0.30`、`_STUB_ENTITY_RATIO_MIN_TOTAL=10` 与纯函数 `_stub_ratio_alert(total, stub)`；`_handle_completed` 写图成功后做 KB 级聚合，超阈值打 WARNING（抽取管线异常信号）；聚合失败只降级记日志，不影响文档 READY 流转 |
| `capabilities/knowledge_base/services/ontology_query_service.py` | `get_kb_statistics` 响应新增 `stub_entity_count` / `stub_entity_ratio` / `stub_ratio_alert`（KB 详情可见），超阈值同步 WARNING；Neo4j 降级时三值置 0 |
| `Reference/Rag-anything/raganything/pipeline/stages.py` | **改动 15（O4）残余**：`table_stats.header_levels` 分布计数（`{"0": n, "1": n, …}`，仅 grid-v2 路径——旧 normalize_table_rows 无 meta，混入会污染 0 档语义）；docstring 同步。fallback 告警与 `tables_fallback` 计数此前已存在 |

测试：

- `tests/unit/test_ontology_arbitration.py`（+10 用例：`TestAnswersAgree` 6 条——归一化相等/仅数字格式差异一致/同数值不同主体不一致/不同数值不一致/空侧不一致/全角数字归一；`TestCrossVerify` 4 条——两路一致免裁决且 arbitrate 不被调用/对侧空答案免裁决/低质信号触发裁决（无槽位命中）/低质信号但无 facts 跳过）
- `tests/unit/test_stub_entity_ratio.py`（新建 9 用例：超阈值/恰阈值不告警/低于阈值/零 stub 健康/小 KB 防噪声/最小样本边界/零总数/自定义阈值）
- `Reference/Rag-anything/tests/test_pipeline/test_push_chunks_stage.py`（+2 用例：`header_levels` 单级表头计 1 档、无表头计 0 档；容器内跑，依赖 LightRAG——本地已用 importlib bypass 验证 `normalize_table_grid` 对两种形态输出 1/0）

### 重建

`knowledge-base-service`（search/reconciliation/ontology_query/仓储）。Reference 侧 stages.py 随 `atomic-rag` 镜像重建生效；reparse 后查 `table_stats.header_levels` 分布与 KB 统计接口的 `stub_entity_ratio`。

## §35 `2026-08-19` — 阶段 5：S3 references 排序 / S4 html 白名单 / S5 枚举意图 + ehint 入库通道（文档 §17.2 第 19/20 步）

第五批四项落地；第 21 步（删除 fuse_rag_answers）按计划自身门槛跳过（见下）。

| 位置 | 改动 |
|------|------|
| `capabilities/knowledge_base/services/search_service.py` | **S3**：`_ontology_refs` 证据加权收集——实体 weight=`1.0+min(confidence,1.0)`、事实 `2.0/hop`、关系边 chunk `1.0/hop`，按 `(score, contrib)` 降序稳定排序（references 不再按 Neo4j 返回顺序随机漂移）；collector detail 新增 `top_ref_scores`（前 5 权重）。**S5**：新增模块常量 `_ENUM_PATTERNS`（列出/列举/罗列/枚举、有哪些/哪几个/哪几家/哪些、分别/各自、所有/全部；**不含「一共」**——计数意图归 P1-6 count_versions 图模板）+ 静态方法 `_detect_enumeration_intent(query)`；本体路径 hook：枚举意图 → 跳过本体取证/作答（collector 记 skipped）→ 自然降级 RAG 取原始表格行；`_cross_verify` 增加 `if not facts: return None` 安全跳过（低质信号但无事实时的裁决调用容错） |
| `Reference/Rag-anything/raganything/config.py` | **S4**：`SUPPORTED_FILE_EXTENSIONS` 默认值追加 `.html,.htm,.xhtml` |
| `capabilities/knowledge_base/services/data_source_service.py` | **S4**：`allowed_ext` setdefault 追加 `html/htm/xhtml`（txt/md 之后） |
| `Reference/Rag-anything/raganything/pipeline/stages.py` | **改动 20 入库通道（方案 C）**：`_build_file_source` 新增 `entity_hint` 参数 → `ehint=` 键（截断 40 字、`|`→空格，位于 notes 后 trace 前，缺省不写键）；`_emit_table_chunks` 调用点传 `entity_hint=caption`（L4.1 表标题 heading） |
| `jonex_core/common/file_source_util.py` | `parse_file_source` 返回 dict 新增 `entity_hint`（`kv.get("ehint") or None`；旧 chunk 无键 → None） |

测试：

- `tests/unit/test_ontology_refs_order.py`（新建 4 用例：hop1 事实 2.0 > 实体 1.1 > hop3 0.67、多事实共证累计 hop3×2 > hop2、关系 chunk 0.5 < 事实 chunk 1.0、同权重稳定序）
- `tests/unit/test_enumeration_intent.py`（新建 7 用例：列出/哪些/分别/所有、单事实问答不误伤、空 query、计数意图「一共几个版本」不判枚举）
- `tests/unit/test_file_source_ctype.py`（+3 用例：ehint 往返、`|` 转义、存量无 ehint → None）
- `Reference/Rag-anything/tests/test_pipeline/test_push_chunks_stage.py`（+3 用例：ehint 写入/管道转义+截断/缺省不写键；容器内跑，依赖 LightRAG）

### 重建

`atomic-rag`（stages/config 改动必须重建镜像）+ `knowledge-base-service`。新入库表格 chunk 才带 `ehint=`（存量 chunk 无此键），检索期主体一致性过滤消费该信号需 reparse 后才有数据（检索期消费属另一计划，本批只打通入库通道）。

### 第 21 步「删除 fuse_rag_answers 旧路径」——本轮跳过

计划备注本身即定：「方案 A 稳定运行一段时间后单独提交」。当前证据链不满足删除条件：

1. **方案 A 未默认开启**：代码默认 `false`（search_service.py:190）、docker-compose 默认 `${RAG_PLATFORM_ANSWER_ENABLED:-false}`、`.env.local.example=false`、deploy/README.md 标注「灰度」；仅 gitignored 的 `deploy/.env` 单机为 `true`，不具备「稳定运行一段时间」的证据。
2. **三处调用中两处在方案 A 替代范围之外**：2007（`_rag_fallback_multi` OntoRAG 多库融合）是开关关闭时的默认路径；3759（llm-wiki 多库融合）、3967（OntoRAG+llm-wiki 混合融合）本就不走 `_rag_platform_answer`，**无替换实现**——现在删除会直接打断 Wiki 检索与混合检索的融合链路。
3. **同源方案 §6.3/§5.3 双重约束**：「保留函数供回滚，不删」「切换与删除分开提交」。

结论：函数与三处调用保持原样，待方案 A 默认开启并稳定后按 playbook §5.3 单独提交删除。

## §36 `2026-08-20` — 文本块打包（§block-packing，docs/text-block-packing-chunk-governance-plan.md 改动 1/2）

入库侧相邻文本块按预算打包成 chunk（治「12000 字符大块被 LightRAG 按 1200 token
二次切分、页码噪声块抢占 top-k」）。纯函数在 utils.py，管线接线在 stages.py。

| 位置 | 改动 |
|------|------|
| `raganything/utils.py` | 新增 `is_noise_block`（C2 纯数字/符号/罗马 ≤4 字、C3 短块全文 ≥3 次重复页眉页脚）/ `is_heading_like`（C4 编号模式受 `max(max_len×2, 80)` 长度闸门；C5 短+无句末符+无换行）/ `pack_text_blocks`（单遍顺序扫描+累积缓冲；`budget`/`heading_max_len`/`heading_levels`/`drop_noise`/`repeat_index`/`carry` 入参；返回 `(packs, dropped)`，dropped 为 `(text, reason)` 供采样日志）。**review P0-1**：C4 初版不受长度限制致超长编号正文被误判标题、包头截断真丢内容——修正为长度闸门 + `_render_pack_head` 返回 `(head, overflow)`、截断溢出转正文首行。**review P0-2**：`heads_fresh` 标志区分新收/继承标题——新收标题包 `page_start`=标题页，续包=首个正文块页（修复续包页码被拉回标题页）。**review P1**：`carry`（`heads`/`head_pages`/`heads_fresh`）原地回传，标题跨硬边界（table/image/equation 冲刷）继承 |
| `raganything/pipeline/stages.py` | `__init__` 读 `RAG_TEXT_BLOCK_PACKING`/`RAG_TEXT_PACK_CHARS`/`RAG_TEXT_PACK_MAX_TOKENS`/`RAG_TEXT_PACK_HEADING_MAX_LEN`/`RAG_TEXT_PACK_DROP_NOISE` 并打印生效值；`_collect_text_chunks` 阶段 A `_prefill_table_captions`（仅打包开启时执行，避免污染 content_list）+ 打包路径（repeat_index 预扫、pending 缓冲、硬边界 `_flush_text_pack(carry)`）；`_flush_text_pack`（pack_text_blocks → 观测计数器 blocks_total/blocks_dropped_noise/blocks_packed/packs_total/packs_cross_page/avg_pack_chars → 每文档前 20 个被丢弃块采样 WARNING → 逐包 `_emit_text_chunks(pack=...)`）；`_emit_text_chunks` pack 路径（tokenizer 实测断言 `RAG_TEXT_PACK_MAX_TOKENS`，超限 WARNING+`oversize_text_chunks`+按换算阈值 `_split_long_text`）；`_build_file_source` 新增 `page_end`/`pspans` kwarg（pspans 60 字符截断与 240 防御丢段共用 `pspans_truncated` 口径） |

### 重建

`atomic-rag`（utils/stages 改动必须重建镜像）。灰度与回滚变量见 `deploy/README.md`「文本块打包变量」；开关 `false` 时逐块 1:1 行为与改造前完全一致。

### 第二份 review 修正（2026-08-20，仅文档/注释，无代码行为变更）

用户对执行计划 §10 片段级页码精算的 review 指出两处根因性错误（均已核实坐实），修正落点：

- `docs/text-block-packing-chunk-governance-plan.md` §10 整节重写：初版根因「查询侧 `QueryParam.chunk_token_size` 二次切分」在代码里不存在（`chunk_token_size` 仅入库侧三处：lightrag.py:310 类字段、operate.py:107 入库切分、lightrag_server.py:1170；lightrag.py:1215/1251 是 insert/ainsert docstring）。真实链路：查询期不切 chunk（utils.py:2800-2875 只做去重/rerank/chunk_top_k 截断/max_total_tokens 裁剪），references 的 content 天然即 chunk 全文 → find offset 恒 0，与打包上限持平无关。§10.4 重构为纯平台侧三步（L1 双路归一化保留逐条 content+chunk_ids 对齐+REMOTE 补 ns token 清理；L2 `_build_references` 内 pspans 页段打分取最高分页；方案 B 前端展示 page–page_end 范围并列二选一），删 vendored LightRAG 改动与归档需求。
- 事实勘误：query_routes.py:436-461 已把 data.chunks 聚合透出为 references 的 content/chunk_ids（初版说反）；唯一真实丢失是两侧归一化 `"\n\n".join` 抹平子 chunk 边界；REMOTE（task_manager.py:852-866）缺 ns token 清理是真缺陷。file_path 继承行号修正 lightrag.py:2033-2041（原 2037-2043 偏 4 行）。
- `docs/text-block-packing-chunk-governance-plan.md` 改动 4 bullet、`capabilities/knowledge_base/services/search_service.py` 精算注释：同步删除「chunk_token_size 持平」错误表述，改为「查询期不切 chunk，content 即全文，offset 恒 0」。
- 语义统一：全文弃用「首个命中片段」，统一为「与 query 最相关」（页段打分取最高分）。

无代码行为变更，`atomic-rag` 无需重建。

## §37 `2026-08-20` — 片段级页码精算 L1/L2（§block-packing §10）

跨页打包 chunk 被召回时，引用 `page_no` 由「恒为 chunk 起点页」升级为「与 query
最相关的页段所在页」（纯平台侧方案，不动 vendored LightRAG）。

| 位置 | 改动 |
|------|------|
| `jonex_core/common/file_source_util.py` | **L1 共享归一化**：新增纯函数 `normalize_chunk_content(content) -> (text, chunk_texts)`——全文 join 后统一清理 ns token（`<!--yx:[0-9a-f]+-->`）；chunk_texts 逐条清理、长度恒等于入参 list（不过滤，空条保留位置）与 chunk_ids 下标对齐；str/空/None 全形态覆盖。进 `__all__` |
| `jonex_core/capability/atomic/rag/lightrag_adapter.py` | **L1 LOCAL**：query() 内联归一化（`"\n\n".join` + 一次性 re.sub）替换为共享函数调用；`parsed` 新增 `chunk_texts`（逐条，与 chunk_ids 对齐），`text` 行为不变 |
| `Reference/Rag-anything/raganything/service/task_manager.py` | **L1 REMOTE**：同构接入共享函数；**修复 ns token 泄漏缺陷**——REMOTE 此前只 `.strip()` 未清 `<!--yx:…-->`（LOCAL 早已清理），现两侧统一。`parsed` 新增 `chunk_texts` |
| `capabilities/knowledge_base/services/search_service.py` | **L2 页段打分**：模块常量 `RAG_REF_PAGE_SCORING_ENABLED`（默认 true，灰度开关）+ 三个纯函数 `_query_terms`（CJK 二元组 + ≥2 字 alnum 词；CJK 不足 2 字退回单字）/ `_split_text_by_pspans`（pspans 切页段；完整性防御：坐标越界/乱序/首 entry 非 0 → None）/ `_best_page_by_query`（关键词命中打分取最高分页，同分取包序靠前页，零命中 → None）。`_build_references` 新增 `query` 参数：pspans 分支先页段打分（开关开 + 有 text），失败回落 `resolve_page_by_offset(pspans, 0)`（chunk 起点页，与改造前一致）。3 个有 query 的调用点穿透（search / 方案 A / fallback 多库融合）；本体路径（refs 无 text）与流式 resolve（无 query）自然走回落 |
| `deploy/docker-compose.yml` / `deploy/.env.example` / `deploy/README.md` | `RAG_REF_PAGE_SCORING_ENABLED`（由 **knowledge-base-service** 读取，非 atomic-rag）；README「文本块打包变量」节补配套变量表 |

打分选型（§10.5③）：**关键词命中打分**——零外部依赖/延迟；embedding 打分（经
llm-gateway，有计量成本）留待后续迭代。

测试（tests/unit 本地套件，不入库）：

- `tests/unit/test_chunk_content_normalize.py`（新 10 例：单/多 chunk join 与对齐、ns token 双侧清理、纯 token/空白条对齐保留、str/空/None/清洗后全空）
- `tests/unit/test_ref_page_scoring.py`（新 23 例：切段 9 例含完整性防御、词项 5 例、打分 5 例含同分取先、验收组合 4 例——§10.6 验收 1 跨页包命中第 2 页 → page_no=2 与验收 2 三类回落）
- `tests/unit/test_rag_platform_answer.py`：fake_build_references 签名补 `query` 并断言穿透值（`_build_references` 签名变更的同步修正）

验证：tests/unit 219 绿；Reference 侧 `test_text_block_packing.py` 66 绿；
jonex_core/capabilities/raganything compileall 通过；task_manager 冒烟导入 +
ns token 清理断言通过。

### 重建

- `knowledge-base-service`（search_service 改动）
- `atomic-rag`（REMOTE task_manager 改动，需重建镜像生效）
- `jonex_core` 走 capability 服务镜像共享层（local 调试直接生效）

灰度：默认开启；`RAG_REF_PAGE_SCORING_ENABLED=false` 完全回退到改造前行为
（offset=0 起点页）。存量 chunk（无 pspans）与同页包不触发精算，零开销。

### 落地 review 修正（2026-08-20，P1/P2 + 低优先文档项）

- **P1 完整性判据（§10.5①/② 处置落地）**：`_build_references` 打分前新增
  `len(r.get("chunk_texts") or []) <= 1` 判据——chunk_texts 多于 1 条 = 包被
  LightRAG 入库二次切分成子 chunk（子 chunk 共享同一 file_source/pspans），
  text 是子 chunk 的 join（含 "\n\n" 分隔、可能部分召回/带 overlap），与包
  原文逐字符错位、pspans 坐标失准 → 不精算回落起点页。同时给 chunk_texts
  兑现了首个消费者（此前零消费者）。实测复现：部分召回时 88% 的末页段文本
  会被错误归给前一页。
- **P2 text 过滤空条**：`normalize_chunk_content` 的 text 改为
  `"\n\n".join(c for c in cleaned if c)`——空条不再贡献 "\n\n" 对（恢复
  LOCAL 旧行为），text 与包原文逐字符对齐（pspans 坐标基准）；chunk_texts
  仍返回不过滤的原长列表（与 chunk_ids 下标对齐）。两目标不冲突。
- 文档：顶部状态改「改动 1~6 + §10 L1/L2 已实施」；§10.5 ①/② 处置标注已
  落地、新增 ⑦ 页段长度偏差已知边界（归一化留待 embedding 打分迭代）；
  §10.6 标注验收测试为 tests/unit 本地验证、不入库。
- 测试：test_chunk_content_normalize 补 text 对齐断言（空条不过滤进 text）；
  test_ref_page_scoring 新增 P1 回归（多子 chunk 跳过打分回落起点页 / 单
  chunk 正常打分的反向用例）。

## §38 `2026-08-20` — 图片引用链路（§image-refs，docs/image-reference-chain-execution-plan.md P0~P3）

图片描述 chunk 携带 `image_idx`/`aext` 位置锚点，入库侧把图片资产上传平台对象存储，
检索侧 references 富化出 `asset_url`，前端在 answer 旁渲染图片条与卡片缩略图。

| 位置 | 改动 |
|------|------|
| `jonex_core/common/file_source_util.py` | **P0-1/P0-2**：`parse_file_source` 新增 `image_idx`（`image_idx=` 键）与 `asset_ext`（`aext=` 键，旧 chunk 无此键 → None）；`to_location` 新增 `image` 分支（优先级 timestamp > table_row > **image** > page > char > chunk，置于 page 之前——图片 chunk 带 page_no 会遮蔽 image_idx，与 table_row 同型教训）。asset_ext 不透出 location（检索侧推导对象键的中间量） |
| `capabilities/knowledge_base/dtos/reference.py` | **P0-3**：`SourceLocation` 新增 `image_idx`/`asset_url`；`ParsedRef` 新增 `image_idx`/`asset_ext`（前端回传 refs 不经过 parse_file_source，缺字段会被 DTO 校验丢弃——resolve 路径 asset_url 生效的前提） |
| `capabilities/knowledge_base/services/search_service.py` | **P0-4**：`_low_quality_reason` 判据 2 覆盖 `ctype=="image"`（英文图片描述 reason=`english_image_description`，与表格摘要 `english_table_summary` 区分）。**P2-1**：`_build_references` 聚合循环收集 `type=="image"` 的 loc + 同条 `r` 的 `asset_ext`，循环后 `asyncio.gather` 批量 `build_asset_key → presigned_url` 填 `loc["asset_url"]`；ext 缺失跳过、异常留 None、整段 best-effort 不阻断引用产出 |
| `capabilities/knowledge_base/repository/search_history_repository.py` | **P0-6**：`_strip_raw_urls` 同步剥离 `locations[].asset_url`（预签名 URL 不进历史快照） |
| `jonex_core/common/object_storage/__init__.py` | **P1-1**：新增 `build_asset_key(tenant, kb, doc, image_idx, ext)`（键 `{prefix}/kb/{t}/{kb}/{doc}/assets/img_{idx}.{ext}`，与文档键同前缀便于按文档批量清理）+ `normalize_asset_ext`（白名单 `{jpg,jpeg,png,gif,webp,bmp}`，非白名单按 png 打 WARNING——aext 经 file_source 旁路理论上可被构造值注入）。**review 安全补强**：image_idx 强制整数校验（raw 端点入参来自请求 payload，非整数抛 ValueError，防 `img_../../evil` 路径穿越键） |
| `raganything/pipeline/stages.py` | **P0-5**：`_build_file_source` 新增 `asset_ext` kwarg（`aext=` 位于 trace 之前）；`_collect_multimodal_chunks` 图片正文前缀「图片描述：」（与「表格概览：」同构，嵌入空间与文本 chunk 区分）。**P1-2**：新增 `AssetUploadStage`（gate `RAG_ASSET_UPLOAD_ENABLED` 默认 on；延迟导入 jonex_core 对象存储，不可用整体跳过；遍历 multimodal_results 中 `content_type=="image"` 项，读 `original["img_path"]` → `put_bytes(build_asset_key(..., image_idx=item["index"], ext), …)`；**复用 item["index"] 全局枚举不另起计数**（[table, image, equation, image] 的两图键为 img_1/img_3 而非 img_0/img_1）；单张失败只 WARNING）。**P1-3**：上传结果 `{image_idx: ext}` 经 `StageResult.asset_exts` 回传，`PushChunksStage.execute` 传给 `_collect_multimodal_chunks` → 上传成功的图片 chunk 才写 `aext=`（检索侧据此判定无 URL 可取） |
| `raganything/pipeline/base.py` | **P1-3**：`PipelineContext` 新增 `asset_exts: dict`；`StageResult` 新增 `asset_exts` 字段 |
| `raganything/pipeline/builder.py` | **P1-2**：http pipeline 注册 `asset_upload` 于 `multimodal` 与 `push_chunks` 之间（aext 必须赶在 chunk 写入 file_source 前就位） |
| `capabilities/knowledge_base/services/document_service.py` | **P2-2**：新增 `get_asset_raw_location(tenant, document_id, image_idx, ext="", knowledge_base_id="")`——与 `get_raw_location` 同构（get_required 租户校验 + 可选 KB 校验）；`build_asset_key` 派生键，cos 预签名 / local 返回 storage_key；ext 空抛 InvalidParameterError |
| `capabilities/knowledge_base/capability.py` | **P2-3**：注册 `get_asset_raw_location`（必填 `document_id`+`image_idx`；进 `_SPACE_READ_ACTIONS` 与 `_DOC_ID_ACTIONS`） |
| `api_gateway/routes/knowledge_base.py` | **P2-4**：新增 `GET /documents/{document_id}/assets/{image_idx}`（token 或 JWT 鉴权与 `/raw` 同口径；cos 302 预签名——`<img>` 跨域加载不受 CORS 限制无需同源代理态；local 走共享卷 FileResponse） |
| `deploy/.env` / `.env.example` / `docker-compose.yml` | **P1-4**：`RAG_ASSET_UPLOAD_ENABLED=true`（**atomic-rag 进程读取，落点 .env 而非 .env.rag**——后者仅 lightrag 容器加载，同坑见 RAG_STRUCTURE_AWARE_CHUNK 从未生效）；compose atomic-rag 段顺手补 `KB_INPUT_DIR: /app/inputs`（显式化，防默认值变更静默写错目录） |
| `frontends/core-business`（types/knowledgeSearch.ts、pages/KnowledgeSearch/SessionCard.tsx、pages/KnowledgeSearch/index.tsx、locales/*.json） | **P3**：`KnowledgeReferenceLocation` 新增 `image_idx`/`asset_url` + type 枚举补 `'image'`（枚举缺 `table_row` 属既有偏差，不顺手修）；SessionCard 新增 `collectImageAssets`（筛 `locations[].type==="image" && asset_url`，按 (doc_id, image_idx) 去重，剥「图片描述：」前缀）+ `ImageAssetThumb`（loading 骨架 / 404 降级 / 页码角标）；answer 图片条插在 Answer 与 References 之间；引用卡片内含 image location 渲染缩略图；`openReference` 扩展 location 级打开（image + asset_url → 新窗口直开原图，无则回落文档级） |

测试：

- `tests/unit/test_file_source_ctype.py`（+7：image_idx/aext 解析往返、to_location image 优先级与 table_row 共存、旧格式无 image 键回落）
- `tests/unit/test_low_quality_chunk_filter.py`（+2：英文图片描述 reason、中文保留）
- `tests/unit/test_asset_key.py`（新 8：键格式、白名单/大小写归一、空 ext ValueError、ext 路径穿越 → png 安全键、image_idx 路径穿越 ValueError）
- `tests/test_pipeline/test_asset_upload_stage.py`（新：全局枚举键对齐 img_1/img_3、键与 chunk file_source 逐项相等端到端、gate 关闭/空结果/缺路径跳过、单张失败 best-effort、jonex_core 缺失降级；RAG venv 跑）
- `tests/test_pipeline/test_push_chunks_stage.py`（+6：aext 写入/缺失/部分上传、正文「图片描述：」前缀、_build_file_source aext 键位、非 image 模态不受影响）

验证：tests/unit 238 绿；compileall 通过；core-business typecheck 绿
（node_modules 缺依赖是 install 陈旧所致，`pnpm install` 后通过）。

### 重建

- `atomic-rag`（stages/base/builder 改动必须重建镜像）
- `knowledge-base-service`（search_service / document_service / capability）
- `api-gateway`（raw 路由）
- `core-business-frontend`（P3）
- `jonex_core` 走 capability 服务镜像共享层

灰度：`RAG_ASSET_UPLOAD_ENABLED=false` 只停资产上传与 aext 旁路（检索侧图片引用无
asset_url、前端自然降级），主链路不受影响。已知债：图片对象无生命周期管理（文档
软删后残留，键设计已对齐 `…/{doc}/assets/` 前缀，供后续硬删脚本批量清理）。

### review 修正（第二轮，2026-08-20）

| 位置 | 修正 |
|------|------|
| `raganything/pipeline/stages.py` | **doc 锚点同源**：`AssetUploadStage` 原来直接用 `ctx.document_id`（无兜底），与 `PushChunksStage` 的 `ctx.document_id or ctx.doc_id or ""` 不同源——document_id 为空时上传 key 的 doc 段是空串、file_source 的 `doc=` 落到 `ctx.doc_id`（内容哈希），检索侧推导的 key 与实际 key 分叉。改为同一表达式兜底 `ctx.doc_id`；两侧兜底后仍为空则整体跳过上传（空 doc 段的 key 无法回链检索侧，上传无意义）并打 WARNING |
| `capabilities/knowledge_base/services/document_service.py` | **资产后端与文档后端解耦**：`get_asset_raw_location` 原来按 `doc.storage_backend` 选后端（注释「资产与文档同一后端」不成立——该假设只在后端配置从未变过时成立）。上传（AssetUploadStage）与检索富化（`_build_references`）都走全局 `get_object_storage()`，raw 端点改为同源全局后端；否则 local→cos 切换后 reparse 历史文档（storage_backend 仍 local）会「资产实际在 cos、端点走 local」错配 404。全局后端为 local 时 `presigned_url` 恒空串，gateway 自动落 FileResponse 分支，gateway 侧无需改（其 `fs_path` 按响应里的 `storage_backend` 字段解析，随本修正自动对齐） |
| `raganything/pipeline/base.py` | 注解修正：`StageResult.asset_exts: Optional[Dict[int, str]]`（实际存 `dict[int, str]`，key 是 int 枚举序号） |
| `tests/test_pipeline/test_asset_upload_stage.py` | +2 用例：`document_id` 为空时 key 的 doc 段取 `ctx.doc_id`（与 chunk 侧同源）；两侧锚点均空时跳过上传、不调 `put_bytes` 且打 WARNING |

## §39 `2026-08-21` — VLM 独立限流 + 调用超时修复（§image-refs 排查衍生）

背景：本地 jonex-atomic-rag 容器大量 ReadTimeout。实测链路与根因：

- 图片描述走 `base64_caption_adapter → BoundModel.complete → OpenAIDriver`（httpx 直连
  `13.230.160.245:8009`，不经 llm-gateway）；远端是 35B MoE **thinking 模型**，同请求
  实测 4.1s 与 104.7s（思考链长度随机）。
- `OpenAIDriver` 超时硬编码 120s，且 `timeout` 会**混入请求 payload**（原代码
  `for k,v in kwargs.items(): payload[k]=v`，`{"timeout":120}` 直接发上游）。
- 多模态阶段所有模态共用 `MAX_PARALLEL_MULTIMODAL` 一个信号量，图片（4~105s）与
  表格/公式（2~3s）同池 → 慢 VLM 头阻塞 + 远端槽位排队 → 排队项超 120s 首试必超。
  观测：retry 1/2 全部成功、0 次 retry 2/2、0 次最终失败——超时而非失败。

方案 B（VLM 独立限流）：

| 位置 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | `MultimodalStage`（STANDALONE 路径）双层信号量：通用 `semaphore`（`config.max_parallel_multimodal`，同旧口径）负责 table/equation/audio 等 LLM 快任务；新增 `vlm_semaphore`（`config.max_parallel_vlm` → 回退 `max_parallel_multimodal` → 回退 2）。**image 只吃 VLM 信号量**（直连 VLM 不消耗 llm-gateway RPM，不占通用额度）；**video 两层都吃**（关键帧 VLM + MapReduce LLM）；其余沿用通用。无死锁：持通用者永不等待 VLM。启动日志输出两路并发值 |
| `raganything/config.py` | 新增 `max_parallel_vlm: int`（env `MAX_PARALLEL_VLM` 默认 2，`get_env_value` 模式同 `max_parallel_multimodal`） |
| `raganything/models/drivers/openai.py` | **payload 污染修复**：`timeout = httpx.Timeout(float(kwargs.pop("timeout", 120.0)))`——pop 后再合并剩余 kwargs，`timeout` 不再发进请求体 |
| `raganything/models/adapters.py` | 新增 `_vlm_timeout()`（env `RAG_VLM_TIMEOUT` 默认 300）；`legacy_vlm_adapter` 与 `base64_caption_adapter` 调用前 `kwargs.setdefault("timeout", _vlm_timeout())`（驱动默认 120s 对 thinking 模型太紧，长思考+排队时首试必超） |
| `deploy/.env` / `docker-compose.yml` / `deploy/.env.example` / `.env.local.example` | 新增 `MAX_PARALLEL_VLM=2`、`RAG_VLM_TIMEOUT=300`（均 atomic-rag 进程读取，落点 .env / 本地调试 .env.local）；`MAX_PARALLEL_MULTIMODAL` 注释改为「LLM 快任务口径，图片不在此列」；实际并发 ≈ WORKER_COUNT × 该值。顺带补齐 `.env.local.example` 旧漏的 §36 block-packing 与 §38 `RAG_ASSET_UPLOAD_ENABLED` 变量 |

测试：`tests/test_pipeline/test_multimodal_stage_vlm_limit.py`（新 6，RAG venv 跑，
并发峰值/重叠对观测，无真实网络）：

- image 只吃 VLM：通用=1 时图片仍 2 并发且与表格同帧
- video 两层都吃：通用=1 压到 1；通用=10 时受 vlm=2 约束
- LLM 快任务不受 VLM 拖累：vlm=1 时表格仍 4 并发
- 回退：缺 `max_parallel_vlm` → 取 `max_parallel_multimodal`；两者都缺 → 2

### 重建

- `atomic-rag`（stages/config/drivers/adapters 改动必须重建镜像；`.env` 变量重启容器即生效）

灰度：`MAX_PARALLEL_VLM` 可独立调大调小（图片吞吐 ↔ 远端 VLM 槽位压力）；
`RAG_VLM_TIMEOUT=300` 兜底长思考，若仍有首试超时可再上调（5min 为经验上限，
再长说明排队而非思考）。未采纳的方案 A（VLM max_tokens 封顶 128，同请求实测
2.7s，副作用是描述信息量下降）留档备查。

## §40 `2026-08-21` — 图片描述 prompt 精简 + VLM max_tokens 门控（docs/image-reference-accuracy-fix-plan.md 方案 D）

背景：§image-refs 准确性排查——图片描述 chunk 的 VLM 描述冗长、大量推测措辞
（「可能/看似/或许」），LLM 作答时把描述当正文事实复述；同轮修复还包括平台侧
（方案 A/B/C，见主仓计划文档 §12，本文件仅记录 raganything 侧改动）。

| 位置 | 改动 |
|------|------|
| `raganything/prompts_zh.py` | `vision_prompt` / `vision_prompt_with_context` 的 detailed_description 约束改为「用 1-3 句话客观描述图片内容要点：只描述可见内容、不推测、不解读创作意图；识别主要元素（有把握才写）；禁止'可能/看似/或许/似乎/大概'等推测措辞」（生产 `RAG_PROMPT_LANG=zh` 走此模板） |
| `raganything/prompt.py` | 英文 `vision_prompt` / `vision_prompt_with_context` 同步同口径精简（concise 1-3 sentences, describe only what is visible, no speculative wording） |
| `raganything/models/adapters.py` | `base64_caption_adapter` 新增 `RAG_VLM_MAX_TOKENS` 输出门控（0=不限制，默认关）：低量化 thinking 模型描述冗长时可 300 起灰度；注释警示过小会截断 JSON 导致 `_parse_response` 重试失败 |

生效范围：仅**新入库**文档（存量 chunk 描述不变），与 reparse 批次合并生效。
灰度：`RAG_VLM_MAX_TOKENS=0`（默认）关闭长度门控；prompt 精简随 git revert 回退。
关联：§39 方案 A（VLM max_tokens 封顶 128，实测 2.7s）为本次门控的留档依据，
默认仍不开启——截断 JSON 风险高于冗长收益，等换高质量 VLM 或按需灰度。

## §41 `2026-08-25` — 图片版面主题锚点注入（docs/image-reference-accuracy-fix-plan.md E 方案 P0-6）

背景：图片引用准确性排查的最终方案——「花瓜鱼蟹四屏」查询缺一屏的根因是 VLM
主题关联系统性失败（低量化 35B VLM 在多候选上下文中 3 次全错配），描述缺失核心
关键词导致 embedding 不召回。方案 E 不依赖 VLM 判断，改从 MinerU 结构化产物
content_list 确定性提取「版面主题锚点」，注入图片描述 chunk 正文前缀。

| 位置 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | 新增 `_extract_image_anchor(content_list, item)` + helpers（`_bbox_x_overlap`/`_clean_anchor_text`）：规则①取 `img_caption`/`img_footnote`（过滤图内编号噪声：纯数字/短编号如 `2`、`1-1`）→ 规则②同页 x 重叠（≥15）且 y 在图下方的短文本（≤160 字符，容忍 25）取最近邻 → 规则③仅本页标题（text_level>0）：图上方最近标题优先，无则取页面最靠上标题；**不做跨页回溯**（实测回溯 14/14 全错配，宁缺毋滥）。锚点长度预算 80 字符超限截断 |
| 同上 | `_build_file_source` 新增 `anchor_src` 参数，写入 `anchor_src=cap\|foot\|col\|heading` 旁路（解析侧 `file_source_util.parse_file_source` 已同步新增 `anchor_src` 键） |
| 同上 | `_collect_multimodal_chunks` 新增 `content_list` 参数（调用处传 `ctx.content_list`）；命中锚点时描述格式为 `图片描述：【版面主题：{anchor}】{VLM description}`——确定性锚点落在 embedding 前段 token，LightRAG 二次切块不会截断 |
| `tests/test_image_anchor.py` | 新增 20 用例：cap/foot 噪声过滤、col x+y 约束、heading 本页规则、不跨页、边界（None/空/无 bbox/截断/item_info 缺失），全部通过 |
| 同上（E2 修订） | 锚点函数迁至 `modalprocessors.py`（VLM 调用前需同模块复用；新增顶层 MinerU item 直传兼容），stages.py 改为 import（push 阶段重算同一纯函数，幂等一致） |
| `raganything/modalprocessors.py` | E2：`ImageModalProcessor.generate_description_only` 在 VLM 调用前提取锚点——有锚点时用断言式模板 `vision_prompt_with_anchor`（「此图是 {anchor} 的配图」，低量化 VLM 遵从度高于参考式）+ context 收窄到本页（window=0，跨页候选是错配根因）；无锚点保持 `_pick_prompt`。`ContextExtractor` 新增 window 参数透传，`_extract_page_context` **本页文本排最前**（修复保头截断挤掉本页的缺陷）。override 命中时锚点仍通过 context 收窄生效 |
| `raganything/prompt.py` / `prompts_zh.py` | 新增 `vision_prompt_with_anchor` 断言式模板（en/zh 对称，保留 §40 客观描述约束） |
| `tests/test_image_anchor.py`（E2 补充） | 新增 4 用例：顶层 item 直传 ×2、上下文本页优先重排、window=0 收窄；共 24 用例全部通过 |

生效范围：仅**新入库**文档（存量 chunk 无锚点，需 reparse；不做自动全量回填）。
灰度：`RAG_IMAGE_ANCHOR_ENABLED`（默认 true；false 完全回退旧行为，`content_list`
为空时也自动跳过，parse_only 旧链路不受影响）。
实测（岭南画派-10p，62 图）：锚点分布 cap 1 / foot 2 / col 21 / heading 24 / 无 14，
核心案例四屏 4 图全部命中「高剑父花瓜鱼蟹四屏」。
关联：主仓侧阈值 `RAG_IMAGE_REF_SCORE_MIN` 默认 0.3 与 llm-gateway rerank 分批修复
见主仓计划文档 §25。

## §42 `2026-08-26` — v1 退役查询契约补齐（主仓 docs/atomic-rag-v1-removal-and-v2-parity-execution-plan.md P1）

背景：v1 `LightRAGAdapter`（`jonex_core/.../lightrag_adapter.py`）退役删除、v2 成为唯一
适配器（LOCAL 面直接绑 `LightRAGAdapterV2`）。为保证「多 KB 只召回、平台侧作答」
（方案 A）与查询计量维度对齐 v1 `_jonex_query_headers`，为 v2 查询链路补齐
`only_need_context` / `user_id` / `trace_id` 透传。

| 位置 | 改动 |
|------|------|
| `atomic-rag-server-v2.py` | `handle_query` 提取 `only_need_context` / `trace_id` / `user_id` 并透传 `task_manager.query(...)`（`# [jonex]` 标记；`only_need_context=true` 时 LightRAG 提前返回 chunk 上下文、不调生成 LLM，由平台侧作答；trace/user 对齐 v1 计量维度） |
| `raganything/service/task_manager.py` | `TaskManager.query` 新增 kw-only 形参 `user_id` / `only_need_context`，透传 `self._http_client.query(...)`；docstring 中 v1 引用改为「已退役」表述 |
| `raganything/service/http_lightrag_client.py` | `query` 新增 kw-only 形参 `user_id` / `only_need_context`，请求 body 携带 `only_need_context`；`_query_headers` 新增 `user_id` 形参，非空时注入 `X-Jonex-User-Id`（配合既有 X-Jonex-Scene / X-Jonex-Tenant-Id / X-Jonex-Kb-Id / X-Jonex-Trace-Id / LIGHTRAG-WORKSPACE，补全 v1 六头） |

生效范围：查询链路（`capability_id=atomic.rag.lightrag.v1` 契约不变）。
关联：主仓侧 `lightrag_adapter_v2.py` / `client.py` 的 LOCAL/REMOTE 面同步透传同字段；
v1 删除与版本开关移除细节见主仓 `docs/atomic-rag-v1-removal-and-v2-parity-execution-plan.md`。

## §43 `2026-09-07` — 独立本体抽取链路切换（主仓 docs/ontology/ontology-native-extraction-execution-plan.md P4）

背景：本体抽取从「读 LightRAG 候选实体/关系 → 归类/定型」旧链路切换为
「读回 LightRAG chunk 存储原文 → `NativeOntologyExtractor` 独立抽取 →
custom-kg 回写」（计划 §5.3/§5.6/§5.7）。唯一不可灰度动作：atomic-rag
抽取链路切换，部署后需按 §7.4 P4 验证优先级闭环（引用回溯 / 删除清理 /
计数基线 / ontology_only 重抽）。

| 位置 | 改动 |
|------|------|
| `raganything/service/task_manager.py` | `_run_ontology_extraction` 整体重写：① `get_document_chunks` 读回权威 chunk（§5.6，禁止按 content 重算）；② 传 `compiled_schema.prompt_schema` 作 TBox 调 `NativeOntologyExtractor().extract()`（tbox 不可解析 → ok=false 整体不落库，§5.8 P1-4）；③ 两档全集经 `_writeback_custom_kg` 回写（§5.7：source_id 权威 chunk_id `<SEP>` 拼接、chunks 传空、full_doc_id 不传、失败重试 1 次不阻断本体）；④ Redis task 落 TBox 档（`ontology_entities`/`ontology_relations`，下游 reconcile 零改动）；⑤ `_serialize_relation` 补 `attributes`/`source_chunks`/`keywords`（既有序列化缺陷修复）；⑥ `_merge_table_objects` 并入目标改本体存储档（O6 去重只作用于 TBox 档，表格对象不回写 LightRAG——方案 C 通道已入库）；⑦ 跨文档判重缺口告警：`duplicated_chunk_count` 落 task、读回缺段记 warning（§8 风险表最后一行）；⑧ `force_edge_based` 参数保留签名兼容（native 无边定型概念，P5 清理） |
| `raganything/service/task_manager.py` | push 完成处补 `task.duplicated_chunk_count = getattr(ctx, 'duplicated_chunk_count', 0)`（P4 ⑦ 的配套） |
| `raganything/service/http_lightrag_client.py` | 新增 `insert_custom_kg(entities, relationships, *, tenant_id, kb_id)` → POST `/documents/custom-kg`（chunks 传空、full_doc_id 不传，重试/熔断复用 `_post`） |
| `tests/test_service/test_task_manager_ontology_p4.py` | 新增 12 测试：chunk 读回失败/空、TBox 档落盘布局、tbox 透传、custom-kg 映射契约（`<SEP>` 拼接去重、keywords/description 规则）、重试 1 次语义、判重缺口 warning |
| `tests/test_service/test_http_lightrag_client.py` | 新增 `TestInsertCustomKg`（请求契约：chunks 空、full_doc_id 不传） |

生效范围：本体抽取链路（Stage4）；`ontology_only` 重抽与常规 insert 读回口径一致。
依赖：lightrag 镜像 §12（JONEX_CHANGES：custom-kg source_id map miss 回退 +
full_* 补写 + SKIP 开关，默认 false）；`jonex_core` 的 `NativeOntologyExtractor`
随本镜像发布（P2 新增，此前未重建镜像）。
部署顺序（§7.4 P4）：先部署 atomic-rag 新镜像（LightRAG 仍在自抽，短时双份
抽取成本），确认 native 抽取 + custom-kg 回写正常后，再 P4.5 置
`SKIP_ENTITY_EXTRACTION=true` 关自抽。

## §44 `2026-09-07` — 旧本体链路清理（主仓 docs/ontology/ontology-native-extraction-execution-plan.md P5）

背景：P4 切换 + P4.5 自抽关闭验证通过后，按计划 §6.1 删除旧抽取链路资产
（旧 `OntologyExtractor` 类与全部死方法、6 个旧 PROMPT 模板、8 个旧链路
专用环境变量），atomic-rag 镜像 ×1。

| 位置 | 改动 |
|------|------|
| `raganything/service/task_manager.py` | `_run_ontology_extraction` 删除 `force_edge_based` 兼容参数及 ontology_only 调用点实参（旧边定型语义随旧链路废弃） |
| `raganything/service/http_lightrag_client.py` | 仅注释修正（v2 关系归一化注释不再提及已删旧类）；本体链路调用点 P4 重写时已清，`get_entities`/`get_relationships` 保留供前端存储浏览（计划 §6.3） |
| `jonex_core/capability/atomic/rag/ontology_extractor.py` | 重写为公共件模块：删除旧类与死方法；保留数据模型、`_openai_chat`、`_extract_concurrency`、`_load_json`/`_post_validate`/`_endpoint_ok`（提升为模块级函数）、`_resolve_schema`/`_get_compiled_client`（提升为模块级，保留 OntologyRegistry fallback） |
| `jonex_core/capability/atomic/rag/native_ontology_extractor.py` | 引用改模块级 `_post_validate`/`_load_json`；接保留环境变量（`ONTOLOGY_EXTRACT_BATCH_SIZE` 语义=每批 chunk 数、`ONTOLOGY_EXTRACT_MAX_TOKENS` 默认 8192、`ONTOLOGY_EXTRACT_JSON_MODE`、`ONTOLOGY_ENTITY_DESC_MAXLEN` 默认 2000），env 未设时行为与 P4 验证态一致 |
| 测试 | `test_post_validate_expressiveness.py` 改调模块级函数；`test_table_object_extractor.py` O6 用例改断言 native `_is_table_chunk` 同语义实现 |

环境变量（计划 §6.2）：删除 `ONTOLOGY_EXTRACT_DROP_CHUNKS` / `DROP_ORPHAN` /
`MAX_ENTITIES` / `MAX_CHUNKS` / `REL_BATCH_SIZE` / `KEEP_UNTYPED_EDGES` /
`REL_USE_ORIGINAL_ENDPOINT` / `TABLE_OBJECT_FILTER_LIGHTRAG_TABLE_ENTITIES`
（部署配置 deploy/.env、deploy/.env.example、.env.local(.example)、
docker-compose.yml 同步清理）；`ONTOLOGY_EXTRACT_MAX_TOKENS` 部署默认
4096→8192；deploy/.env.rag.example 补 `SKIP_ENTITY_EXTRACTION` 注释段。

## §45 `2026-09-08` — Skill/Action 本体增强 P7：procedural 抽取子管线 + TBox 构建技能链（主仓 docs/ontology/skill-action-ontology-enhancement-plan.md §2.5/M2）

背景：P6 已把 Skill/Action 实体、9 条关系与 Action 的 extraction_skills
声明落地 default.yaml 并试点 KB 生效。P7 实现抽取侧两类机制，atomic-rag
镜像 ×1（批 3）：① procedural 子管线——对含 Action 的 schema 抽取技能/
动作条目（M2 输出格式）；② 技能链执行器——声明过 extraction_skills 的
类型按声明顺序跑 refine→constrain 技能链，未声明类型走现有硬编码路径
（行为逐字段一致，锁单测防回归）。

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/ontology_extraction_skills.py`（新建） | 技能库 + 执行器：`parse_extraction_skills` 声明解析（未知分组/未知 builtin/前缀缺失报错，catalog 一期跳过+warning）；9 个内置确定性技能（step_serializer 补 step_no/NEXT_STEP/HAS_STEP 边属性 D3、contract_filler、semantic_definition 兜底摘句、name_normalizer 去敬语/序号/句末标点+超长截断进 description+挂接端点同步改名、endpoint_verifier 端点须在 chunk 正文、no_fabrication authorization/failure_policy 无原文支撑置空、synonym_merger 抽取期 no-op、dedup_merger casefold 合并、schema_validator 复用 `_post_validate`）；`run_entity_skill_chains` 声明类型逐个跑链，实体整类替换、挂接关系按对象身份 diff 同步（ctx 传 `list(attached)` 拷贝，防技能原地 remove 污染快照）；reject 只丢单实体/挂接不整批失败（D12） |
| `jonex_core/capability/atomic/rag/native_ontology_extractor.py` | procedural 子管线：`_PROCEDURAL_SIGNAL_RE` 信号词预筛（表格/图片 chunk 剔除、命中密度降序取前 N=30）；`PROCEDURAL_SYSTEM_PROMPT`（核心 4 字段宁缺毋假、端点逐字出现原文）/`PROCEDURAL_USER_PROMPT`（M2 输出格式）；`_parse_procedural_item`（契约字段 list 保持数组禁止 str() 字符串化、空串/空列表不入属性）；`_procedural_relations_from_item`（part_of_skill→HAS_STEP 带 step_no 边属性、next_action→NEXT_STEP、acts_on/uses_tool/performed_by/requires/results_in→对应关系端点 unknown）；`_parse_procedural_results`（`ONTOLOGY_PROCEDURAL_MAX_ACTIONS=100` 上限截断丢实体与挂接+告警）；`extract()` 接线：双 AND gate（`ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED` env 总闸默认关 AND options.procedural 任务层条件）+ 技能链调度（procedural 开或类型有 extraction_skills 声明时对声明类型跑链） |
| `jonex_core/capability/atomic/rag/ontology_extractor.py` | `_post_validate` inverse 翻转重建关系时补 `keywords`/`attributes`（既有缺陷，翻转丢 step_no 等） |
| `raganything/service/task_manager.py` | `extract()` 调用处按 schema 含 Action 组装 `options={"procedural": {}}`（每文档条件，未声明 Action 的 KB 不进 procedural 路径） |
| `tests/unit/test_ontology_extraction_skills.py`（新建） | 36 测试：声明解析 8（报错口径/前缀/catalog 跳过）、链调度 3（refine 先于 constrain、reject 只丢单实体、无声明类型输出原样零变化锁行为）、内置技能行为 11（name_normalizer/no_fabrication/dedup/step_serializer/schema_validator 逆名翻转/contract_filler）、链接线 2、procedural 8（预筛表格剔除+密度排序、LLM 输出解析契约字段、空字段允许、坏 JSON/调用失败、上限截断）、gate 判定 3（`is not None` 判定回归锁）、extract 集成 1（④ 主解析先于 ③′ procedural 的整链路回归锁） |
| `deploy/.env` / `deploy/.env.example` / `deploy/docker-compose.yml` | 新增 procedural 4 变量：`ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED`（默认 false，总闸）、`ONTOLOGY_PROCEDURAL_CHUNK_MAX`（30）、`ONTOLOGY_PROCEDURAL_MAX_ACTIONS`（100）、`ONTOLOGY_PROCEDURAL_NAME_MAXLEN`（30） |

gate：`ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED` 进程级总闸默认 false AND
options.procedural 任务层（schema 含 Action）双 AND，未声明 Action 的 KB
现有行为零变化（不可灰度动作仍只有 P4 抽取链路切换，P7 部署在默认关闸下
行为一致）。

部署顺序：重建 atomic-rag 镜像（批 3）→ 关闸验证（现有 KB 抽取结果与
P5 基线逐字段一致，技能链无声明类型不触发）→ 试点 KB 置
`ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED=true` → reextract_kb_documents →
list_entity_types 出 Skill/Action 实例（P7 e2e 验收）。

部署验证（`2026-09-08` 本地 Docker，试点 KB kb_demo_internet schema v2）：

- **关闸**：规程文档「信用卡挂失办理规程」上传 → 14 实体 7 关系零 procedural，
  Action 由 LLM 自由产出（native_llm），与 P5 基线行为一致 ✓
- **开闸**（总闸 true + force reparse）：
  - procedural 日志 `procedural: chunks=1 actions=3 skills=1 relations=13`
  - Skill 实例 0→1（「信用卡挂失办理」procedural_llm）；Action 7→8
    （「提交补卡申请」procedural_llm；「挂失冻结/挂失确认」与 native 同名，
    合并后 extraction_method 归首现 native——`_merge_entities` 保留第一个，语义自洽）
  - HAS_STEP 三步链 step_no 边属性 1/2/3、NEXT_STEP 链、5 挂接
    （ACTS_ON/PERFORMED_BY/REQUIRES/RESULTS_IN/USES_TOOL）全部落 Neo4j ✓
- **e2e 暴露并修复两个 bug**（均单测锁定回归）：
  1. gate falsy 陷阱：task_manager 传空 dict 占位，`bool({})` falsy → procedural
     永远关闸。修复：`_procedural_enabled()` 用 `is not None` 判定
  2. UnboundLocalError：extract() 里 `errors.extend(proc_errors)` 先于
     `errors = _parse_results(...)` 解包执行。修复：④ 主解析前移到 ③′ 之前
- 36 测试全绿；验证后总闸已恢复 false（默认关闸上线，P7 部署行为零变化）。
  ⚠️ 两个 bug 修复在镜像构建之后发现，上线前需**再重建一次 atomic-rag 镜像**固化。

## §46 `2026-09-08` — Skill/Action 本体增强 P8：检索增强（主仓 docs/ontology/skill-action-ontology-enhancement-plan.md §M3 批 4 减 M3-d/e）

背景：P7 已让 HAS_STEP/NEXT_STEP 步骤链与 5 挂接落 Neo4j，但检索侧
只有通用 neighbors()（自环类型 + per_hop_limit 截断会砍掉后半段）与
裸关系名（「SKILL 的 STEP_OF 是什么」问法不认 HAS_STEP 的 incoming 边）。
P8 实现批 4 检索增强剩余 4 项，kb-service 镜像 ×1（M3-d/e 已在 P1 提前完成）。

| 位置 | 改动 |
|------|------|
| `capabilities/knowledge_base/services/search_service.py` | **M3-a**：模块级 `_build_inverse_map`（只收有 inverse_of 声明的 `{name: inverse}`）+ `_expand_inverse_relations`（incoming 边翻译 relation_type 与 relation_chain 最后一跳，direction 保持 incoming 不翻转、多跳只换最后一跳、空 map 返回原列表引用零变化）；`ONTOLOGY_INVERSE_EXPAND_ENABLED` 开关默认 true；query_with_ontology 在 `_filter_stub_facts` 前接线（OntologyCompiler().get_compiled_schema 取声明，异常仅 warning 降级）；**M3-b**：`_PROCEDURAL_PATTERNS` 5 条（如何/步骤/流程/第 N 步/下一步/先…然后）+ `_resolve_intent_priority`（procedural 命中 → (True, False)，enum 判定不生效）+ 类型白名单 Skill/Action；**M3-c**：`_execute_graph_query` 加 procedural 分支（`prebuilt_facts` 透传，template_name="find_action_chain"）；**M3-f**：`_should_exempt_min_facts`（procedural 意图 + root 命中单个 Action 时豁免 ONTOLOGY_MIN_FACTS） |
| `capabilities/knowledge_base/repository/ontology_graph_repository.py`（新增 3 方法） | `find_action_chain(tenant, kb, root_name, root_type, max_steps=30)`：Skill root 走 HAS_STEP Cypher（OPTIONAL MATCH 展开 5 挂接 ACTS_ON/USES_TOOL/PERFORMED_BY/REQUIRES/RESULTS_IN，RETURN step_rel_attributes 等）；Action root 走 NEXT_STEP 变长 `*0..N`（N clamp 1..50 内联字面量，depth 作 step_no=depth+1，菱形链 (step_name, hook_type, target) 去重取最小 depth）；`_chain_step_no`（HAS_STEP 边属性 JSON 取 step_no，非法/缺失 → 999）；`_facts_from_chain_rows`（开头 sorted by step_no，每 step 首次出现产出步骤 fact + 挂接 fact（source=step），末尾调 `_deserialize_fact_entities` 反序列化）——排序在构造侧完成，answer_from_facts 函数体不动 |
| `tests/unit/test_ontology_procedural_search.py`（新建） | 39 测试：procedural 命中 9 + 未命中 5（含 enum 共存与优先锁）、inverse map 构建 3、inverse 展开 6（含空 map 返回原引用零变化、direction 不翻转）、chain step_no 3、chain rows 转换 6（排序/结构/挂接/NEXT_STEP/999/去重）、MIN_FACTS 豁免 3、Action 分支 Cypher 形态锁定 2（e2e bug 回归锁） |

M3-a′（answer_from_facts prompt 附加句）：**跳过代码改动**。answer_from_facts
在 `jonex_core/common/ontology_llm.py`，改动触发 kb-service + atomic-rag 双镜像，
与「镜像重建次数最小化」约束冲突且计划标注「可选」；步骤序信息已由 fact 的
step_no 字段携带，归档注明即可。

gate：`ONTOLOGY_INVERSE_EXPAND_ENABLED` 默认 true，但未声明 inverse_of 的
schema `_build_inverse_map` 返回 {} → 展开短路零变化（锁单测）；procedural
意图仅在 root 命中 Skill/Action 时走新模板，其余问法（含 enum 命中但无
procedural 信号）走原路径——除试点 KB 的步骤类问法外行为不变。

部署验证（`2026-09-08` 本地 Docker，kb-service 镜像重建后；试点 KB
kb_demo_internet schema v2）：

- **前置**：总闸临时开 + 规程文档 force reparse，procedural 数据恢复
  （Skill ×1 procedural_llm、Action 8 含「提交补卡申请」procedural_llm、
  HAS_STEP step_no 1/2/3、NEXT_STEP 链全部落 Neo4j）
- **① 步骤链问法**（「信用卡挂失办理的操作步骤是什么」）✓：timeline
  `procedural 意图命中：枚举判定不生效` → `图查询模板 find_action_chain
  取到 16 条事实` → 答案按 step_no 1/2/3 有序输出三步并带契约信息
  （身份核验/永久作废/6 个月无逾期）
- **② inverse 问法**（「挂失冻结是什么技能的步骤」）✓：timeline
  `incoming 边按 inverse 映射翻译（9 条声明）`，答案「是『信用卡挂失办理』
  技能的第 1 步」——incoming HAS_STEP 正确翻译为 STEP_OF 语义
- **③ 单 Action 问法**（「提交补卡申请下一步是什么」）✓：procedural 意图
  + Action root → NEXT_STEP 链模板取 4 条事实，答案「进入制卡环节」；
  长链问法（「挂失冻结下一步做什么」）按 step_no 1/2 有序并带后置步骤
- **e2e 暴露并修复 1 个 bug**（2 单测锁定形态）：Action 分支变长 Cypher
  用了 Neo4j 5 非法的「内联属性 map + 变长」组合
  （`-[:ONT_REL {relation_type:'NEXT_STEP'}*0..N]->` → SyntaxError
  `Invalid input '*'`）。修复：属性过滤移到 WHERE
  （`[rels:ONT_REL*0..N]` + `all(rel IN rels WHERE rel.relation_type='NEXT_STEP')`），
  容器热修验证通过后已恢复总闸 false。
- **固化镜像复核（2026-09-08 第二次重建后）** ✓：容器内确认修复形态
  `all(rel IN rels ...)` 就位、非法形态 `{relation_type:'NEXT_STEP'}*`
  不存在；总闸 false 下冒烟——步骤链/ inverse/ 单 Action 三类问法
  答案正确，Action 模板命中（`find_action_chain 取到 4 条事实`）
  无 SyntaxError。

回归：新增 39 单测全绿（37 + 2 个 Cypher 形态锁定）；tests/unit 全量
438 passed（test_rag_v2_query_parity 需 raganything 的 RAG venv 环境，
非本批改动）。

## §47 `2026-09-08` — Skill/Action 本体增强 P9：观测日志 + M4 协议交付（主仓 docs/ontology/skill-action-ontology-enhancement-plan.md §M4/批 5）

背景：P8 检索增强闭环后进入批 5 收尾。本批无行为变化——纯观测日志
（灰度期观测技能链 reject 率与契约字段空置率）+ M4 协议文档交付。
jonex_core 改动（capability.Dockerfile COPY jonex_core）触发
kb-service + atomic-rag 双镜像各 ×1。

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/ontology_extraction_skills.py` | `apply_declared_chain` 每技能一行观测日志 `技能链 <type>: skill=<name> refined=N rejected=M elapsed_ms=T`（refined=执行后实体数变化、rejected=本次新增拒绝数、elapsed=耗时；技能异常容错路径同样输出计数 0）——reject 率异常高说明该约束技能与语料风格不匹配（技能链可调，§2.5 可追踪性） |
| `jonex_core/capability/atomic/rag/native_ontology_extractor.py` | procedural 统计日志后补契约字段空置率 `procedural 契约空置率: actions=N authorization_vacant=P% failure_policy_vacant=Q%`——空置率异常高说明语料本就不含契约信息，或预筛信号词过于宽松（置空≠无约束，M4 按 D15 消费） |
| `tests/unit/test_ontology_extraction_skills.py` | +2 测试（TestSkillChainExecLog）：每技能一行日志含计数与 elapsed_ms、技能异常容错仍输出计数 0 |
| `docs/ontology/action-execution-boundary-protocol.md`（新建） | M4 执行边界消费协议：本体只定义边界不执行动作；fact 消费数据形态（step_no 有序、source_chunks 引用链）；契约字段→执行器边界映射表（atomic_effects/follow_ups schema 预留一期未填充，缺失=无联动安全方向）；D15 缺失语义（authorization 缺失=未知→拒绝/转人工，显式「无需授权」才落无约束，唯一「缺失即风险」字段）；failure_policy 是文字规则不是代码；单一事实源约定（不是「唯一写入通道」）；消费侧伪代码示例；P9 观测项 |
| `docs/README.md` | ontology 主题包索引登记新协议文档 |

gate：批 5 无新开关、无行为变化；观测日志仅在 procedural 开闸/技能链
执行时产出，关闸（默认）状态下与 P5 基线行为一致。

回归：新增 2 单测全绿；本体相关 7 文件 187 passed；compileall 全仓通过。

部署顺序：重建 kb-service + atomic-rag 镜像（批 5，双镜像各 ×1）→
关闸冒烟（现有 KB 抽取结果与 P5 基线一致）→ 总闸临时开 + 试点 KB 重抽 →
观测三类日志（procedural 统计 / 契约空置率 / 技能链执行）→ 总闸恢复
false。M4 为纯协议文档，无部署动作。

## §48 `2026-09-08` — P9 部署验证暴露的两个抽取链路 bug 修复

背景：§47 部署验证开闸重抽后，技能链执行日志始终缺失，追查发现
P7 以来技能链在真实链路从未执行过（P7 遗留）；技能链首次真实执行后，
endpoint_verifier 又暴露标点/空白差异误杀。两个 bug 均在 P9 验证中
定位并修复（kb-service 改动 ×1、jonex_core 改动 ×1，jonex_core 触发
kb-service + atomic-rag 双镜像）。

### Bug 1：prompt_schema 不透传 extraction_skills（P7 遗留，技能链从未执行）

根因链：task_manager 把 `compiled_schema["prompt_schema"]` 作为 tbox 传给
extract() → `_build_prompt_schema` 的 entity_types 输出只有
name/aliases/attributes（无 extraction_skills）→ run_entity_skill_chains 的
decl_map 恒空 → `if not decl_map: return entities, relations, [], []` 静默跳过。
自 P7 起技能链从未在真实抽取链路执行（单元测试直接传含 extraction_skills
的 schema，故未暴露）。

| 位置 | 改动 |
|------|------|
| `capabilities/knowledge_base/services/ontology_compiler.py` | `_build_prompt_schema` entity_types 输出增加 `"extraction_skills": entity_type.get("extraction_skills")`（无声明为 None；LLM prompt 渲染只取 name 列表不受影响） |
| `tests/unit/test_ontology_schema_expressiveness.py` | +2 测试：extraction_skills 透传进 prompt_schema / 无声明时输出 None |

生效路径：prompt_schema 是编译产物，`save_compiled_schema`（前端编辑器
保存同路径）才会重编译；试点 KB v2→v3 重编译后重抽，9 技能全跑
（技能链日志每技能一行），Neo4j 核心结构（HAS_STEP 三步链 / 5 挂接）
完整，schema_validator 拒绝 6 条为 schema 外低质挂接（与 _post_validate
语义一致）。存量 schema_version 不变则行为零变化（旧 prompt_schema 无
extraction_skills，decl_map 恒空路径不变）。

### Bug 2：endpoint_verifier 标点/空白差异误杀

症状：重抽后 3 条挂接被拒，逐字比对原文确认全为仅标点/空白差异——
原文「请点击「挂失冻结」按钮办理」vs 端点「挂失冻结按钮」；原文
「系统核验客户近 6 个月无逾期记录」vs 端点「近6个月无逾期记录」；
原文「卡片状态变更为「已冻结」」vs 端点「已冻结」。

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/ontology_extraction_skills.py` | `_chunk_mentions` 三级比对：逐字 → 归一化（`_NORM_STRIP` 去空白与引号类标点）→ 按标点拆主体过半命中。归一化比对使仅标点/空白差异不再作为「不在原文」依据 |
| `tests/unit/test_ontology_extraction_skills.py` | +4 测试（TestChunkMentionsNormalization）：引号包裹/空格差异/引号状态词匹配，真正不在原文的值仍拒绝（防过度放宽） |

验证：热修重抽后 endpoint_verifier rejected 3→1。剩余 1 条为真实字面
差异（端点「身份核验通过」vs 原文「身份核验必须通过」，LLM 去修饰词），
保守拒绝合理，符合 D12 丢弃单挂接设计，不再放宽。Neo4j 核心结构
（HAS_STEP 5 挂接 / NEXT_STEP 三步链 / RESULTS_IN·PERFORMED_BY·USES_TOOL
·ACTS_ON）完整。验证后总闸已恢复 false。

回归：test_ontology_extraction_skills 42 passed、test_ontology_schema_
expressiveness + test_ontology_procedural_search 70 passed。

部署顺序：重建 kb-service + atomic-rag 镜像（jonex_core 改动双触发）→
关闸冒烟（行为与基线一致）→ 完成固化。总闸保持 false。

## §49 `2026-09-09` — 独立抽取器召回修复 P0-P5（30 vs 107 排查落地）

背景：同一文档（Xiaomi MiMo-V2-Flash.md）57 老链路抽 107 个本体实例，
P4 新链路仅 30 个。验证 A（Neo4j 图侧实体数=30、类型全英文领域类型、
零中文兜底档）→ 主因是**召回**（LLM 真只抽了 30 个，不是抽了被判出
本体）；验证 B（72h 无截断日志）→ 大批判注意力稀释下的自然少输出。

按用户 review 修正方案 P0-P5 落地（全部在 `jonex_core`，无灰度行为
变化，均为默认值收敛到更优配置）：

| # | 位置 | 改动 |
|---|------|------|
| P0 | `native_ontology_extractor.py` `_build_prompts`/`_render_type_def` | 类型定义渲染 name + 中文 aliases + 属性表（只渲染属性非空类型，属性行 name + description 截断 40 字）。旧实现只传 name：丢 aliases 使中文语义入口被掐（中文实体被中文兜底池吸引、档位判出本体），丢 attributes 使属性 schema 不进 prompt（属性必然全空） |
| P1 | `native_ontology_extractor.py` `extract()` ② 段 | 每批 chunk 数 = `max_tokens // ONTOLOGY_EXTRACT_PER_CHUNK_OUTPUT_RESERVE`（默认 1000 → 8192/1000≈8 chunk/批），与 BATCH_SIZE 取小；输入预算保留 AND 上限 |
| P2 | `native_ontology_extractor.py` `_run_gleaning` + `NATIVE_GLEANING_USER_PROMPT` | 实现 gleaning 补抽（§5.4 设计、此前代码从未实现）：每批回传已抽实体名只输出遗漏，默认 1 轮（`ONTOLOGY_EXTRACT_GLEANING`）；防御过滤只收新实体与至少一端为新实体的关系；某批一轮无新增即停止后续轮（temperature=0 同 prompt 必然同输出） |
| P3 | `native_ontology_extractor.py` `_backfill_source_chunks` | chunk 引用确定性回填：canonical_name + aliases 经 `_chunk_mentions` 归一化三级比对匹配 chunk 正文，命中并集追加 source_chunks（不删 LLM 声明，覆盖「索引有效但不准」）；关系取两端命中交集（同现支撑）、空则并集 |
| P4 | `native_ontology_extractor.py` `_run_batches`/`_run_procedural` + `ontology_extractor.py` `_load_json`/`_recover_truncated_objects` + `extract()` ⑦ 段 | ① 截断不再降半重试（temperature=0 同 prompt 必然再 length，纯浪费调用），直接按 chunk 拆批；单 chunk 仍截断保留截断输出；② `_load_json` 增加截断部分恢复（entities/relations 大括号平衡扫描逐对象解析，完整对象保住，不再整批清零）；③ ⑦ endpoint_backfill 实体继承来源关系 source_chunks（旧实现恒空 → custom-kg 图节点 source_id/file_path 空串脏数据） |
| P5 | `deploy/docker-compose.yml` / `.env.example` / `.env.rag.example` / `deploy/README.md` | 暴露三个变量：`ONTOLOGY_EXTRACT_BATCH_BUDGET_TOKENS`(24000)、`ONTOLOGY_EXTRACT_PER_CHUNK_OUTPUT_RESERVE`(1000)、`ONTOLOGY_EXTRACT_GLEANING`(1)（BATCH_BUDGET 此前从未在 compose/env 暴露） |

回归：`tests/unit/test_native_ontology_extractor.py` 33→51 用例全过（P0 类型
渲染、P1 输出预算不变式、P2 gleaning 过滤/停止轮/集成、P3 回填并集与
交集回落、P4 直接拆批/部分恢复/端点继承）；`tests/unit` 全量 473 passed。
截断行为变更注意：`_run_batches` 不再产生 4096 降半调用（调用序列断言
已更新）；`_load_json` 对截断输出从「整批清零」改为「部分恢复」。

部署顺序：重建 kb-service + atomic-rag 镜像（`jonex_core` 改动双触发，
`capability.Dockerfile` COPY jonex_core）→ 本地 force reparse 对照文档
验证产出密度回升 → 灰度观察。总闸 `ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED`
保持 false（与本次改动无关）。

### P0-fix `2026-09-09` — 重建后验证暴露的属性说明透传丢失（32 本体 + 属性全无排查）

重建镜像 force reparse 后：产出 34 本体（kg 档 36，P0-P5 召回改善已生效
30→36），但属性仍全无（with_attrs 0/24）。根因不在 P0 渲染（容器内打印
实际 prompt 确认属性表已进 prompt），而在**上游两条链路的属性语义说明
被丢弃**——prompt 属性表只剩「属性名 → 属性名」，LLM 无说明可依整批
不填 attributes：

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/ontology/registry.py` `to_prompt_json` | 属性此前只输出 name/type/required，`description` 根本不进 prompt_schema；现输出 `description`（缺失回空串） |
| `capabilities/knowledge_base/services/ontology_compiler.py` `_compile_from_yaml_fallback` | 属性 `description` 硬编码 `""`（default.yaml「法定全称」等说明编译即丢）；现透传 `attr.description or ""` |
| `jonex_core/capability/atomic/ontology/models.py` `from_compiled_dict` | `description=display_name or description` 恒取 display_name（fallback=name 恒非空），description 永不被采用；反转为 description 优先，属性/关系两处同修 |

数量差距另一半根因（34 vs 107，结构性非 bug）：22 个 chunk 中 14 个是
表格明细 chunk 被 O6 剔除、8 个文本 chunk 成 1 批抽取；Row-as-Object
零产出——`.md` 直通解析 content_list 仅 2 个 block 且无 `type=table`
item（MinerU 不把 markdown 表格识别为 table block，表格明细 chunk 是
push 阶段 RAG_TABLE_GRID_V2 网格化产物），`_merge_table_objects` 静默
跳过（`ctx.content_list` 有值但无 table item，零日志零产出）。老链路
107 个包含表格 benchmark 内容，差距即表格内容未进入任何抽取路径。
待定方案：Row-as-Object 输入源改为 push 阶段已网格化的表格 chunk 元
数据（确定性反序列化），或接受表格不进本体只进 RAG 检索。

回归：`tests/unit/test_ontology_schema_expressiveness.py` +5 用例
（TestSchemaDescriptionPassthrough：to_prompt_json 透传/缺失回空、
yaml fallback 编译透传、from_compiled_dict 优先级与回落）；
`tests/unit` 全量 478 passed。

排查假象备忘：宿主机直连微型测试出现「finish_reason=length + content
空」——`.env.local` 的 `ONTOLOGY_LLM_BINDING_HOST` 指向 57 网关且测试
未传 scope（无 `X-Jonex-Scene` 头）→ 网关不识别 scene 不注入
`thinking.disabled` → 上游思考模式吃光 2048 预算（思考+正文共享预算
tokenhub 行为）。真实链路（容器内带 scope）4 条调用全部正常返回，非
链路 bug。排查时微型测试须带 scope 或显式关思考。

### P0-fix 补充 `2026-09-09` — 关系约束字段透传（用户核实第四节第 1/2 条）

用户核实指出：属性全无与关系丢失是同一个病（编译档有字段、prompt_schema
丢字段），且关系侧更严重——`_post_validate` 读 `source_types`/`target_types`/
`inverse_of`，两条 prompt_schema 构建路径都不输出：

| 位置 | 改动 |
|------|------|
| `jonex_core/capability/atomic/ontology/registry.py` `to_prompt_json` | relation 此前只输出 name/source/target；现补 description/cardinality/inverse_of/source_types/target_types（yaml fallback 路径的 prompt_schema 来源） |
| `capabilities/knowledge_base/services/ontology_compiler.py` `_build_prompt_schema` | relation 此前缺 source_types/target_types/inverse_of；现补齐（编辑器/模板路径） |
| `capabilities/knowledge_base/services/ontology_compiler.py` `_compile_from_yaml_fallback` | relation `description` 硬编码 `""` → 透传 `relation_type.description or ""`（`_writeback_custom_kg` 的 rel_desc 读完整 compiled 档，此前 LightRAG 图 TBox 内关系描述恒空） |

修复前影响：`allowed_src/tgt = set(source_types) or {source}` 恒回落单值 →
多态扩展（ACTS_ON.target_types:[Product,Concept,Location] 等）全失效，
非主值类型关系被丢；`inverse_map` 恒空 → LLM 输出逆名（STEP_OF/PERFORMS/
ACTED_ON_BY）的关系全部静默丢弃。

回归：`test_ontology_schema_expressiveness.py` 36→40 用例（+4：
TestRelationConstraintPassthrough——两条构建路径字段透传、yaml fallback
关系说明透传、`_post_validate` 端到端契约：多态扩展保留 + 逆名换 canonical
并翻转端点；另更新 TestBuildPromptSchema 精确断言纳入三个新字段）。
`tests/unit` 全量 482 passed。

⚠️ 重抽前置（compiled schema 是 KB 级，删文档重传不重置）：
- rebuild atomic-rag + knowledge-base 两个镜像；
- KB 级重编译：invoke action `recompile_schema`（`force=true,
  apply_to_documents=true`，gateway 路由 `/api/v1/knowledge-base/…`），
  编译后新 prompt_schema 覆盖 PG + Redis，文档置 PENDING 由对账重抽。
  存量 PG 行 prompt_schema 为旧格式（无 description/关系约束字段），
  不重编译则 Redis 过期后 PG 旧行回写缓存，抽到的还是旧 schema。

### P0-fix 验证结论 `2026-09-09` — 重编译后实体 25 非退化；with_attrs=0 是语料无值

重编译（schema v2，属性说明+关系约束已进 prompt）后重抽 kg_entities=25，
低于上轮 36。容器内同输入复跑 4 次：25（真实任务）/ 36（A 冒烟）/
31（run1）/ 27（run2）——**上游 temperature=0 采样非严格确定**
（deepseek-v4-flash 经 tokenhub，波动带 25~39）；A/B 对照（v2 新格式 vs
模拟旧格式 prompt_schema）36 vs 39 无显著差异。结论：数量下降是方差，
非 schema 变更退化。

with_attrs=0/36 恒定（5 次实验全 0）。单 chunk 微型测试定位：
① 语料含属性值句（「Xiaomi is a consumer electronics...」）→ 模型正确
填 `{"legal_name": "Xiaomi", "industry": "consumer electronics and smart
manufacturing"}`——**P0-fix 修复有效，模型会填属性**；
② 真实 chunk（[17] 架构章节，无编造句）→ 全部留空——该英文技术报告
语料里没有 TBox 组织档案类属性（法定全称/所属行业/职位头衔）的值，
模型遵守「禁止编造」留空是正确行为。缩批对照（reserve=3000 →
2 chunk/批 3 批）with_attrs 仍 0，排除注意力稀释主因。

待决策：
- 属性：KB 级 TBox 属性语义与语料对齐（前端编辑适配技术文档的属性，
  如 Product 加「参数量/上下文长度」），或换组织档案类文档验证；
  代码层无 bug，不改。
- 数量回不到 107：结构性原因是 17/22 表格 chunk 被 O6 剔除且
  Row-as-Object 静默零产出（content_list 无 table item），非召回。

## §50 `2026-09-09` — P5-var 采样方差收敛打包（用户 review 四部分落地，同一次 rebuild）

用户 review 确认：上游 temperature=0 非位精确（同输入 4 次 25/27/31/36），
且 reconcile 是「删了重写」（reconciliation_service `delete_by_document` →
`merge_entity` 全量替换）——方差不是解释而是缺陷，方差下沿 = 数据损失。
按「成本递增」四抓手 + 可观测全部落地，与关系约束字段透传（§49）打包
进同一次 rebuild：

**1. gleaning 1→2（零代码成本抓手）**
- `native_ontology_extractor._gleaning_rounds` env 默认 1→2
  （`ONTOLOGY_EXTRACT_GLEANING`，compose 同步改默认 2）。
- `_run_gleaning` 增加按批内 chunk_id 集合去重：多次采样下 raw_items 含
  同批多份，同批 known 相同、重复补抽必然无新增，纯浪费调用。

**2. 多次独立采样取并集（self-consistency，成本 ≈2x）**
- 主批 `_run_batches` 跑 `ONTOLOGY_NATIVE_SAMPLE_RUNS` 次（默认 2；
  opts `sample_runs` 优先；`_sample_runs` 下限 1 不可关闭主批），各次
  产出全量解析后由 ⑤ 按 canonical_name 并集合并。
- 新增 `_merge_relations`：同名边主键 (source_name, target_name,
  relation_type) 合并（confidence 取大、chunks 去重聚合、keywords 首条
  非空优先、attributes 首见优先）——不合并会翻倍污染关系数。

**3. reconcile 退化闸门 + delta 日志（数据保护）**
- 新 env `ONTOLOGY_RECONCILE_DEGRADE_GUARD_THRESHOLD`（默认 0.3，kb-service
  侧；0 关闭）。写图前 `count_entities_by_document`（新 repo 方法）对比
  本次产出：跌幅超阈值 → `logger.error` 告警并**拒绝覆盖**（不 delete、
  不 merge，旧图数据保留，文档正常收尾）；首次入库（before=0）不触发。
- 写图日志扩展 `before/after/delta`（写后再统计一次；拒绝覆盖时
  before==after；delta 查询失败只降级日志，不影响写图结果判定）。
- 纯函数 `_degrade_guard_rejects(before, incoming, threshold)` 独立可测。

**4. O6 条件剔除（方案 B，约 3 行）**
- 仅当任务层确认 Row-as-Object 已接管表格（content_list 存在
  `type=="table"` item，经 options `table_objects_handled` 传入）才剔除
  表格 chunk；无接管时表格 chunk 进 native 文本抽取（LLM 兜底修本体
  召回）。options 无该键 = 旧行为恒剔除（行为零变化保守默认）。
- 局限保留：表格对象仍只进本体归档不进 LightRAG 图（方案 A 输入源
  改造推迟到下一轮，等 B 堵住出血点、方差收敛后再评估）。

**5. 观测补齐**
- `_post_validate` reject 计数日志：实体按 TBox 外类型分桶、关系按
  no_constraint / endpoint_mismatch 分桶——区分「没抽出来」与
  「抽出来被校验丢了」。
- 抽取完成日志新增 with_attrs 比例 + TBox 属性零命中清单
  （`zero_hit=[...]`，区分语料无值与结构上不可能）。

**测试**：主仓 `tests/unit` 498 passed（新增 TestSampleRuns /
TestTableChunkConditionalRemoval / TestPostValidateRejectLogging /
TestReconcileDegradeGuard 等 16 例）；Rag-anything 侧
test_task_manager_ontology_p4.py 12 passed。精确调用序列断言类测试
（TestTruncation 两例）补设 `ONTOLOGY_NATIVE_SAMPLE_RUNS=1`。

**镜像重建批次**：`jonex_core` 改动 → atomic-rag 镜像；kb-service
（reconciliation_service / ontology_graph_repository）→ kb-service 镜像。


## §51 `2026-09-12` — P0-b 表格上下文跨任务回放（重抽不再丢表格对象）

**问题**：Row-as-Object 表格对象只从 `content_list` 的 table item 推导
（`_merge_table_objects` 的唯一输入），而 `ontology_only` 重抽既不本地化
原文件也不解析（§43 P0-A）→ `ctx.content_list` 恒为空 → 重抽不产出表格
对象；写图又是「`delete_by_document` 全删 + merge」的全量替换
（kb-service `reconciliation_service._rewrite_ontology_graph`）→ 首轮由表格
对象建立的节点被整体静默换掉。表格为主的文档更糟：native 侧若把表格
chunk 判为不可用，直接零产出。

**为什么不让重抽自己重新解析**：解析缓存键含 `str(file_path.absolute())`
与 `st_mtime`（`cache.ParseCacheManager.generate_cache_key`），而
`_fetch_cos_object` 每次下载都落在带随机 uuid 前缀的新路径上 → key 必不同
→ 必然 miss → 每次都要重解一遍原文件。代价与失败面都远大于「首轮存一份」。

**修法**（均在 `service/task_manager.py` + `pipeline/base.py`）：full 链路进
本体阶段时把 content_list 的表格子集 gzip 落盘（`{WORKING_DIR}/_table_ctx/
{tenant}/{kb}/{document_id}.json.gz`，持久卷，跨容器重启/重建存活），
`ontology_only` 读回放进 `ctx.content_list`——让 `table_handled` /
`_merge_table_objects` / P10 批 2.4 零产出兜底三条既有逻辑**原样生效**，
判据一个不改，重抽口径自然与首轮一致。

- 新增模块级函数：`_table_context_dir` / `_table_context_path` /
  `_table_context_items` / `_persist_table_context` / `_load_table_context` /
  `_sweep_table_context`。
- 存档条目一律扁平化为 `{type, table_body, table_caption, table_idx}`
  （表体统一落 `table_body` 键 = `get_table_body` 的首选别名），**不带
  `_mapped_tables` 嵌套**——否则读回后同一个表会被 `_merge_table_objects`
  当作顶层 item 和内层条目各消费一次。
- 落盘点在 `_run_ontology_extraction` 开头、**所有早退之前**（chunk 读回
  失败 / native 抽取异常都拦不住本体重试，但存档要已经就位）。
- **存档缺失 → 拒绝重抽，不降级**（`_run_ontology_extraction` 内的 P0-b
  闸门）。判据三重收敛：`ctx.ontology_only`（新增字段，只有重抽才存在
  「全量替换掉首轮好结果」的问题）**且** `ctx.content_list is None`
  （空列表是「首轮确认无表格」的有效存档）**且** chunk 侧确有表格
  （`NativeOntologyExtractor._is_table_chunk`，避免误伤纯文本文档）。
  命中即 `task.ontology_status = "failed"` + `ontology_error` 写明需
  `force_reparse` + `return`，不调 `extract()`。
  理由：降级把表格当普通文本抽，产出无结构化 attributes、无复合主键区分的
  通用实体，再经 `delete_by_document` 全量替换——不是「补上缺失的」，是
  「用差的换掉好的」；代价还是全部 chunk 进 LLM（实测 24212 chunk / 3027 批
  ≈45 分钟）+ backfill 分钟级 CPU。拒绝则首轮结果一行不动，KB 侧见
  `ontology_status=failed` 走 reconciliation 的「已完成但无 ontology_data」
  分支，最多空转 `MAX_ONTOLOGY_RETRIES` 次后落 FAILED 并把指引透到文档
  error，**全程不调 `_handle_completed`、不碰 Neo4j**。
- 存档三态：文件不存在（无存档 → 交 P0-b 闸门裁决）/ 空列表（首轮确认无
  表格 → 正常跑）/ 非空（回放）。故「无表格但 content_list 非空」时仍要写
  一个空列表存档，否则「本来就没表格」会被误报成「存档丢失」。
- **gzip 压缩存档**：表体是高重复结构化文本，实测 XBRL 财报 1015 张表 /
  29431 行的表体原文 **23.72MB → 压缩后 0.57MB（41.5x）**，磁盘占用降到
  1/40。读取端按 gzip 魔数 `1f 8b` 判定，非 gzip 则按明文 JSON 解析（方便
  运维手工塞存档排查）。
- **上限口径 = 解压后字节**（`RAG_TABLE_CTX_MAX_BYTES`，默认 **64MiB**）。
  真约束不是磁盘而是读回时的内存：`_load_table_context` 一次性
  `gzip.decompress` + `json.loads`，再经 `_merge_table_objects` 展开成
  Python 对象（JSON 文本的 3~5 倍）。**不能按压缩后判**——压缩比是文档属性、
  能差两个数量级：同一份「8MiB 压缩后」的存档，低压缩比文档解压后 24MB、
  高重复文档可达 348MB，用它当闸门约束不住内存。64MiB ≈ 200~320MB Python
  对象，乘 `WORKER_COUNT=4`（atomic-rag 容器无 `mem_limit`）量级可控；
  gzip 只负责省磁盘。实测最脏文档 23.72MB 有 2.7x 余量。
- **TTL 回收**`_sweep_table_context()`：仿 `_sweep_cos_cache`，TTL 默认 90 天
  （`RAG_TABLE_CTX_TTL_SEC`，≤0 关停），模块级时间戳按 1 小时节流（存档是
  {tenant}/{kb}/{doc} 三层树、文件数随文档数增长，不能每次落盘都全树遍历），
  在 `_persist_table_context` 里顺带触发。不做文档删除链路钩子。
- 独立目录 `_table_ctx` 而非并入 `_cos_cache`：后者有 24h TTL 清扫
  （`_sweep_cos_cache`），存档混进去会被清掉。
- 已知局限：存档是首轮 parse 的产物，而 chunk 可被 `update_chunk` 端点改写；
  改写后回放的表格上下文与实际 chunk 可能漂移（概率低）。要根治需在
  `update_chunk` 时失效存档，未做。
- 可选 env：`RAG_TABLE_CTX_DIR`（默认 `KB_INPUT_DIR`/`WORKING_DIR`
  ——容器内即 `/app/rag_storage`——下的 `_table_ctx`）、
  `RAG_TABLE_CTX_MAX_BYTES`（默认 **64MiB，按解压后字节**——内存保护的口径，
  不是压缩后；超限不落盘 + 告警，并在日志里点明该文档日后重抽会被 P0-b 闸门
  拒绝）、`RAG_TABLE_CTX_TTL_SEC`（默认 90 天）。
- 落盘格式：`gzip.compress(json.dumps(...), 6)`，文件后缀 `.json.gz`；读回时按
  魔数 `\x1f\x8b` 判 gzip、否则按明文 JSON 读（兼容旧存档/手工产物）。gzip 只省
  磁盘，**不参与闸门判定**。

**测试**：新增 `tests/test_service/test_table_context_replay.py` 23 例——
存档三态、扁平化防重复消费、`table_data` 别名折叠、超限（含「高重复表体在
小上限下仍能落下」= 压缩判定）、gzip 魔数与明文宽容读、路径净化、list 型
表体往返保真、落盘早于 chunk 读回失败、回放后 `table_objects_handled=True`、
**无存档 + 有表格 chunk → 拒绝（不调 extract / 不调 `_merge_table_objects`）**、
无存档 + 纯文本 chunk → 照常跑、空存档不告警、回放真的走到
`_merge_table_objects`、TTL 回收三态（过期删 / 未过期留 / TTL≤0 关停）。

**镜像重建批次**：`Reference/Rag-anything` 改动 → atomic-rag 镜像（与
`jonex_core` 的 P2-a/b 同批）。

## §52 `2026-09-12` — 引用命中查询卡死对账循环（两段式 + 索引 + 超时兜底）

**现象**（线上 172.30.2.57，c31ac916 恢复重抽）：kb-service 日志在 `16:47:10`
之后**完全静默**——不再打 `对账→本体`，PG 停在 `ontology_status=extracting`、
`ontology_applied_schema_version=None`，Neo4j 该文档实体数恒定 25489（= 首轮
遗留，本次一个字未写：`delete_by_document` 排在卡点之后）。对账循环是**串行**
的，卡在一个文档上就把整个对账（本体收尾 + PARSING 超时巡检 + OpenKB 编译
巡检）一起停摆十几分钟。

**为什么没有异常日志**：卡在 `await`——既不抛异常（不走内层 `except` → 不触发
「引用关系降级」），也不返回（不打 `Ontology written`），两条日志路径都不触发。

**根因**：`_write_graph_once` 第一步 `find_reference_hits` 的 Cypher 把两种命中
写在同一条 OR 谓词里：

```cypher
MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
WHERE coalesce(e.stub,false)=false
  AND (e.canonical_name=v OR v IN coalesce(e.aliases,[]))
```

- OR 让 planner 无法下推索引，只能按 `(tenant_id, kb_id)` 前缀取该 KB 全部节点
  再逐行过滤——而唯一约束的复合索引是
  `(tenant_id, kb_id, entity_type, canonical_name)`，查询不带 `entity_type`，
  前缀在第 3 列断裂，`canonical_name` 用不上索引；
- `v IN e.aliases` 是列表属性，Neo4j 常规索引不支持列表成员查找，无法索引化；
- 复杂度 = 候选值数 × KB 节点数：候选上限 3000（`_REF_CANDIDATE_CAP`），线上该
  KB 已 5 万节点 → ≈ **1.5 亿次属性比较**，单条 Cypher 不分批、无服务端超时。

实测：`SHOW TRANSACTIONS` 里该事务 `elapsed=PT11M32S, status=Running`，Neo4j
容器 CPU **102%**、kb-service 容器 **0.14%**（纯 await 干等）。**候选数上限不是
有效保护**——1.5 亿次已足够卡死，且节点数是乘数，KB 越大越糟。

**修法**（三处，AND 关系而非 OR——只改 Cypher 不加索引无效）：

| 位置 | 改动 |
|------|------|
| `jonex_core/common/neo4j_client.py` | `ensure_ontology_schema` 在唯一约束之后补 `CREATE INDEX ont_entity_name IF NOT EXISTS FOR (e:OntologyEntity) ON (e.tenant_id, e.kb_id, e.canonical_name)`——与查询谓词同序，使 `canonical_name` 等值命中可走 seek |
| `capabilities/knowledge_base/repository/ontology_graph_repository.py` | `find_reference_hits` 拆两段：① `MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, canonical_name:v})` 属性映射写法（planner 100% 走索引）；② **只对第 ① 段未命中的剩余值**扫 aliases。第 ① 段全命中则完全不发第 ② 段。精确命中优先于 aliases 命中的原语义由分段天然保证 |
| `capabilities/knowledge_base/services/reconciliation_service.py` | `_write_graph_once` 给 `find_reference_hits` 套 `asyncio.wait_for`：**`ONTOLOGY_REF_HITS_TIMEOUT_SEC`（默认 30s，<=0 关）**。超时 = 放弃引用关系 → 继续 `delete_by_document` + 写图 → 文档正常 READY（内层 try 本就允许降级，只丢一批增强边）。wait_for 取消 await 会关闭该 driver 连接 → 服务端那条长跑 Cypher 一并终止，不留空转 |

语义要点：**加超时不等于「快速失败」，而是「降级成功」**——被降级的只有引用
关系（图谱展示的增强特性），实体与关系照写、文档照常收尾。

**订正**：本文件 §「表格对象 upsert 键」一节把这一步记成「实体全部写图后批量查
引用命中」，有误。实为 **delete 之前**（`_write_graph_once` 内：先 `find_reference_hits`
→ 再 `delete_by_document` → 再 merge），是有意设计，命中口径 = 本文档写入前的
KB 图。以代码为准。

**配置**：`ONTOLOGY_REF_HITS_TIMEOUT_SEC`（kb-service 侧，默认 30）。


## §53 `2026-09-16` — VLM 计量 scene 区分：视觉调用从 `raganything_ingest` 单列 `raganything_vlm`

**背景**：llm-gateway **不识别「VLM」这一类别**——它只看请求路径（`/v1/chat/completions`
vs `/v1/embeddings`）与调用方注入的 `X-Jonex-Scene` 头，不解析 body 里有无 `image_url`。
而 raganything 侧 `_build_metering_headers` 对所有模型类型（llm/vlm/embedding）
**硬编码** `X-Jonex-Scene: raganything_ingest`，于是 VLM 调用与文本抽取在 gateway 侧完全同键。

**两个后果**：

1. **计量分不出 VLM 消耗**——`metering.llm_usage_log` 与 Redis 维度 key
   `llm:usage:{tenant}:{scene}:{model}:{day}` 的 scene/model 双同，直接合并计数；
2. **VLM 被连带注入 `thinking.disabled`**——`LLMGW_DISABLE_THINKING_MODELS`（含
   `deepseek-v4-flash-202605`）与 `LLMGW_DISABLE_THINKING_SCENES`（含
   `raganything_ingest`）双命中，`_maybe_disable_thinking` 即注入。当前与业务侧
   `RAG_VLM_REASONING_EFFORT=none` 冗余但一致；**隐患**是将来想把 VLM 改回开思考
   做高精度 OCR 时，网关注入会顶掉它（业务侧不显式传 `thinking`，网关就会注入）。

**修法**（两处实现 + 测试）：

| 位置 | 改动 |
|------|------|
| `raganything/service/model_factory.py` | `_build_metering_headers` 增 `scene` 参数（默认 `raganything_ingest`，向后兼容）；`_bind_model` 按 `prefix` 取值：`prefix == "vlm"` → `raganything_vlm`，其余（llm/embedding）维持 `raganything_ingest` |
| `raganything/service/jonex_metering_ctx.py` | `build_ingest_headers()` **不再输出 `X-Jonex-Scene`**，只保留 tenant/kb/doc/trace |
| `tests/test_service/test_model_factory.py` | 新增 `TestMeteringScene`：三种 prefix 的 scene 断言 + overlay 无 scene + 端到端 driver 请求头断言 |

**为什么必须同时去掉 overlay 里的 scene**：driver 的 header 合成顺序是
`{**spec.extra_headers, **_build_ingest_h()}`（`models/drivers/openai.py:49-55`、
`anthropic.py:151-158`），overlay 在**后**——若它仍输出 scene，会把模型级的
`raganything_vlm` 覆盖回单一值，只在 model_factory 里改 prefix 无效。
scene 属**模型级身份**，任务级 contextvar 只该负责 doc_id/trace_id 这类**任务维度**。

**升级重放**：全局搜索 `[jonex]` 定位 `model_factory.py` 的 `_build_metering_headers`
（含 `scene` 参数与 `_bind_model` 内 prefix 判定）、`jonex_metering_ctx.build_ingest_headers`。

**配置**：无需改 `deploy/.env`——`raganything_vlm` 不在 `LLMGW_DISABLE_THINKING_SCENES`
中，VLM 从此不再被网关注入 `thinking.disabled`，关思考完全由业务侧
`RAG_VLM_REASONING_EFFORT` 决定（要恢复网关兜底，把 `raganything_vlm` 加进该变量即可）。

**测试**：新增 `tests/unit/test_reference_hits_two_stage.py` 6 例（① 段属性映射
形态且无 OR/aliases、② 段只收未命中值、全命中不发 ② 段、精确不被 aliases 覆盖、
空候选短路、stub 过滤两段保留）；`tests/unit/test_ontology_graph_rewrite_retry.py`
追加 2 例（超时后 delete + merge 照常走完、超时配置读取与脏值回落）。

**镜像重建批次**：`jonex_core`（索引）+ `capabilities/knowledge_base`（两段式 +
超时）→ **knowledge-base 镜像**。


## §54 `2026-09-20` — 视频解析接入 MinerU（video_analysis backend）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/video_analysis/backends/mineru.py` | 新增 | `MineruVideoBackend`（`binding="mineru"`）+ `map_video_result()` |
| `raganything/video_analysis/backends/__init__.py` | 新增一行 import | 触发 `@register_video_analysis_backend` |
| `raganything/processor_builder.py` | `_create_video` | `default_backend` 改读 `config.video_analysis_binding`；按 binding 入池 |
| `raganything/video_processor.py` | `_select_backend` | 路由：per-item 标记 > 全局默认 > 内置 local |
| `raganything/config.py` | `video_analysis_binding` | docstring 补 `"mineru"` |
| `config/presets/video_mineru.yaml` | 新增 | MinerU 视频档位 |

**要点**：

- HTTP 传输**复用 `MineruSelfHostParser`** 的标准库实现（multipart / JSON / 轮询），
  只覆盖 `base_url` 与轮询参数；不引入第三方 HTTP 库。
- 端点变量 `MINERU_VIDEO_BASE_URL`（回退 `MINERU_SELFHOST_BASE_URL`）——独立变量
  是因为文档解析与视频解析可能指向不同实例。
- 字段映射：`summary←summary.overview`、`scenes←chapters[]`（下游只读
  `description`/`start_time`/`end_time`）、`tags←topics`（包装）、
  `raw_json` 无损兜底；`segments`/`slides`/`keyframe_ocr` 仅存 `raw_json`。
- **失败直接抛错不降级**（决策 D4）。
- 前提：目标 MinerU 实例须 `VIDEO_ENABLED=true`，且视频 ≤25 分钟 / ≤2GB。

**配置**：`VIDEO_ANALYSIS_BINDING=mineru`（此前该配置项全仓无人读，恒为 `local`）
与 `MINERU_VIDEO_*`；示例（全为注释，不改默认行为）见 `deploy/.env.example` §18
「VLM / ASR 连接信息」段。档位 preset：`config/presets/video_mineru.yaml`。

**测试**：新增 `tests/test_video_analysis/`——`test_mineru_backend.py` 28 例
（产物映射 / 端点解析与回退 / 失败不降级 / HTTP 契约）、
`test_backend_selection.py` 12 例（`_select_backend` 路由 + `_create_video` 装配）。
回归：全量 `pytest -q -m "not integration and not cos and not vlm"` 1126 passed /
19 failed；19 例失败在改动前基线（`a1ddf4a4`）上逐一复现，**无新增失败**。

**镜像重建批次**：`raganything` 源码变更 → **atomic-rag 镜像**。

### §54.1 `2026-09-20` — 三类来源聚合进 scenes 并区分类型（chapter / slide / asr）

承接 §54：原来 `scenes` 只取 `chapters[]`，slide 与 ASR 的内容全部丢弃（只躺在
`raw_json` 里）。本改把三类都聚合进 `scenes`，并让来源一路带到 chunk 的 `ctype`，
使检索侧能区分。

**动因（实测数据）**：23 分钟财报视频 `chapters=10 / slides=19 / segments=646`。
slides 是 OCR 原文、**含 chapters 没有的精确数字**（营收 328.5亿 / 1864.40亿 …），
对财务类问答价值更高；但 646 条 speech 段若逐条入 scenes 会变成 646 个 chunk。

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/video_analysis/backends/mineru.py` | `map_video_result` + 三个 `_*_to_scenes` | chapters→`chapter`（现有逻辑）、slides→`slide`（带 `slide_page`）、`segments[source=speech]` **按 `chapter_id` 归并**→`asr`；结果按 `start_time` 排序 |
| `raganything/video_processor.py` | `_analyze_via_mps` | synthetic segment 增 `source_type`（缺省 `chapter`，含无 scenes 的兜底单段） |
| `raganything/pipeline/stages.py` | `_SCENE_SOURCE_CTYPE` + `_collect_multimodal_chunks` | 段 chunk 的 `ctype` 由 `source_type` 决定：`chapter→video_chapter` / `slide→video_slide` / `asr→audio_segment` |
| `jonex_core/common/ontology_llm.py` | `answer_from_chunks` 标签 | 三个新 ctype 各给中文标签，作答时能区分来源 |

**要点**：

- **只取 `segments[source=speech]`**：`source=slide` 的段与 `slides[]` 重复（实测那条
  视频 19 段全是 slide 源）；`source=visual` 属第三类，本期不处理（只留 `raw_json`）。
- **归并是必需的**：646 段按 `chapter_id` 归并后约 10 条，与 chapters 同量级；
  无 `chapter_id` 的散段归为**一组**兜底，不逐条散落。
- **为什么复用 `ctype` 而不新开键**：`ctype` 是既有的、检索侧**已在消费**的类型通道
  （`search_service` 的双路配额/低质过滤/主体分组、`ontology_llm` 作答标签）。
  新取值在配额逻辑里落 `others` 分支，与原来的 `audio_segment` 同路，**无兼容风险**。
  另开新键会重蹈 `speaker_labels` / `group_summary` / `relative_position` 的覆辙
  —— 那几个字段只有写入、全仓无下游消费。
- ⚠️ **行为变更**：MPS 路径与历史数据的 scene 不带 `source_type` → 缺省 `chapter`
  → ctype 由 `audio_segment` 变为 `video_chapter`。已入库的旧 chunk 不变（新旧并存）。
  另一处影响：`search_service` 按 `(doc_id, chunk_type)` 分组取代表，同一视频文档
  现最多出 3 个代表（原 1 个），引用更全但可能挤占其它文档。

**测试**：`test_mineru_backend.py` +8 例（三类聚合/去重/排序/兜底）、
`test_backend_selection.py` +3 例（`source_type` 透传与缺省）、
`test_push_chunks_stage.py` +3 例（ctype 映射与兜底）。受影响既有用例 2 条按新行为更新。

**镜像重建批次**：同 §54 —— `raganything` 与 `jonex_core` 变更 → **atomic-rag 镜像**。


## §55 `2026-09-24` — 文档/图片/文本档位解析器切到内网自建（`mineru_online` → `mineru_selfhost`）

**动因**：把平台 MinerU 解析整体指向 jonex-gpu `127.0.0.1:20490`（MinerU-master fork），
文档/图片/文本不再走 mineru.net 云。此前只有视频档（`video_mineru.yaml`）接了内网自建，
文档类档位仍钉死 `mineru_online`。

**为什么必须改 preset 而不是只改 env**：preset 的 `config.parser` **优先于**全局 `RAG_PARSER`
兜底（`lightrag_adapter_v2.py` 只在 preset 未指定时读 `RAG_PARSER`），只改 env 不生效。

| 文件 | 位置 | 说明 |
|------|------|------|
| `config/presets/document_parse.yaml` | `config.parser` | `mineru_online` → `mineru_selfhost`（端点由 env 提供） |
| `config/presets/image_parse.yaml` | `config.parser` | 同上 |
| `config/presets/text_parse.yaml` | `config.parser` | 同上（仅作非 `.txt/.md` 格式兜底时生效） |

**配套（主仓侧，不在本目录）**：`deploy/.env`（gitignored；服务器为
`/home/app/jonex-platform/deploy/.env`）三项 —— `RAG_PARSER=mineru_selfhost`、
`MINERU_SELFHOST_BASE_URL=http://127.0.0.1:20490`（改前是**注释状态且端口是坏的 `:8000`**）、
`MINERU_SELFHOST_RETURN_IMAGES=true`（document 档 `modalities` 含 image，不拉图会降级）。
另同步 `deploy/.env.example` / `.env.local.example`，防「下次更新被模板覆盖」。

**要点**：

- 端点与 backend 一律由 env 提供，preset 不写死（与 §54 的 `video_mineru.yaml` 同约定）。
- ⚠️ **解析质量/吞吐档位变更有意为之**：从云端 Precision Extract（`mineru_model_version: vlm`）
  换成共享 5090 上的 pipeline 档（服务端并发上限 3）。

**镜像重建批次**：preset 是**烤进镜像**的（`COPY Reference/Rag-anything/ /opt/raganything/`，
无挂载）→ 必须 `docker compose build atomic-rag` 后 `--force-recreate` 容器，**只重启无效**。

## §56 `2026-10-08` — 视频后端判据从「后端池非空」改为「实际选中的后端」（线上：绑定 mineru 却报 MPS 不可用）

**现象**（线上，`客户环境变量文件`）：该画像 `MPS_ENABLED=false`、
`VIDEO_ANALYSIS_BINDING=mineru`（`MINERU_VIDEO_BASE_URL=http://127.0.0.1:20490`），
但上传视频后文档 chunk 正文只有一句：

> 该视频的转写已跳过：MPS 视频分析需要 COS 存储的 URL，当前为本地存储（无 mps_video_url），MPS 不可用。

即**一次转写都没发生**，且报的是一个该 KB 根本没配的后端（用户原话：「我都没有使用 MPS 视频器」）。

**成因**：`service/task_manager.py::_textualize_multimodal` 的判据是
`if _backends and not mps_url:`，旁边注释写成「`_video_backends` 非空 = MPS 路径
（MPS_ENABLED=true）」——并把 `_select_backend` 的返回值模型记成「只有 `"mps"` 或 `None`，
无 local 兜底」。

这个假设在 `video_analysis_binding` 还是死配置（恒为 `local`）时成立：那时非空池里
只可能是 `mps`。**绑定接活之后 `mineru` 同样入池**（`processor_builder.py:147-167`），
「池非空」就再≠「走 MPS」，于是 mineru 绑定的视频被这句粗判据拦在 MPS 短路口上，
写得像 MPS 配置问题、实际是判据过期。

**修法**：按 `_select_backend`（与 `generate_description_only` 内部**同一个函数**）
判「这次到底会走哪个后端」：

```python
_backends = getattr(proc, "_video_backends", None) or {}
_selected = proc._select_backend(modal_content)      # item 现状 → 实际后端
_use_mps = _selected == "mps" or bool(mps_url and "mps" in _backends)
if _use_mps and not mps_url:      # 绑定 mps 但视频在本地存储 → 短路（行为不变）
    body = "…MPS 视频分析需要 COS 存储的 URL…"
    self._record_multimodal_warning(task, "video_mps_no_cos_url", body)
else:
    if _use_mps:
        modal_content["mps_video_url"] = mps_url   # 仅 MPS 路径注入
    elif "mps" in _backends:
        logger.info("[jonex] 视频未走 MPS（无 COS URL 且未绑定 mps）→ 内置本地管线")
    desc, _entity = await proc.generate_description_only(...)
```

两种 COS URL 来源都要认：`item["mps_video_url"]`（外部直调 / KB 选了 `video_mps`）
与 `task.mps_video_url`（COS 存储时平台侧 FetchObject 设置，`:1361-1369`）——
后者正是 `video_mps` 预设 + COS 存储那条**必须继续走 MPS** 的老路径。

| 场景 | 池 | 绑定 | task COS URL | 改前 | 改后 |
|------|----|------|--------------|------|------|
| **线上**：绑定 mineru、本地存储 | `{mineru}` | mineru | 无 | ❌ 跳过（假 MPS 消息） | ✅ 调 mineru 后端转写 |
| 绑定 mineru + COS 存储 | `{mineru}` | mineru | 有 | ❌ 跳过 | ✅ 调 mineru，**不**注入 URL（注入会被当本地路径 → `FileNotFoundError`） |
| `video_mps` 预设 + COS 存储 | `{mps}` | local | 有 | ✅ 走 MPS | ✅ 走 MPS（不变） |
| 绑定 mps + 本地存储 | `{mps}` | mps | 无 | ✅ 短路 + 占位 | ✅ 短路 + 占位（不变） |
| 池空（默认画像） | `{}` | local | 无 | ✅ 本地管线 | ✅ 本地管线（不变） |
| MPS_ENABLED=true 但绑定 local、本地存储 | `{mps}` | local | 无 | ⚠️ 短路 + MPS 占位 | ⚠️ **改为**回落内置本地管线 + info 日志 |

最后一行是本次**唯一的行为变更**：MPS 全局开着、但这条视频既没有 COS URL 又没绑定 mps 时，
改前会写「MPS 不可用」占位，改后按 `_select_backend` 自身的契约（`video_processor.py:85-86`：
「`local` 与「未入池」都返回 `None`，走内置本地 VLM 管线」）回落本地管线，并留一条 info。
理由：占位是把「这条视频没转写」伪装成「MPS 配置问题」，而本地管线至少能给出正文。

**闸门**：`Reference/Rag-anything/tests/test_video_analysis/test_video_parser_routing.py`
新增 `TestMpsShortCircuitGate`
（6 例，`_VideoProcStub._select_backend = VideoModalProcessor._select_backend`
用**真实路由逻辑** + 假后端调用），改前 3 红、改后全绿。
另补 `TestSelectBackendRouting` 两例固化 `_select_backend` 在非 MPS 池 / mps 绑定无 URL 下的返回值。

### 为什么「同一个 preset、同一个视频，本体库能解析、jonex_wiki 不行」

线上反馈的这条现象**不是玄学**，它精确对应代码里的两条平行实现：

```
LLM_WIKI_TYPES = frozenset({"openkb", "jonex_wiki"})      # kb_type_service.py:17
      ↓ capabilities/knowledge_base/services/document_service.py:626
execution_mode = "parse_only" if kb_type in LLM_WIKI_TYPES else "full"
      ↓
parse_only（jonex_wiki/openkb）        full（本体 / ontology 等）
task_manager.py:2417 _run_parse_only    stage pipeline
  → :2989 _textualize_multimodal          → stages.py:595 MultimodalStage._process_one
  → ❌ 有「后端池非空 = MPS」的粗判据      → ✅ 只做「有 COS URL 就注入」、无条件调 processor
```

preset 只决定**入池哪些后端**，两条链拿到的池完全一样（`video_full_pipeline`
不设 `video_analysis_binding`，两边都吃 env 的 `VIDEO_ANALYSIS_BINDING=mineru`）；
差别纯粹在**谁去消费这个池**。`_video_backends` 全仓只出现在 `video_processor.py`
与 `task_manager.py` 两处 —— `stages.py` 里根本没有，所以 full 链一直是对的。

### 方案 A：抽出共享决策函数（`route_video`）

同一段判断存在两份实现，只改一份就会变成「按 KB 类型分裂的行为差异」，
而且 preset / env / 镜像全都一样、极难排查。故把判据抽到
`video_processor.py`（它本来就拥有 `_select_backend`），两条链都调它：

```python
class VideoRouting(NamedTuple):
    backend: Optional[str]   # _select_backend 的结果；None = 内置本地管线
    use_mps: bool            # 这条视频最终会不会走 MPS
    needs_cos_url: bool      # 会走 MPS 但拿不到 COS URL → 调用方应提前短路

def route_video(processor, modal_content, mps_url) -> VideoRouting:
    backends = getattr(processor, "_video_backends", None) or {}
    select = getattr(processor, "_select_backend", None)
    backend = select(modal_content) if callable(select) else None
    use_mps = backend == "mps" or bool(mps_url and "mps" in backends)
    return VideoRouting(backend, use_mps, use_mps and not mps_url)
```

- `pipeline/stages.py`（full 链）：`if route_video(processor, item, mps_url).use_mps: 注入`
- `service/task_manager.py`（parse_only 链）：`_routing.needs_cos_url` → 短路写占位；
  `_routing.use_mps` → 注入；否则原样交给后端

顺带收敛掉一处无效动作：full 链原先是「有 URL 就注入」，**不看池里有没有 mps**；
现在只在真会走 MPS 时注入（`mps` 未入池时那次注入本来就是空动作，`_select_backend`
还会就此告警）。

**两链一致闸门**（同文件，`TestTwoChainsAgree`，共 4 例 + 表驱动 6 档）：
把 **full 链（`MultimodalStage.execute`，用假 ctx/services 驱动）** 与
**parse_only 链（`_textualize_multimodal`）** 在同一场景下各跑一遍，断言
①注入的 COS URL 一致 ②实际分派的后端一致；`TestRouteVideoDecisionMatrix`
（8 档参数化）固化判定表；`test_neither_chain_reimplements_the_decision`
是结构守卫（两处调用点都必须出现 `route_video(`，且 full 链不得再直接读
`_video_backends`、parse_only 链不得再出现 `and not mps_url`）。
**变异验证**：把 full 链临时退回旧写法 → 行为一致性用例与结构守卫**同时报红**，
还原后 36 passed。

> ⚠ **闸门搬家（2026-10-08）**：该文件原在 `tests/unit/`，而根 `.gitignore:84`
> 有一条 `/tests/` —— 新增文件默认被忽略，`git ls-files` 证明它**从未被跟踪**
> （计划文档里那句 `git add tests/unit/test_video_parser_routing.py` 静默没生效）。
> 测试进不了版本库就不算闸门，故迁到**被跟踪**的
> `Reference/Rag-anything/tests/test_video_analysis/`，与同样测视频后端的
> `test_backend_selection.py` 同目录（那个测 `_select_backend` 与池装配，
> 这个测两条链谁按它行动）。**往根 `tests/unit/` 加新测试要记得 `git add -f`。**

### 保留的不对称（有意，非漂移）与另一处新发现

- `needs_cos_url` 时**只有 parse_only 链**提前短路：它加这道闸是为了不白等 MPS
  长超时并留下可读占位；full 链没有这道闸、照调不误。两条链的**决策**仍一致，
  差异只在「拿不到 URL 时谁提前收手」。闸门里对此有显式断言，不是漏测。
- ⚠️ **新发现（本次未改，已用测试钉住现状）**：两条链调用
  `generate_description_only` 的**签名**本身还有一处分叉 ——
  `stages.py:619` 传 `item_info=item`（供 `ContextExtractor` 按 `page_idx`
  提取周边文本），`task_manager._textualize_multimodal` **不传** →
  parse_only 链（jonex_wiki / openkb）的多模态描述拿不到页面上下文。
  属**质量差异**而非崩溃，与本次修的 MPS 判据无关；改动它会改变喂给 VLM 的
  prompt 内容（模型输出会变），故未擅动。见
  `test_item_info_divergence_is_known_and_pinned`。

**⚠️ 排查时顺带发现**：排查现场那份 env 文件里的
`MINERU_VIDEO_BASE_URL` 与仓库文档给出的示例值不一致（示例见
`deploy/mineru-video/README.md` 与 `deploy/.env.example`）。**本处未改**（可能确为独立实例）：若该值
是笔误，即使本 bug 修好，MinerU 调用仍会连不上（`_resolve_base_url` 取到非空值就不会回退
`MINERU_SELFHOST_BASE_URL`）。

**镜像重建批次**：`raganything/service/task_manager.py`、`pipeline/stages.py`、
`video_processor.py` 都烤进 `atomic-rag` 镜像（无挂载）→ 必须
`docker compose build atomic-rag` + `--force-recreate`，**只重启无效**。
改后**存量文档需 reparse**（旧占位不会被自动改写；好在短路分支 `_cacheable=False`，
占位从未进 `parsed/{doc}/transcript.json`，reparse 会真正重试转写）。

## §57 `2026-10-09` — 转写失败原因分类器认不出真实传输层：urllib/402 全塌缩成「转写服务不可用」（bd `jonex-platform-977`）

**现象**（线上 2026-10-08）：视频转写因上游 tokenhub 欠费失败，写进 `content.md`
的占位句却只有一句

> 该视频的转写失败（转写服务不可用），暂无可提取文本。

—— 上游明确回了 **402 Payment Required**，用户与运维看到的却是与「端口没开」「鉴权挂了」
完全相同的九个字，排查因此绕远路（用户原话：「转写失败原因分类器认不出 urllib/402」）。

**成因：两条通道的信息都在，但都没往上带。**

1. `parsers/mineru.py` 把传输层异常**一律重抛成 `RuntimeError`**，状态码只活在消息文本里：
   `except urllib.error.HTTPError: raise RuntimeError(f"… HTTP {exc.code}: {body}")`；
2. 更隐蔽的一条：`_poll_task` 判到 `state == "failed"` 时抛
   `RuntimeError(f"… {status.get('error') or status}")` —— **这类失败的 HTTP 是 200**，
   状态码只写在**任务体**的 `error` 文本里。**线上那次正是这一种。**

而分类器 `TaskManager._classify_transcribe_error` 只按**异常类型名**判定
（`"Timeout" in name` / `"ConnectError" in name` / `"Config"…"Error"`），
对 `RuntimeError` 拿到的信息是**零** → 400/401/402/404/429/5xx/连不上/对端掐断
**全部塌缩成同一句**。本地实测（复现表）：`HTTPError` 400/402/404/500、`URLError`、
`ConnectionRefusedError`、`RemoteDisconnected` **清一色**「转写服务不可用」。

**改法：由「拿得到状态码的那一层」打好粗粒度标签，分类器只读标签。**

- `parsers/mineru.py`：新增 `MineruTransportError` / `MineruTaskFailedError`，
  都是 **`RuntimeError` 子类**（现有调用方一律 `except RuntimeError` 收口，换基类会静默漏接），
  各带一个 `reason` 粗粒度串。落点四处：`_api_json_request` 的 HTTPError/URLError、
  `_read_json_response`（视频链走的就是这条：`_post_multipart` → 它）的同两处、
  以及 `_poll_task` 的 failed 分支（从任务体文本取状态码）。
- **另补一路兜底**：`ConnectionRefusedError`（`OSError`）与 `RemoteDisconnected`
  **不是** `URLError`（后者在**读完响应**时才抛），原先会裸着穿到调用方 ——
  现以 `except (OSError, http.client.HTTPException)` 收口。
- `video_analysis/backends/mineru.py::_submit`：那个「HTTP 400 → 追加 VIDEO_ENABLED 指引」
  的**重包**原先 `raise RuntimeError(...)`，会把 reason 丢掉 → 改为
  `raise MineruTransportError(…, reason=getattr(exc, "reason", None) or _REASON_REJECTED)`。
- `service/task_manager.py::_classify_transcribe_error`：**① 优先读 `exc.reason`**，
  ② 无标签的仍按类型名判定（httpx / asyncio 等其它生产者），③ 兜底「转写服务不可用」。

**任务体文本取码故意保守**：只认 `HTTP 402` / `402 Client Error` / `status: 502` 这类
HTTP 语义明确的写法与 `unauthorized` / `payment required` 等短语，
**不匹配裸三位数字** —— 否则正文里「第 402 条」会被误判成欠费（本仓知识库真有
《公司章程》这类文档）。认不出就兜底，宁可少说。

**安全约束（沿用 §55 同款规矩并加强）**：`reason` 会写进 `content.md` → 编译 → 索引 →
**可能出现在搜索回答里**，所以只准是粗粒度串；异常原文（含 COS 签名 URL / 内网域名）
只进日志。新增闸门测试 `test_reason_is_safe_for_content_md` 对**全部**产出的原因串断言
不含 `http` / `://` / `/v1/` / `:2049` / `127.0.0.1` / `sk-` / `Bearer` / `.com` / `.cn`
且长度 ≤ 20。

**同轮实测结果**（本地 `.venv-atomic-rag`，驱动真实传输层与真实轮询，不造异常）：

| 场景 | 改前 | 改后写进 `content.md` 的占位句 |
| --- | --- | --- |
| HTTP 402 | 转写服务不可用 | 该视频的转写失败（**转写服务额度不足**），… |
| HTTP 401 / 403 | 转写服务不可用 | （转写服务鉴权失败） |
| HTTP 400 | 转写服务不可用 | （转写服务拒绝请求） |
| HTTP 404 / 429 / 5xx | 转写服务不可用 | （端点不对 / 限流 / 内部错误） |
| 端口关闭 / 对端掐断 | 转写服务不可用 | （转写服务连不上） |
| **线上那次的真实形态（任务体 402）** | 转写服务不可用 | （**转写服务额度不足**） |

**验证**：新增 `tests/test_video_analysis/test_transcribe_error_classification.py`
（25 例，纯行为面、不引用新内部类型名）—— 改前 **20 failed / 5 passed**，
改后 **25 passed**；`tests/test_video_analysis/` 全量 **112 passed**；
Rag-anything 全量 **1197 passed / 25 failed**，那 25 条经 `git stash` 前后对比
**逐条同名同数、零差异**（预先存在，与本次无关）。
**变异验证**：① 分类器忽略 `reason` → 20 failed；② 去掉 402 映射 → 2 failed；
③ `_poll_task` 不带 reason → 7 failed；恢复后均转绿。

**镜像重建批次**：`raganything/parsers/mineru.py`、
`raganything/service/task_manager.py`、`raganything/video_analysis/backends/mineru.py`
都烤进 `atomic-rag` 镜像（无挂载）→ 必须 `docker compose build atomic-rag` +
`--force-recreate`，**只重启无效**。属**纯改善诊断信息**的改动：
存量文档不必 reparse（成功的转写结果不变，只有失败占位句会变准）。
⚠ 上游 402 已另行解决，本条修的是**下次再出故障时能一眼看出是什么故障**。
