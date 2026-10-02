#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[jonex] 数据运营二期事件表回填（检索线四表 + 处理线一表）。

背景
----
数据运营二期把聚合口径从 `knowledge_search_history` 快照切到埋点事件表
（`deploy/postgres/migrations/008_search_events.sql`）。切源那一刻事件表是空的 ——
趋势图、构成、异常清单全部为 0，页面看起来是坏的。本脚本把存量数据折算成事件行
补进去，让「切源」对用户不可见。

两条线的幂等手法**不同**（别照抄）：

| 线 | 目标表 | 幂等键 | 唯一约束 | 幂等手段 |
| --- | --- | --- | --- | --- |
| 检索 | `search_request_event` / `search_step_event` / `search_hit_event` / `search_adoption_event` | `request_id = 'hist:{history.id}'` | **无**（D1 只有普通索引） | 先 `SELECT request_id LIKE 'hist:%'` 建 set，Python 侧跳过 |
| 处理 | `processing_task_event` | `processing_task_id = 'bf:{doc.id}:{task_type}:{generation}'` | `uq_pte_task` | `ON CONFLICT DO NOTHING` |

`hist:` / `bf:` 前缀是「这是回填行」的标记，`--force` 只删带前缀的行，绝不误伤线上埋点。

口径断层（**必须写进发布说明**）
------------------------------
`knowledge_search_history` 受 `MAX_HISTORY_PER_USER = 200` 的软删 trim + `is_deleted=0`
过滤，回填出的「历史请求数」**小于**当时的真实请求数；而事件表记的是真实次数。
所以切源那一刻 `total_requests` / `request_trend` 会**跳变**（历史按留存行、未来按真次数），
趋势图跨切换点不可比。本脚本只回填 `is_deleted = 0` 的行，保持「历史 = 一期口径」
的最小惊讶。

⚠️ 与 `capabilities/knowledge_base/services/search_event_writer.py` 的关系
----------------------------------------------------------------------
本文件**不 import `capabilities.*` 的任何东西**（`scripts/backfill_office_preview.py:136-140`
的先例：import capability 会连带拉起 openai 等重依赖，回填只需要 DB）。代价是
`_clip` / `_clip_identity` / `hash_query` / `ontology_object_id` / `_trim_detail`
在本文件与写入器里**各有一份本地副本**，口径必须一致 —— **改截断规则时务必同时改两处**
（`tests/unit/test_backfill_search_events.py` 有两条哈希口径一致性测试守着）。

唯一的**故意差异**是 `_trim_detail`：写入器按 stage 白名单硬过滤（它的输入形状确定），
本脚本不按白名单 —— 历史行的 detail 形状逐年演化，白名单会连带丢掉对账要用的计数键
（`total_hits` / `kb_ok` / `raw_recall_count`）。本脚本只做「丢大字段 + 裁数组 + 8KB 封顶」。

用法
----
    # 试跑：连库统计会写多少行，不落库
    python scripts/backfill_search_events.py --dry-run
    # 回填一个租户（两条线）
    python scripts/backfill_search_events.py --tenant tenant_jonex_demo
    # 只回填处理线，且只处理某个库
    python scripts/backfill_search_events.py --only processing --kb-id kb_xxx
    # 重跑（先删本脚本写过的行，再重写）
    python scripts/backfill_search_events.py --force

运行环境
--------
- 环境变量与平台进程一致（DB_HOST/DB_PORT/DB_USERNAME/DB_PASSWORD/DB_NAME）；
  默认自动加载仓库根 `.env.local`（`--env-file` 可换，`--no-env-file` 关闭）。
- ⚠️ **先核对时区**：`knowledge_documents.created_at/updated_at`
  是**无时区的本地时间**列（`TimestampMixin` 用 `datetime.now()`），而
  `processing_task_event.started_at/finished_at` 是 `TIMESTAMPTZ`。脚本用
  `AT TIME ZONE current_setting('TimeZone')` 让 **DB 自己**按它的时区把 naive 值折算成
  timestamptz，因此**不需要**脚本所在机器的时区与业务主机一致；但 DB 的
  `SHOW TimeZone` 必须与写入这些列的进程所在时区一致（同机部署下天然成立）。
  asyncpg 对 naive datetime 一律按 UTC 编码 —— 直接透传会差 8 小时，这是本脚本
  唯一一处必须借 SQL 完成的换算。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import bindparam, text

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("backfill_search_events")

LOADED_ENV_FILE = ""

# ── 幂等前缀（`--force` 只删带前缀的行）──
_HIST_PREFIX = "hist:"
_BF_PREFIX = "bf:"

# ── 列宽（与 008_search_events.sql 逐列对齐，与写入器同口径）──
_USER_ID_MAX = 128        # search_request_event.user_id VARCHAR(128)
_TITLE_MAX = 128
_SUMMARY_MAX = 2000
_STAGE_MAX = 32
_STATUS_MAX = 16
_ENTITY_TYPE_MAX = 64
_CANONICAL_NAME_MAX = 256
_KB_ID_MAX = 128
_TARGET_ID_MAX = 128
_REQUEST_ID_MAX = 64
_QUERY_HASH_MAX = 64
_ERROR_MAX_LEN = 1000     # 与 processing_task_repository._ERROR_MAX_LEN 一致

# detail 序列化后的单条上限与数组保留条数
_DETAIL_MAX_BYTES = 8 * 1024
_DETAIL_ARRAY_MAX = 20
# 数组型字段的逐项保留键；未列出的数组只留标量项
_ARRAY_ITEM_KEEP: dict[str, tuple[str, ...]] = {
    "hits": ("name", "type", "kb_id", "score"),      # 本体命中候选
    "recalls": ("doc_id", "chunk_index", "kb_id"),   # ⚠️ 必须丢 text（实测 50000 字符）
}

# ── 命中事件的 target_type（照抄写入器）──
TARGET_ONTOLOGY_INSTANCE = "ontology_instance"
TARGET_DOCUMENT = "document"

# ── 检索步骤常量（照抄 dtos/reasoning.py，脚本不能 import 它）──
STAGE_ONTOLOGY_MATCH = "ontology_match"
STATUS_DONE = "done"
STAGE_UNKNOWN = "unknown"

# ── 处理线取值（照抄 repository/processing_task_repository.py 与 models/document.py）──
TASK_TYPE_PARSE = "parse"
TASK_TYPE_EXTRACT = "extract"
TASK_TYPE_COMPILE = "compile"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"

_DOC_STATUS_READY = "ready"
_DOC_STATUS_FAILED = "failed"
# pending = 排队待处理（还没开工）、parsing/ingesting = 处理中 → 都不回填，
# 交给在线埋点；回填 running 只会造一条没有终态来收的僵尸行
_DOC_STATUS_IN_FLIGHT = frozenset({"pending", "parsing", "ingesting"})
_ONTOLOGY_STATUS_READY = "ready"
_ONTOLOGY_STATUS_FAILED = "failed"
_COMPILE_STATUS_COMPILED = "compiled"
_COMPILE_STATUS_FAILED = "failed"

# wiki 库类型（照抄 services/kb_type_service.py 的 LLM_WIKI_TYPES）
_LLM_WIKI_TYPES = frozenset({"openkb", "jonex_wiki"})

# 处理线每批 executemany 的行数（ON CONFLICT DO NOTHING 保证重跑安全）
_TASK_BATCH_SIZE = 200


def load_env_file(path: str) -> None:
    """加载 shell 风格 env 文件（已存在的环境变量不覆盖）。

    只认 KEY=VALUE；值两端的引号会被剥掉（.env.local 里有单引号包裹的 JSON）。
    """
    global LOADED_ENV_FILE
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    LOADED_ENV_FILE = str(p)


# 延迟 import：必须在 env 文件加载之后（settings 在 import 期读环境变量）
_get_db_session = None


def _db_session():
    """`jonex_core.common.database.get_db_session` 的延迟句柄。"""
    global _get_db_session
    if _get_db_session is None:
        from jonex_core.common.database import get_db_session

        _get_db_session = get_db_session
    return _get_db_session()


# ══════════════════════════════════════════════════════════
# 本地副本（口径必须与 services/search_event_writer.py 一致）
# ══════════════════════════════════════════════════════════


def hash_query(query: str | None) -> str | None:
    """query_hash：`sha256(strip + lower)`[:64]（写入器同口径）。

    ⚠️ **不要**抄 `knowledge_search_history.query_hash` 列 —— 那一列来自
    `models/search_history.build_query_hash`，用 `normalize_query` 只折叠空白、
    **不转小写**，两者对大小写不同的同一句话给出不同哈希。抄了会把「切源前后问过的
    同一句话」在 `top_questions` 里裂成两行。事件表语境一律以本函数为准，现算。
    """
    if not query:
        return None
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:_QUERY_HASH_MAX]


def ontology_object_id(tenant_id: str, kb_id: str, entity_type: str, canonical_name: str) -> str:
    """本体实例的展示用 id：`sha256(tenant|kb|entity_type|canonical_name)`[:32]。

    只作返回 DTO 的展示 id，**绝不作聚合 join 键**：改名会让历史行的
    target_id 指向一个不再存在的键，`active_entities` 系统性少算、
    `top_used_entities` 计数裂成两行。聚合按三元组 group by，target_id 只在返回时现算。
    """
    raw = f"{tenant_id}|{kb_id}|{entity_type}|{canonical_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _clip(value, limit: int):
    if value is None:
        return None
    return str(value)[:limit]


def _clip_identity(value: str | None, limit: int) -> str | None:
    """截断**标识类**字段：截断后加短哈希后缀，避免两个不同实体截成同一个值。

    表格行实体的 canonical_name 含主键，真实长度可超 256，不截断会触发 PG
    `StringDataRightTruncation` 导致**整批**命中事件插入失败；朴素截断又会让两个
    不同实体撞同一个 (entity_type, canonical_name) 被并成一条。
    """
    if value is None:
        return None
    if len(value) <= limit:
        return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    head = max(0, limit - 9)  # limit 含后缀长度：`#` + 8 位 = 9
    return f"{value[:head]}#{suffix}"


def _as_int(value):
    """把 duration_ms 之类的历史值收敛成 int（INTEGER 列不接受 float/str）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scalar_only(detail: dict) -> dict:
    """只留标量 + 一层 dict（丢弃数组与深层嵌套）—— 8KB 兜底用。"""
    out: dict = {}
    for key, value in (detail or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = {
                k: v for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
    return out


def _trim_item(item, keep: tuple[str, ...] | None) -> dict:
    if not isinstance(item, dict):
        return {"value": item}
    if keep is None:
        return _scalar_only(item)
    return {k: item.get(k) for k in keep if k in item}


def _trim_detail(detail: dict | None, stage: str | None = None) -> dict | None:
    """裁剪 detail，防单请求数十 KB 把事件表这一列撑爆。

    规则：**丢大字段 + 裁数组 + 8KB 封顶**。

    1. 标量 / 一层 dict（值仍是标量）原样保留；
    2. 数组最多 20 条，逐项按 `_ARRAY_ITEM_KEEP` 保留（`recalls` 丢 `text`），
       未列出的数组只留标量项；
    3. 序列化 > 8KB → 退化为「只留标量 + 一层 dict」；仍超 → 只留键名清单。

    ⚠️ **与写入器的故意差异**：这里**不按 stage 白名单硬过滤**。写入器的输入形状是
    确定的（在线链路当下产出的 detail），而历史行的 detail 形状逐年演化
    （`_hit_trace_of` 2026-09-23 才改成现在的键集）—— 白名单会连带丢掉对账要用的
    计数键（`total_hits` / `kb_ok` / `raw_recall_count`）。聚合侧读的是这些计数键，
    不是 detail 的形状，所以「多留几个键」是安全的，「少留计数键」不是。

    `stage` 参数保留仅为与写入器同签名，当前实现不使用它。
    """
    if not detail or not isinstance(detail, dict):
        return None

    trimmed: dict = {}
    for key, value in detail.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            trimmed[key] = value
        elif isinstance(value, dict):
            trimmed[key] = {
                k: v for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
        elif isinstance(value, list):
            keep = _ARRAY_ITEM_KEEP.get(key)
            items = []
            for item in value[:_DETAIL_ARRAY_MAX]:
                if isinstance(item, dict):
                    items.append(_trim_item(item, keep))
                elif isinstance(item, (str, int, float, bool)) or item is None:
                    items.append(item)
            if items:
                trimmed[key] = items

    # 8KB 兜底
    try:
        size = len(json.dumps(trimmed, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return {"truncated": True, "keys": list(trimmed.keys())}
    if size <= _DETAIL_MAX_BYTES:
        return trimmed or None
    fallback = _scalar_only(trimmed)
    try:
        size = len(json.dumps(fallback, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return {"truncated": True, "keys": list(trimmed.keys())}
    if size <= _DETAIL_MAX_BYTES:
        return fallback or None
    return {"truncated": True, "keys": list(trimmed.keys())}


def _dedup_rows(rows: list[dict], key_fields: tuple[str, ...]) -> list[dict]:
    """落库前按给定键去重（三张检索事件表**都没有唯一约束**，去重必须在写入前做）。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in rows:
        key = tuple(row.get(f) for f in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedup_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _get(ref, key: str):
    """references 元素可能是 dict 也可能是对象，两种都取。"""
    if isinstance(ref, dict):
        return ref.get(key)
    return getattr(ref, key, None)


# ══════════════════════════════════════════════════════════
# SQL
# ══════════════════════════════════════════════════════════

_HISTORY_SQL = """
    SELECT id, tenant_id, user_id, query, knowledge_base_id, domain_space_id,
           extra_metadata, references, reasoning, searched_at
      FROM knowledge_base.knowledge_search_history
     WHERE is_deleted = 0
"""

_HIST_REQUEST_SQL = """
    SELECT request_id
      FROM knowledge_base.search_request_event
     WHERE request_id LIKE 'hist:%'
"""

_KB_BY_SERVICE_SQL = """
    SELECT skb.kb_id
      FROM knowledge_base.service_knowledge_bases skb
      JOIN knowledge_base.knowledge_info ki
        ON ki.id = skb.kb_id AND ki.is_deleted = 0
     WHERE skb.service_id = :service_id
       AND skb.tenant_id = :tenant
       AND skb.is_deleted = 0
"""

_KB_BY_SPACE_SQL = """
    SELECT id
      FROM knowledge_base.knowledge_info
     WHERE space_id = :space_id
       AND tenant_id = :tenant
       AND is_deleted = 0
"""

# ⚠️ `created_at AT TIME ZONE current_setting('TimeZone')`：把**无时区的本地时间**
#    折算成 timestamptz，交给 DB 时钟/时区说话。直接把这俩 naive 值当参数传给
#    timestamptz 列会被 asyncpg 按 UTC 编码 → 差 8 小时（详见模块 docstring）。
_DOC_SQL = """
    SELECT d.id, d.tenant_id, d.knowledge_base_id, d.file_name,
           d.status, d.error_message,
           d.ontology_status, d.ontology_error,
           d.llm_wiki_compile_status, d.llm_wiki_compile_error,
           d.content_generation,
           d.created_at AT TIME ZONE current_setting('TimeZone') AS started_at,
           d.updated_at AT TIME ZONE current_setting('TimeZone') AS finished_at,
           ki.kb_type AS kb_type
      FROM knowledge_base.knowledge_documents d
      LEFT JOIN knowledge_base.knowledge_info ki
             ON ki.id = d.knowledge_base_id AND ki.is_deleted = 0
     WHERE d.is_deleted = 0
"""

_INSERT_REQUEST = text("""
    INSERT INTO knowledge_base.search_request_event
        (id, tenant_id, user_id, request_id, entry_service_id, domain_space_id,
         query, query_hash, knowledge_base_ids, received_at, is_deleted)
    VALUES
        (:id, :tenant_id, :user_id, :request_id, :entry_service_id, :domain_space_id,
         :query, :query_hash, CAST(:kb_ids AS JSONB), :received_at, 0)
""")

_INSERT_STEP = text("""
    INSERT INTO knowledge_base.search_step_event
        (id, tenant_id, request_id, stage, title, status, summary, detail,
         duration_ms, knowledge_base_ids, service_id, searched_at, is_deleted)
    VALUES
        (:id, :tenant_id, :request_id, :stage, :title, :status, :summary,
         CAST(:detail AS JSONB), :duration_ms, CAST(:kb_ids AS JSONB), :service_id,
         :searched_at, 0)
""")

_INSERT_HIT = text("""
    INSERT INTO knowledge_base.search_hit_event
        (id, tenant_id, request_id, target_type, target_id, entity_type,
         canonical_name, kb_id, hit_at, is_deleted)
    VALUES
        (:id, :tenant_id, :request_id, :target_type, :target_id, :entity_type,
         :canonical_name, :kb_id, :hit_at, 0)
""")

_INSERT_ADOPTION = text("""
    INSERT INTO knowledge_base.search_adoption_event
        (id, tenant_id, answer_id, request_id, target_type, target_id,
         entity_type, canonical_name, kb_id, adopted_at, is_deleted)
    VALUES
        (:id, :tenant_id, :answer_id, :request_id, :target_type, :target_id,
         :entity_type, :canonical_name, :kb_id, :adopted_at, 0)
""")

# `uq_pte_task UNIQUE (tenant_id, processing_task_id)` 是**真实唯一约束**
# （与检索四表不同），所以这里可以（且应该）用 ON CONFLICT DO NOTHING。
_INSERT_TASK = text("""
    INSERT INTO knowledge_base.processing_task_event
        (id, tenant_id, processing_task_id, asset_id, kb_id, task_type, status,
         error, started_at, finished_at, is_deleted)
    VALUES
        (:id, :tenant_id, :processing_task_id, :asset_id, :kb_id, :task_type,
         :status, :error, :started_at, :finished_at, 0)
    ON CONFLICT (tenant_id, processing_task_id) DO NOTHING
""")

_DELETE_HIST_REQUEST = text(
    "DELETE FROM knowledge_base.search_request_event WHERE request_id = ANY(:ids)"
)
_DELETE_HIST_STEP = text(
    "DELETE FROM knowledge_base.search_step_event WHERE request_id = ANY(:ids)"
)
_DELETE_HIST_HIT = text(
    "DELETE FROM knowledge_base.search_hit_event WHERE request_id = ANY(:ids)"
)
_DELETE_HIST_ADOPTION = text(
    "DELETE FROM knowledge_base.search_adoption_event WHERE request_id = ANY(:ids)"
)
_DELETE_TASKS = text("""
    DELETE FROM knowledge_base.processing_task_event
     WHERE processing_task_id LIKE 'bf:%'
       AND asset_id = ANY(:ids)
""")


def _with_expanding(stmt):
    return stmt.bindparams(bindparam("ids", expanding=True))


# ══════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════


async def _load_histories(session, args) -> list[dict]:
    clauses, params = [], {}
    if args.tenant:
        clauses.append("tenant_id = :tenant")
        params["tenant"] = args.tenant
    if args.kb_id:
        # history.knowledge_base_id 是那个 87% 为空串的列，只按它筛会几乎筛空 →
        # 再认一遍 extra_metadata.knowledge_base_ids（多库列表）
        clauses.append(
            "(knowledge_base_id = :kb_id"
            " OR (extra_metadata -> 'knowledge_base_ids') @> CAST(:kb_json AS JSONB))"
        )
        params["kb_id"] = args.kb_id
        params["kb_json"] = json.dumps([args.kb_id])
    sql = _HISTORY_SQL + (" AND " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY searched_at"
    if args.limit:
        sql += " LIMIT :limit"
        params["limit"] = args.limit
    rows = await session.execute(text(sql), params)
    return [dict(row._mapping) for row in rows.fetchall()]


async def _load_hist_request_ids(session) -> set[str]:
    """已回填的 request_id 全集（**不按租户筛**：request_id 全局唯一，多跑几次也安全）。"""
    rows = await session.execute(text(_HIST_REQUEST_SQL))
    return {r[0] for r in rows.fetchall() if r[0]}


async def _load_docs(session, args) -> list[dict]:
    clauses, params = [], {}
    if args.tenant:
        clauses.append("d.tenant_id = :tenant")
        params["tenant"] = args.tenant
    if args.kb_id:
        clauses.append("d.knowledge_base_id = :kb_id")
        params["kb_id"] = args.kb_id
    if args.doc_id:
        clauses.append("d.id IN :doc_ids")
        params["doc_ids"] = list(args.doc_id)
    sql = _DOC_SQL + (" AND " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY d.created_at"
    if args.limit:
        sql += " LIMIT :limit"
        params["limit"] = args.limit
    stmt = text(sql)
    if args.doc_id:
        stmt = stmt.bindparams(bindparam("doc_ids", expanding=True))
    rows = await session.execute(stmt, params)
    return [dict(row._mapping) for row in rows.fetchall()]


# ══════════════════════════════════════════════════════════
# 检索线：kb 归属解析
# ══════════════════════════════════════════════════════════


class _KbResolver:
    """解析「一次检索命中了哪些库」。

    优先级**照抄** `answer_feedback_service._resolve_kb_ids`：

    1. `extra_metadata.knowledge_base_ids`（多库列表，后端检索时写入）
    2. `knowledge_base_id` 单值（旧维度）
    3. `extra_metadata.domain_id`（≠ "all"）→ `service_knowledge_bases` 反查
    4. `domain_space_id` → 该空间下全部 kb
    5. 兜底：`references[].kb_id` 去重

    ⚠️ **不要**把 `knowledge_base_id` 列放第一位 —— 它正是那个 87% 为空串的列，
    放第一位会让「有值但只是其中一个库」的行拿不到真实多库列表。
    """

    def __init__(self, session, tenant_id: str):
        self._session = session
        self._tenant_id = tenant_id
        self._by_service: dict[str, list[str]] = {}
        self._by_space: dict[str, list[str]] = {}

    async def resolve(self, history: dict) -> list[str]:
        meta = history["extra_metadata"] or {}

        many = [
            _clip(k, _KB_ID_MAX)
            for k in (meta.get("knowledge_base_ids") or [])
            if k and str(k).strip()
        ]
        many = [v for v in many if v]
        if many:
            return _dedup_values(many)

        single = str(history["knowledge_base_id"] or "").strip()
        if single:
            return [_clip(single, _KB_ID_MAX)]

        domain_id = str(meta.get("domain_id") or "").strip()
        if domain_id and domain_id != "all":
            service_kbs = await self._service_kbs(domain_id)
            if service_kbs:
                return service_kbs

        space_id = space_of(history)
        if space_id:
            space_kbs = await self._space_kbs(space_id)
            if space_kbs:
                return space_kbs

        refs = [
            _clip(str(kb), _KB_ID_MAX)
            for kb in (_get(ref, "kb_id") for ref in (history["references"] or []))
            if kb and str(kb).strip()
        ]
        return _dedup_values([v for v in refs if v])

    async def _service_kbs(self, service_id: str) -> list[str]:
        if service_id not in self._by_service:
            rows = await self._session.execute(
                text(_KB_BY_SERVICE_SQL),
                {"service_id": service_id, "tenant": self._tenant_id},
            )
            self._by_service[service_id] = [
                _clip(str(r[0]), _KB_ID_MAX) for r in rows.fetchall() if r[0]
            ]
        return self._by_service[service_id]

    async def _space_kbs(self, space_id: str) -> list[str]:
        if space_id not in self._by_space:
            rows = await self._session.execute(
                text(_KB_BY_SPACE_SQL),
                {"space_id": space_id, "tenant": self._tenant_id},
            )
            self._by_space[space_id] = [
                _clip(str(r[0]), _KB_ID_MAX) for r in rows.fetchall() if r[0]
            ]
        return self._by_space[space_id]


def space_of(history: dict) -> str | None:
    """空间归属：历史列优先，`extra_metadata.domain_space_id` 兜底。"""
    space = str(history["domain_space_id"] or "").strip()
    if space:
        return space
    meta = history["extra_metadata"] or {}
    return str(meta.get("domain_space_id") or "").strip() or None


def entry_service_of(history: dict) -> str | None:
    """`entry_service_id = extra_metadata.domain_id`（≠ "all"）。

    比留空更有价值：回填行的服务归因与线上同口径。
    """
    meta = history["extra_metadata"] or {}
    domain_id = str(meta.get("domain_id") or "").strip()
    if domain_id and domain_id != "all":
        return domain_id
    return None


def _hist_request_id(history_id: str) -> str:
    return f"{_HIST_PREFIX}{history_id}"[:_REQUEST_ID_MAX]


def _steps_of(reasoning) -> list[dict]:
    """`reasoning.steps`（实测 96/96 行完整、零空）。

    ⚠️ **容忍不在 18 个 `STAGE_*` 里的历史 stage**（实测存在遗留的
    `retrieval_rerank`）：原样保留，不报错、不改写 —— 改写等于篡改历史事实。
    """
    if not isinstance(reasoning, dict):
        return []
    steps = reasoning.get("steps")
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict)]


def _build_search_rows(
    history: dict,
    kb_ids: list[str],
    entry_service_id: str | None,
    space_id: str | None,
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """一条检索历史 → (请求行, 步骤行[], 命中行[], 采用行[])。"""
    tenant_id = history["tenant_id"]
    request_id = _hist_request_id(history["id"])
    answer_id = _clip(history["id"], _REQUEST_ID_MAX)
    searched_at = history["searched_at"]          # TIMESTAMPTZ，已是 aware
    query = history["query"]
    steps = _steps_of(history["reasoning"])
    kb_json = json.dumps(kb_ids, ensure_ascii=False)
    service_id = _clip(entry_service_id, _KB_ID_MAX)

    request_row = {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "user_id": _clip(history["user_id"], _USER_ID_MAX),
        "request_id": request_id,
        "entry_service_id": service_id,
        "domain_space_id": _clip(space_id, _KB_ID_MAX),
        "query": query,
        "query_hash": hash_query(query),
        "kb_ids": kb_json,
        # ⚠️ 必须显式写 history.searched_at：缺它就落到列默认 NOW()，
        #    所有行堆在脚本运行那一刻，趋势图会多出一根假尖峰
        "received_at": searched_at,
    }

    step_rows: list[dict] = []
    for step in steps:
        stage = _clip(step.get("stage"), _STAGE_MAX) or STAGE_UNKNOWN
        detail = _trim_detail(step.get("detail"), stage)
        step_rows.append({
            "id": str(uuid4()),
            "tenant_id": tenant_id,
            "request_id": request_id,
            "stage": stage,
            "title": _clip(step.get("title"), _TITLE_MAX),
            "status": _clip(step.get("status"), _STATUS_MAX) or STATUS_DONE,
            "summary": _clip(step.get("summary"), _SUMMARY_MAX),
            "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
            "duration_ms": _as_int(step.get("duration_ms")),
            "kb_ids": kb_json,
            "service_id": service_id,
            "searched_at": searched_at,
        })
    step_rows = _dedup_rows(step_rows, ("stage", "title"))

    # 本体命中：只有 `steps[stage=ontology_match].detail["hits"]` 里有
    # `name`/`type`/`kb_id` 三者齐备的候选可回填。
    # ⚠️ 文档命中（`context_retrieve` 的 recalls）**不回填** —— 历史 steps 里的
    #    recalls 受 `ONTOLOGY_RAG_RECALL_DETAIL_ENABLED` 控制且只留 TOP-N，
    #    回填会得到一个偏低的假命中数；口径按「文档命中自二期起可用，历史为 0」。
    hit_rows: list[dict] = []
    for step in steps:
        if step.get("stage") != STAGE_ONTOLOGY_MATCH:
            continue
        detail = step.get("detail")
        hits = detail.get("hits") if isinstance(detail, dict) else None
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            name, entity_type, kb_id = hit.get("name"), hit.get("type"), hit.get("kb_id")
            if not name or not entity_type or not kb_id:
                continue
            hit_rows.append({
                "id": str(uuid4()),
                "tenant_id": tenant_id,
                "request_id": request_id,
                "target_type": TARGET_ONTOLOGY_INSTANCE,
                # object_id 用**未截断原文**算：截断只用于展示列
                "target_id": _clip(
                    ontology_object_id(tenant_id, str(kb_id), str(entity_type), str(name)),
                    _TARGET_ID_MAX,
                ),
                "entity_type": _clip(entity_type, _ENTITY_TYPE_MAX),
                "canonical_name": _clip_identity(str(name), _CANONICAL_NAME_MAX),
                "kb_id": _clip(kb_id, _KB_ID_MAX),
                "hit_at": searched_at,
            })
    hit_rows = _dedup_rows(hit_rows, ("target_type", "target_id"))

    # 采用事件：`references` → target_type='document'（实测 65 行 / 200 条明细）。
    # ⚠️ 不要从 references 造 `ontology_instance` 采用事件 —— references 里没有实体信息。
    adoption_rows: list[dict] = []
    for ref in history["references"] or []:
        doc_id = _get(ref, "doc_id")
        if not doc_id:
            continue
        adoption_rows.append({
            "id": str(uuid4()),
            "tenant_id": tenant_id,
            "answer_id": answer_id,
            "request_id": request_id,
            "target_type": TARGET_DOCUMENT,
            "target_id": _clip(doc_id, _TARGET_ID_MAX),
            "entity_type": None,
            "canonical_name": _clip(_get(ref, "file_name"), _CANONICAL_NAME_MAX),
            "kb_id": _clip(_get(ref, "kb_id"), _KB_ID_MAX),
            "adopted_at": searched_at,
        })
    adoption_rows = _dedup_rows(adoption_rows, ("target_type", "target_id"))

    return request_row, step_rows, hit_rows, adoption_rows


async def _write_search_rows(session, rows) -> dict[str, int]:
    request_row, step_rows, hit_rows, adoption_rows = rows
    await session.execute(_INSERT_REQUEST, [request_row])
    if step_rows:
        await session.execute(_INSERT_STEP, step_rows)
    if hit_rows:
        await session.execute(_INSERT_HIT, hit_rows)
    if adoption_rows:
        await session.execute(_INSERT_ADOPTION, adoption_rows)
    return {
        "request": 1,
        "step": len(step_rows),
        "hit": len(hit_rows),
        "adoption": len(adoption_rows),
    }


async def _backfill_search(args, stats: dict) -> None:
    async with _db_session() as session:
        histories = await _load_histories(session, args)
        existing = set() if args.force else await _load_hist_request_ids(session)

    logger.info(
        "检索线：扫描到检索历史 %d 行（租户=%s，limit=%s）",
        len(histories), args.tenant or "<全部>", args.limit or "不限",
    )
    todo = [h for h in histories if _hist_request_id(h["id"]) not in existing]
    stats["skipped"] += len(histories) - len(todo)
    if existing:
        logger.info("检索线：其中 %d 行已回填过（非 --force 跳过）", len(histories) - len(todo))

    if args.force and todo:
        ids = [_hist_request_id(h["id"]) for h in todo]
        if args.dry_run:
            logger.info("检索线：--dry-run，跳过清理 %d 行的旧回填数据", len(ids))
        else:
            async with _db_session() as session:
                for stmt in (
                    _DELETE_HIST_REQUEST, _DELETE_HIST_STEP,
                    _DELETE_HIST_HIT, _DELETE_HIST_ADOPTION,
                ):
                    await session.execute(_with_expanding(stmt), {"ids": ids})
            logger.info("检索线：已清理 %d 行的旧回填数据（--force）", len(ids))

    for index, history in enumerate(todo, start=1):
        try:
            async with _db_session() as session:
                resolver = _KbResolver(session, history["tenant_id"])
                kb_ids = await resolver.resolve(history)
                if not kb_ids:
                    # 空间过滤下这些行会被静默丢掉 → 必须让运维看见
                    stats["no_kb"] += 1
                if not _steps_of(history["reasoning"]):
                    stats["no_steps"] += 1
                rows = _build_search_rows(
                    history,
                    kb_ids=kb_ids,
                    entry_service_id=entry_service_of(history),
                    space_id=space_of(history),
                )
                if args.dry_run:
                    counted = {
                        "request": 1,
                        "step": len(rows[1]),
                        "hit": len(rows[2]),
                        "adoption": len(rows[3]),
                    }
                else:
                    counted = await _write_search_rows(session, rows)
            for key, value in counted.items():
                stats["rows"][key] = stats["rows"].get(key, 0) + value
            stats["done"] += 1
        except Exception as exc:  # noqa: BLE001 —— 单行失败不影响其余
            stats["failed"] += 1
            logger.warning("检索线异常: history=%s: %s", history["id"], exc, exc_info=True)
        if index % 20 == 0:
            logger.info("检索线进度 %d/%d", index, len(todo))

    logger.info(
        "检索线：请求 %d / 步骤 %d / 命中 %d / 采用 %d 行",
        stats["rows"].get("request", 0), stats["rows"].get("step", 0),
        stats["rows"].get("hit", 0), stats["rows"].get("adoption", 0),
    )


# ══════════════════════════════════════════════════════════
# 处理线
# ══════════════════════════════════════════════════════════


def _task_row(doc: dict, task_type: str, status: str, error: str | None) -> dict:
    """一行处理任务事件。

    ⚠️ `started_at`/`finished_at` 用 `updated_at - created_at` 的墙钟时间 ——
    它是「从上传到最后一次改动」的时长，**不是处理耗时**，`duration_ms` 会失真。
    这一点已写进前后端契约（前端按「有值但不可比」展示）。取「有值但失真」胜过全空，
是本期的取舍。

    `processing_task_id` 长度：`bf:`(3) + doc_id(≤64) + `:` + task_type(≤7) + `:` + 代次(≤4)
    ≈ 81 ≤ VARCHAR(128) ✔
    """
    generation = _as_int(doc["content_generation"]) or 0
    return {
        "id": str(uuid4()),
        "tenant_id": doc["tenant_id"],
        "processing_task_id": f"{_BF_PREFIX}{doc['id']}:{task_type}:{generation}",
        "asset_id": doc["id"],
        "kb_id": _clip(doc["knowledge_base_id"], _KB_ID_MAX),
        "task_type": task_type,
        "status": status,
        "error": _clip(error, _ERROR_MAX_LEN) or None,
        "started_at": doc["started_at"],
        "finished_at": doc["finished_at"],
    }


def _build_task_rows(doc: dict, stats: dict) -> list[dict]:
    """按下面的推导表，把文档当前状态折算成任务事件行。

    | 文档当前状态 | 事件 | status |
    | --- | --- | --- |
    | `status='ready'` | `parse` | success |
    | `status='failed'` | `parse` | failed |
    | `status IN (pending, parsing, ingesting)` | **不写** | — |
    | `ontology_status='ready'` 且 lightrag | `extract` | success |
    | `ontology_status='failed'` 且 lightrag | `extract` | failed |
    | `llm_wiki_compile_status='compiled'` | `compile` | success |
    | `llm_wiki_compile_status='failed'` | `compile` | failed |
    | `llm_wiki_compile_status IN (stale, compiling, NULL)` | **不写** | — |

    两条限定（每条都对应一个真实误判）：

    1. **`ontology_status` 必须加 kb_type 限定** —— wiki 文档的 `ontology_status`
       恒 `ready`（代码强制置位），不加限定会给每个 wiki 资产回填一条**假的抽取成功**；
    2. **kb_type 判据是 JOIN `knowledge_info.kb_type`**，不靠
       「`llm_wiki_compile_status` 是否非空」反推（该列在 lightrag 文档上恒 NULL、
       在 wiki 文档的 `stale` 路径下也可能为 NULL）。
    """
    rows: list[dict] = []

    status = str(doc["status"] or "").strip()
    if status in _DOC_STATUS_IN_FLIGHT:
        stats["in_flight"] += 1     # 进行中，交给在线埋点
    elif status == _DOC_STATUS_READY:
        rows.append(_task_row(doc, TASK_TYPE_PARSE, TASK_STATUS_SUCCESS, None))
    elif status == _DOC_STATUS_FAILED:
        rows.append(_task_row(doc, TASK_TYPE_PARSE, TASK_STATUS_FAILED, doc["error_message"]))

    kb_type = str(doc["kb_type"] or "").strip()
    if not kb_type:
        # knowledge_info 里查不到对应 KB 的孤儿文档 —— 判不出库类型，保守跳过 extract
        stats["unknown_kb_type"] += 1
    elif kb_type not in _LLM_WIKI_TYPES:
        # lightrag（标准检索型）：抽取阶段的终态看 ontology_status
        ontology_status = str(doc["ontology_status"] or "").strip()
        if ontology_status == _ONTOLOGY_STATUS_READY:
            rows.append(_task_row(doc, TASK_TYPE_EXTRACT, TASK_STATUS_SUCCESS, None))
        elif ontology_status == _ONTOLOGY_STATUS_FAILED:
            rows.append(
                _task_row(doc, TASK_TYPE_EXTRACT, TASK_STATUS_FAILED, doc["ontology_error"])
            )

    # 编译：不额外加 kb_type 限定 —— 该列在 lightrag 文档上恒 NULL，天然只覆盖 wiki
    compile_status = str(doc["llm_wiki_compile_status"] or "").strip()
    if compile_status == _COMPILE_STATUS_COMPILED:
        rows.append(_task_row(doc, TASK_TYPE_COMPILE, TASK_STATUS_SUCCESS, None))
    elif compile_status == _COMPILE_STATUS_FAILED:
        rows.append(
            _task_row(doc, TASK_TYPE_COMPILE, TASK_STATUS_FAILED, doc["llm_wiki_compile_error"])
        )
    # stale / compiling / NULL：不写（stale 是 reparse 打的「待重编」标记、不是终态）

    if not str(doc["knowledge_base_id"] or "").strip():
        stats["no_kb"] += 1
    return rows


async def _backfill_processing(args, stats: dict) -> None:
    async with _db_session() as session:
        docs = await _load_docs(session, args)
    logger.info(
        "处理线：扫描到文档 %d 篇（租户=%s，limit=%s）",
        len(docs), args.tenant or "<全部>", args.limit or "不限",
    )

    pending: list[dict] = []
    for doc in docs:
        pending.extend(_build_task_rows(doc, stats))
    stats["rows"]["task"] = len(pending)

    if args.dry_run:
        stats["done"] += len(docs)
        logger.info("处理线：dry-run 将写入 %d 行", len(pending))
        return

    if args.force and pending:
        # 只删**本次会重新写**的那些资产的行（`--limit` 场景下不能一把删光）
        asset_ids = sorted({row["asset_id"] for row in pending})
        async with _db_session() as session:
            await session.execute(_with_expanding(_DELETE_TASKS), {"ids": asset_ids})
        logger.info("处理线：已清理 %d 个资产的旧回填数据（--force）", len(asset_ids))

    written = 0
    for start in range(0, len(pending), _TASK_BATCH_SIZE):
        batch = pending[start:start + _TASK_BATCH_SIZE]
        try:
            async with _db_session() as session:
                await session.execute(_INSERT_TASK, batch)
            written += len(batch)
        except Exception as exc:  # noqa: BLE001 —— 单批失败不影响其余批
            stats["failed"] += len(batch)
            logger.warning(
                "处理线异常（第 %d 行起，%d 行）：%s", start, len(batch), exc, exc_info=True,
            )
    stats["rows"]["task"] = written
    stats["done"] += len(docs)
    logger.info("处理线：写入 %d 行", written)


# ══════════════════════════════════════════════════════════
# 编排
# ══════════════════════════════════════════════════════════


async def run(args: argparse.Namespace) -> int:
    if LOADED_ENV_FILE:
        logger.info("已加载环境变量文件: %s", LOADED_ENV_FILE)
    if args.dry_run:
        logger.info("dry-run：只统计，不落库")

    stats = {
        "done": 0,
        "skipped": 0,
        "failed": 0,
        "rows": {},
        "no_kb": 0,
        "no_steps": 0,
        "unknown_kb_type": 0,
        "in_flight": 0,
    }

    if args.only in ("all", "search"):
        await _backfill_search(args, stats)
    if args.only in ("all", "processing"):
        await _backfill_processing(args, stats)

    # 收尾：三类会被静默丢掉的行数必须让运维看见
    logger.info(
        "收尾：无 kb 归属 %d 行（空间过滤下会被静默丢掉）、reasoning.steps 为空 %d 行",
        stats["no_kb"], stats["no_steps"],
    )
    logger.info(
        "收尾：kb_type 未知（孤儿文档）%d 行、处理中跳过 %d 行",
        stats["unknown_kb_type"], stats["in_flight"],
    )
    logger.info(
        "回填结束: 成功 %d / 跳过 %d / 失败 %d",
        stats["done"], stats["skipped"], stats["failed"],
    )
    return 1 if stats["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="[jonex] 数据运营二期事件表回填（检索线四表 + 处理线一表）",
    )
    parser.add_argument("--tenant", default="", help="租户 id（缺省=全部租户）")
    parser.add_argument(
        "--kb-id", default="",
        help="知识库 id 过滤（检索线按 knowledge_base_ids 多库列表认，处理线按 documents.knowledge_base_id 认）",
    )
    parser.add_argument(
        "--doc-id", action="append", default=[],
        help="只处理指定文档 id（可重复），仅处理线生效",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="每条线最多处理多少行（0=不限；两条线各自计数）",
    )
    parser.add_argument(
        "--only", choices=("all", "search", "processing"), default="all",
        help="只回填其中一条线（缺省 all）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="先删本脚本写过的回填行（hist: / bf: 前缀）再重写",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计，不落库")
    parser.add_argument(
        "--env-file", default=str(_REPO_ROOT / ".env.local"),
        help="环境变量文件（缺省 仓库根/.env.local）",
    )
    parser.add_argument("--no-env-file", action="store_true", help="不加载任何 env 文件")
    args = parser.parse_args()

    if not args.no_env_file and args.env_file:
        load_env_file(args.env_file)

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
