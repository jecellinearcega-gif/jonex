# -*- coding: utf-8 -*-
"""[jonex] 表格对象抽取（Row-as-Object）
（docs/table-parsing-retrieval-governance-plan.md 增补二 §10.2 + 增补十表头判定分层架构）。

核心判断：表格是天然关系型数据，一行 = 一个对象实例，列名 = 属性名。
不需要 LLM 猜——确定性映射进本体，绕开通用文本抽取的三个结构性问题：

- P1 属性全空：列名→属性直接结构化写入 attributes；
- P2 实体硬截断：表格对象不参与通用抽取的截断，
  只受 `TABLE_OBJECT_MAX_ROWS`（默认 2000）每表行数上限；
- P3 同名合并污染：canonical_name 用「主键（+限定值）」在生成时保证 KB 内唯一，
  不参与 LightRAG 按名合并。

LLM 职责收缩到「每表一次」：表级判定（含表头行/说明行/层数 + 对象类型 +
主键列 + 限定列，增补十分层架构第 ② 层），结果按 `hash(表标题+列名签名)` 缓存；
列名→标准属性名映射（可选）。解析失败一律退回纯规则，不阻塞入库。

挂点：atomic-rag `_run_ontology_extraction`（ctx.content_list 可用时），
产出并入 `ontology_data`，由 kb-service reconciliation 写 Neo4j。
"""
import copy
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 主键词表（10.2.1 优先级 2）──
_PK_NAME_PATTERNS = (
    "名称", "简称", "品种", "代码", "编码", "编号", "ID", "客户",
    "机构", "字段", "错误码", "证券代码", "营业部",
)
# 度量值列（非主键候选）：纯数字/金额/百分比/日期
_METRIC_RE = re.compile(
    r"^\s*[\d,，.+\-]+(%|％|元|万元|亿元|手|克|千克|吨|％|%|\s)*\s*$"
)
# 分组关系类型（分组列值 → 对象实例）
_GROUP_RELATION_TYPE = "包含"
# [jonex] row-to-table：表归属关系复用 `包含`（与分组边同类型，下游按
# relation_type 分流的地方零改动）。表节点的 entity_type 用固定值 `Table`
# ——已核实后端 DTO/服务与前端均无 entity_type 白名单校验。
_TABLE_RELATION_TYPE = _GROUP_RELATION_TYPE
_TABLE_NODE_TYPE = "Table"
_TABLE_NODE_PREFIX = "表·"


def table_node_name(table_sig: str) -> str:
    """[jonex] row-to-table：表节点名 = ``表·{table_sig}``。

    **只用 `table_sig`，不带 caption**——确定性优先于可读性（2026-09-18 决策）。
    两条实测依据：

    1. **caption 不唯一**：本地 5 库 2192 条 caption 只去重出 103 个、79 个重复
       （最高 ``process_card_test_record`` 出现 926 次），还有文件名当表名的。
    2. **同一张表会出现带/不带 caption 两种条目**：实机重抽后实测 ``393a305d``
       与 ``d0b7e518`` 各自被拆成了两个节点（``2.1 金属氧化物避雷器（MOA）·393a305d``
       与 ``表·393a305d`` 并存）——同一张物理表，一次 caption 可用、一次不可用。
       按 caption 命名**必然分裂**，与 caption 质量无关。

    `table_sig` 是 header 的 md5 前 8 位，因此：同一张表切成多 chunk → 同 sig
    → 正确合并；header 不同的表 → 不同 sig → 不合并。可读性由行对象上的
    `table_sig` 属性反查（行锚点仍带 `table_sig`）。
    """
    return f"{_TABLE_NODE_PREFIX}{table_sig}"
# 多级表头拼接跳过的占位符与空值词表同源（``—`` 表示沿用上一级列名，
# ``-0-`` 等表示无值）——统一走 _NULL_TOKENS，见下。
# [jonex] §null-token 文档里的空值占位符：这些字面量表示「没有值」，必须按空串
# 处理，否则会被当成真实值。OFAC SDN 用 ``-0-``（sdn.md 实测 6593 行都含它），
# 污染链：对象名 ``173（…、-0-、CUBA）``、属性 ``Call_Sign: -0-``、多级表头
# ``Call_Sign--0-``、分组关系 54% 挂在该占位符下。
# 词表只收**无歧义**的写法——``NA``/``NONE``/``NaN`` 等可能是合法值（国别码、
# 化学式），不收。
_NULL_TOKENS = {
    "-0-", "--", "-", "—", "–", "/", "\\",
    "n/a", "null", "无", "未知", "空", "暂无",
}

# [jonex] P0-c（2026-09-13 线上 OOM 复盘）：`_compose_header` 表头推断失败时
# 的兜底列名 `col_0…col_N`。它不是真列名、零信息量，多级拼接时必须整段跳过
# ——否则规则表头退化后 `apply_header_verdict` 会把 `col_0` 与 LLM 判出的
# 真表头 `metric_id` 拼成 `col_0-metric_id`，属性名依旧不可用。
_COL_PLACEHOLDER_RE = re.compile(r"^col_\d+$")


def is_null_value(value: str) -> bool:
    """空值判定：空串/纯空白，或命中空值占位符词表（``-0-`` 等）。"""
    v = (value or "").strip()
    return not v or v.lower() in _NULL_TOKENS


def _cell(row: List[str], idx: int) -> str:
    """取单元格值，空值占位符归一为空串（越界同空）。"""
    if idx >= len(row):
        return ""
    v = (row[idx] or "").strip()
    return "" if is_null_value(v) else v


@dataclass
class TableVerdict:
    """表级判定结果（LLM 或规则 fallback）。"""

    header_row_indices: List[int] = field(default_factory=list)
    note_row_indices: List[int] = field(default_factory=list)
    levels: int = 0
    object_type: str = ""
    pk_columns: List[str] = field(default_factory=list)
    qualifier_columns: List[str] = field(default_factory=list)
    column_mapping: Dict[str, str] = field(default_factory=dict)  # 原列名 → 标准属性名
    source: str = "rules"  # llm / cache / rules


def table_cache_key(table_caption: str, column_list: str) -> str:
    """缓存键：表标题 + 列名签名（10.2.4 / 增补十第 ⑤ 层）。"""
    raw = f"{table_caption}||{column_list}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_metric(value: str) -> bool:
    """度量值判定（非主键候选）：纯数字/金额/百分比。"""
    if not value or not value.strip():
        return True  # 空值不算有效主键
    return bool(_METRIC_RE.match(value.strip()))


def _column_unique_ratio(rows: List[List[str]], col_idx: int) -> float:
    """某列非空值去重率（1.0 = 全不重复，适合做主键）。"""
    values = [_cell(row, col_idx) for row in rows]
    values = [v for v in values if v]
    if not values:
        return 0.0
    return len(set(values)) / len(values)


def _combined_unique_ratio(rows: List[List[str]], indices: List[int]) -> float:
    """多列组合去重率（全空行不计入分母）。"""
    keys = []
    for row in rows:
        parts = [_cell(row, i) for i in indices]
        if not any(parts):
            continue
        keys.append("|".join(parts))
    if not keys:
        return 0.0
    return len(set(keys)) / len(keys)


def infer_pk_columns(header: List[str], rows: List[List[str]]) -> List[str]:
    """主键识别规则兜底（10.2.1）：
    1. 列名匹配主体词表（按词表顺序第一个命中）；
    2. 兜底：第一个「唯一值率 > 0.8 且非度量」的列；
    3. 复合主键：单列唯一率 < 0.8 时追加限定列直到唯一（每列唯一率 > 0.5）。

    返回列名列表（复合主键按顺序）。
    """
    if not rows or not header:
        return []

    n_cols = len(header)
    n_rows = len(rows)

    def col_values(col_idx: int) -> List[str]:
        return [_cell(rows[r], col_idx) for r in range(n_rows)]

    def unique_ratio(col_idx: int) -> float:
        values = [v for v in col_values(col_idx) if v]
        if not values:
            return 0.0
        return len(set(values)) / len(values)

    # 优先级 2：词表匹配（多个命中时按唯一率取最佳——「品种」0.67 与
    # 「代码及合约」1.0 并列命中时选后者）
    best: Optional[int] = None
    best_ratio = 0.0
    for pattern in _PK_NAME_PATTERNS:
        for idx, col in enumerate(header):
            if pattern.lower() in (col or "").lower():
                ratio = unique_ratio(idx)
                if ratio >= 0.5 and ratio > best_ratio:
                    best_ratio = ratio
                    best = idx

    if best is None:
        # 优先级 3：高基数且非度量
        for idx, col in enumerate(header):
            if _is_metric(col or ""):
                continue  # 列名本身是度量词
            values = [v for v in col_values(idx) if v]
            if not values:
                continue
            if any(_is_metric(v) for v in values[: min(20, len(values))]):
                # 列值是度量 → 排除（采样前 20 行判断）
                continue
            ratio = unique_ratio(idx)
            if ratio > best_ratio:
                best_ratio = ratio
                best = idx
    if best is None:
        return []

    pk = [header[best]]
    # 复合主键：单列唯一率不达标时追加限定列，直到组合唯一率 ≥ 0.8
    if unique_ratio(best) < 0.8:
        remaining = [i for i in range(n_cols) if i != best]
        for i in remaining:
            combined = [
                f"{_cell(rows[r], best)}|{_cell(rows[r], i)}"
                for r in range(n_rows)
            ]
            combined = [c for c in combined if not c.endswith("|")]
            if not combined:
                continue
            ratio = len(set(combined)) / len(combined)
            if ratio >= 0.8:
                pk.append(header[i])
                break
    return pk


def _compose_header_parts(
    header_rows: List[List[str]], n_cols: int,
) -> List[List[str]]:
    """多级表头逐列拆解为父级链（供拼接与属性组共用）：
    ``审批授权情况 / 部门负责人`` → ``["审批授权情况", "部门负责人"]``；
    占位符（「—」等）、``col_N`` 兜底列名与重复值跳过；全空列返回 ``[]``。

    ``col_N`` 跳过（P0-c）：规则表头退化时它是占位符而非真列名，保留会让
    多级拼接产出 ``col_0-metric_id``；跳过后仍有真值的列自然拿到真列名，
    整列都是占位符的列在 `_compose_header_columns` 里照旧回落 ``col_N``。
    """
    out: List[List[str]] = []
    for c in range(n_cols):
        parts: List[str] = []
        seen: set = set()
        for row in header_rows:
            v = row[c].strip() if c < len(row) else ""
            if not v or v in seen or is_null_value(v):
                continue
            if _COL_PLACEHOLDER_RE.match(v):
                continue
            parts.append(v)
            seen.add(v)
        out.append(parts)
    return out


def _compose_header_columns(
    header_rows: List[List[str]], n_cols: int,
) -> List[str]:
    """多级表头逐列拼接（与 raganything/utils.py 的 `_compose_header`
    同口径——jonex_core 不反向依赖 vendored，故在平台侧复刻）：
    ``审批授权情况 / 部门负责人`` → ``审批授权情况-部门负责人``；
    占位符（「—」等）与重复值跳过；仍为空列保留 ``col_N`` 兜底。"""
    out: List[str] = []
    for c, parts in enumerate(_compose_header_parts(header_rows, n_cols)):
        out.append("-".join(parts) if parts else f"col_{c}")
    return out


def std_attribute_name(
    column: str, mapping: Optional[Dict[str, str]],
) -> str:
    """列名 → 标准属性名（§10.2.4 第二次 LLM 产物的消费入口）：
    映射表命中取标准名，否则原样返回。"""
    if not mapping or not column:
        return column
    return mapping.get(column) or column


def apply_header_verdict(
    header: List[str],
    rows: List[List[str]],
    verdict: TableVerdict,
) -> Tuple[List[str], List[List[str]], List[int]]:
    """[jonex] §table-grid-v2 步14 补缺：消费表级判定的表头/说明行结论。

    规则推断把「规则认定的表头」剥掉后，LLM 可能判定剩余的 data_rows 中
    还有表头行（多级表头未被规则识别）或说明行（长文本未被规则吸净）。
    此前 `header_row_indices` / `note_row_indices` 只校验不消费（对象抽取
    仍用规则表头）——本函数补齐下游消费：

    - ``header_row_indices``（相对 rows，0-based）→ 与既有 header 逐列
      拼接为多级列名（与 `_infer_table_header` 同拼接口径）；
    - ``note_row_indices`` → 从对象行剔除；
    - 其余行按原序保留，返回的 ``row_map[i]`` = 该行在原 rows 中的索引
      （source_chunks 行锚点不因剔除而错位）。

    无额外表头/说明行（或全越界）时原样返回，零副作用。
    主键/限定列的列名重映射由调用方按列位置做（列序恒定）。
    """
    h_idx = [i for i in verdict.header_row_indices if 0 <= i < len(rows)]
    note_idx = {i for i in verdict.note_row_indices if 0 <= i < len(rows)}
    if not h_idx and not note_idx:
        return header, rows, list(range(len(rows)))
    n_cols = len(header)
    new_header = _compose_header_columns([header] + [rows[i] for i in h_idx], n_cols)
    kept_rows: List[List[str]] = []
    row_map: List[int] = []
    for i, row in enumerate(rows):
        if i in h_idx or i in note_idx:
            continue
        row_map.append(i)
        kept_rows.append(row)
    return new_header, kept_rows, row_map


def detect_group_column(
    header: List[str], rows: List[List[str]],
) -> Optional[int]:
    """分组列检测（10.2.3）：rowspan forward-fill 产物——某列连续多行同值，
    变化次数 ≤ 行数一半（只看前 8 列，分组键通常靠左）。

    [jonex] 整列只有一个值（含空值占位符整列）不算分组：它没有分组信息，
    只会把全部行塞进同一个组。sdn.md 实测 Call_Sign 整列 ``-0-``（空值占位），
    变化次数 1 稳居候选首位，120 个对象全挂在 ``Call_Sign·-0-`` 一个组下。
    """
    if not rows:
        return None
    n_cols = len(header)
    n_rows = len(rows)
    best_col: Optional[int] = None
    best_changes = n_rows
    for c in range(min(n_cols, 8)):
        changes = 0
        prev: Optional[str] = None
        for row in rows:
            v = _cell(row, c)
            if v != prev:
                changes += 1
                prev = v
        if changes < best_changes and changes <= n_rows * 0.5:
            distinct = {_cell(row, c) for row in rows} - {""}
            if len(distinct) < 2:
                continue
            best_changes = changes
            best_col = c
    return best_col


@dataclass
class TableObject:
    """一行 → 一个对象实例（10.2.2）。"""

    canonical_name: str
    entity_type: str
    aliases: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    source_chunks: List[Dict[str, Any]] = field(default_factory=list)
    pk_value: str = ""
    table_sig: str = ""
    # [jonex] P0-c：canonical_name 的来源——"pk"（主键值）/ "row_hash"
    # （整行 hash 兜底）。调用方据此统计「主键识别失败但仍落了对象」的表数。
    name_source: str = "pk"


class TableObjectExtractor:
    """表格对象抽取器（确定性映射 + 表级 LLM 判定）。"""

    def __init__(
        self,
        cache: Optional[Dict[str, TableVerdict]] = None,
        pk_cache: Optional[Dict[str, Tuple[List[str], str]]] = None,
    ):
        self._cache = cache if cache is not None else {}
        # [jonex] P0-c：主键重判结果单独命名空间缓存（与 analyze_table 的
        # 表级判定缓存隔离），存 ``(pk_columns, source)``——source 一并缓存，
        # 否则命中片段会把「LLM 判出来的」误报成「缓存判出来的」。
        # 同一张表被切成 N 个片段时共用同一份表头签名，只有首次真调 LLM。
        self._pk_cache = pk_cache if pk_cache is not None else {}

    # ── 表级判定：LLM 主判 + 校验 + 规则 fallback（增补十 ②③④⑤）──

    @staticmethod
    def _validate_verdict(
        verdict: TableVerdict, n_rows: int,
    ) -> bool:
        """③ 输出校验（代码硬约束）：
        行号连续、从 0 开始、≤ 总行数、header 与 note 不重叠、层级 ≤3。
        """
        h = verdict.header_row_indices
        if not h:
            return False
        if h != list(range(h[0], h[0] + len(h))):  # 连续
            return False
        if h[0] != 0:
            return False
        if max(h) >= n_rows:
            return False
        if set(h) & set(verdict.note_row_indices):
            return False
        if len(h) > 3:
            return False
        return True

    async def analyze_table(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        scope: Optional[Dict[str, Any]] = None,
        tbox_attributes: Optional[List[str]] = None,
    ) -> TableVerdict:
        """表级判定（LLM 主判，合并表头判定模型化）+ 列名→TBox 属性映射。

        输入：表标题 + 列名清单 + 前 5 行样例；输出 TableVerdict。
        缓存命中 → cache；LLM 失败/校验不过 → 规则 fallback。
        第二次 LLM 调用（列名映射，§10.2.4）失败只丢弃映射，不阻塞判定；
        映射随判定一起按 `hash(表标题+列名签名)` 缓存。
        """
        column_list = "、".join(header)
        key = table_cache_key(table_caption, column_list)
        cached = self._cache.get(key)
        if cached is not None:
            # [jonex] §table-llm-bypass 修复：缓存对象与调用方隔离。
            # TaskManager._merge_table_objects 按列位置**原地**重映射
            # verdict.pk_columns（列名换成重拼表头的名字）。共享同一对象时，
            # 第二份起在原表头里找不到已重映射的名字 → 主键被清空 → 同一张表
            # 只有首份片段产出对象（缩减版 sdn.md 实测 6593 行只出 120 个）。
            hit = copy.deepcopy(cached)
            hit.source = "cache"
            return hit

        verdict = await self._llm_verdict(
            table_caption, header, rows, scope,
        )
        if verdict is not None and self._validate_verdict(verdict, len(rows)):
            verdict.source = "llm"
            verdict.column_mapping = await self._llm_column_mapping(
                header, scope, tbox_attributes,
            )
            # 原件只进缓存；出口一律给副本（调用方会原地改 pk_columns）
            self._cache[key] = verdict
            return copy.deepcopy(verdict)

        # ④ 规则 fallback（LLM 不可用/校验失败时必须能降级）
        logger.info(
            "TableObjectExtractor: LLM 判定不可用或校验失败，回退规则主键识别 "
            "(caption=%s)", table_caption[:40],
        )
        verdict = TableVerdict(
            object_type="",
            pk_columns=infer_pk_columns(header, rows),
            source="rules",
        )
        # 表头/说明行取规则推断的既有结果（调用方已跑 normalize_table_grid，
        # 此处不重跑——header 本身就是规则推断产物）
        return verdict

    async def _llm_verdict(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        scope: Optional[Dict[str, Any]],
    ) -> Optional[TableVerdict]:
        """LLM 表级判定（每表一次）。失败返回 None。"""
        try:
            from .ontology_extractor import _openai_chat
            from .prompts.table_object import TABLE_OBJECT_TYPE_PROMPT

            sample_rows = min(5, len(rows))
            samples = "\n".join(
                f"行 {i}: {' | '.join(str(c) for c in rows[i][: len(header)])}"
                for i in range(sample_rows)
            )
            prompt = TABLE_OBJECT_TYPE_PROMPT.format(
                table_caption=table_caption or "（无标题）",
                column_list="、".join(header),
                row_count=len(rows),
                sample_rows=sample_rows,
                row_samples=samples,
            )
            raw = await _openai_chat(
                [{"role": "user", "content": prompt}],
                temperature=0,
                scope=scope,
                json_mode=True,
            )
            data = json.loads(raw)
            return TableVerdict(
                header_row_indices=[int(i) for i in data.get("header_row_indices", [])],
                note_row_indices=[int(i) for i in data.get("note_row_indices", [])],
                levels=int(data.get("levels", 0)),
                object_type=str(data.get("object_type", "")),
                pk_columns=[str(c) for c in data.get("pk_columns", [])],
                qualifier_columns=[
                    str(c) for c in data.get("qualifier_columns", [])
                ],
            )
        except Exception as exc:  # noqa: BLE001 — 判定失败必须降级
            logger.warning("TableObjectExtractor LLM verdict failed: %s", exc)
            return None

    async def _llm_column_mapping(
        self,
        header: List[str],
        scope: Optional[Dict[str, Any]],
        tbox_attributes: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """§10.2.4 第二次 LLM：列名 → TBox 标准属性名（每表一次）。

        输出 `{原列名: 标准属性名}`；失败/校验不过返回 `{}`（映射是
        增强项，缺失时对象抽取用原列名，不阻塞入库）。
        """
        try:
            from .ontology_extractor import _openai_chat
            from .prompts.table_object import TABLE_COLUMN_MAPPING_PROMPT

            prompt = TABLE_COLUMN_MAPPING_PROMPT.format(
                column_list="、".join(header),
                tbox_attributes="、".join(tbox_attributes) if tbox_attributes else "（无）",
            )
            raw = await _openai_chat(
                [{"role": "user", "content": prompt}],
                temperature=0,
                scope=scope,
                json_mode=True,
            )
            data = json.loads(raw)
            mapping = data.get("mapping", {}) or {}
            out: Dict[str, str] = {}
            for k, v in mapping.items():
                kk = str(k).strip()
                vv = str(v).strip()
                if kk and vv and kk in header and vv != kk:
                    out[kk] = vv
            return out
        except Exception as exc:  # noqa: BLE001 — 映射失败只丢弃，不影响判定
            logger.warning("TableObjectExtractor column mapping failed: %s", exc)
            return {}

    # ── 主键重判（P0-c：表头纠正后补一次聚焦判定）──

    async def resolve_pk_columns(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        scope: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], str]:
        """[jonex] P0-c：用**纠正后的表头**重判主键。返回 ``(pk_columns, source)``。

        `analyze_table` 是一次调用里「先判表头、再判主键」的串行依赖，但喂给
        LLM 的列名清单是表头判定**之前**的规则表头。规则表头一旦退化成
        ``col_0…col_N``（表头推断失败），LLM 只能在坏列名里挑主键 →
        必然给不出结果；而空主键结论会被 `_validate_verdict` 当作合法结论
        放行（它只校验表头行号）→ 规则兜底轮不到 → 整表零对象。本方法把
        这条依赖拆开补齐：表头纠正后再判一次。

        ``source ∈ {"llm","rules",""}``（缓存命中返回原来源，不报 "cache"）；
        ``""`` 表示两路都没有可用主键，调用方**不要整表放弃**——
        `build_objects` 已内建整行 hash 兜底命名，整表放弃正是本次线上事故的
        起点（1438 张表零产出 → 零产出兜底退化为全量 LLM → 23.7GB OOM）。

        结果按 ``hash(表标题 + pk::列名签名)`` 缓存（与表级判定缓存隔离）：
        同一张表切成的 N 个片段共用表头签名，只有首次真调 LLM。
        """
        if not header or not rows:
            return [], ""
        key = table_cache_key(table_caption, "pk::" + "、".join(header))
        cached = self._pk_cache.get(key)
        if cached is not None:
            pk_c, src_c = cached
            # 缓存命中返回**原来源**（不是 "cache"）：同一张表的 N 个片段
            # 必须报同一个来源，否则 P0-c 指标会把「LLM 判出来的」记成别的。
            return list(pk_c), src_c

        pk = await self._llm_pk_columns(table_caption, header, rows, scope)
        source = "llm"
        if not pk:
            pk = infer_pk_columns(header, rows)
            source = "rules" if pk else ""
        # 空结论同样入缓存：避免同一张表的 N 个片段各打一次 LLM
        self._pk_cache[key] = (list(pk), source)
        return pk, source

    async def _llm_pk_columns(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        scope: Optional[Dict[str, Any]],
    ) -> List[str]:
        """聚焦的主键判定 LLM 调用（`TABLE_PK_PROMPT`）。失败/校验不过返回 ``[]``。"""
        try:
            from .ontology_extractor import _openai_chat
            from .prompts.table_object import TABLE_PK_PROMPT

            sample_rows = min(5, len(rows))
            samples = "\n".join(
                f"行 {i}: {' | '.join(str(c) for c in rows[i][: len(header)])}"
                for i in range(sample_rows)
            )
            # 各列唯一率：把「哪列真能区分行」算好喂给 LLM，比让它从样例行
            # 自己数样本可靠得多（21 列大表尤其明显）。
            column_stats = "、".join(
                f"{col or f'col_{i}'}({_column_unique_ratio(rows, i):.2f})"
                for i, col in enumerate(header)
            )
            prompt = TABLE_PK_PROMPT.format(
                table_caption=table_caption or "（无标题）",
                column_list="、".join(header),
                row_count=len(rows),
                column_stats=column_stats,
                sample_rows=sample_rows,
                row_samples=samples,
            )
            raw = await _openai_chat(
                [{"role": "user", "content": prompt}],
                temperature=0,
                scope=scope,
                json_mode=True,
            )
            data = json.loads(raw)
            return self._sanitize_pk(
                [str(c) for c in data.get("pk_columns", [])], header, rows,
            )
        except Exception as exc:  # noqa: BLE001 — 重判失败必须能回落规则
            logger.warning(
                "TableObjectExtractor LLM pk resolution failed: %s", exc,
            )
            return []

    @staticmethod
    def _sanitize_pk(
        raw_pk: List[str], header: List[str], rows: List[List[str]],
    ) -> List[str]:
        """LLM 主键输出的硬校验（增补十第 ③ 层口径，代码约束不信任模型）：

        - 逐字命中列名（不得改写/翻译/缩写/臆造）；
        - 去重保序、最多 3 列；
        - 组合唯一率 ≥ 0.5——选了没有区分度的列即判无效，回落规则兜底。
        """
        pk: List[str] = []
        for c in raw_pk:
            name = (c or "").strip()
            if name and name in header and name not in pk:
                pk.append(name)
        pk = pk[:3]
        if not pk:
            return []
        indices = [header.index(c) for c in pk]
        if _combined_unique_ratio(rows, indices) < 0.5:
            logger.info(
                "TableObjectExtractor: LLM 主键「%s」组合唯一率 < 0.5，判为无效",
                "、".join(pk),
            )
            return []
        return pk

    # ── 对象实例生成（每行一次，纯代码）──

    @staticmethod
    def _row_fallback_name(cells: List[str]) -> str:
        """整行 hash 兜底命名（P0-c）：可读前缀 + 内容摘要。

        取首个非空单元格作可读前缀（截 24 字符），拼整行内容的 md5 前 6 位。

        **名字是行内容的纯函数**（不含行号/序号）：同一张表被切成多个片段
        时，同内容行落在不同片段的第几行都会得到同名 → 写图按
        ``(table_sig, pk_value)`` upsert 时自然合并，不会因为切块位移而
        复制对象。带行号反而会破坏这个性质（同一行在两个片段里行号不同）。
        行内定位由 ``source_chunks.row_start`` 承担，不需要编进名字。
        """
        hint = next((c.strip() for c in cells if c and c.strip()), "")
        if len(hint) > 24:
            hint = hint[:24] + "…"
        digest = hashlib.md5(
            "\x1f".join(cells).encode("utf-8")
        ).hexdigest()[:6]
        return f"{hint}·{digest}" if hint else f"行-{digest}"

    def build_objects(
        self,
        table_caption: str,
        header: List[str],
        rows: List[List[str]],
        verdict: TableVerdict,
        table_sig: str,
        table_idx: Optional[int] = None,
        row_indices: Optional[List[int]] = None,
        header_parts: Optional[List[List[str]]] = None,
    ) -> List[TableObject]:
        """行 → 对象实例（10.2.2）。

        命名：主键列可用时用主键值；**整表主键识别失败**（`pk_columns` 为空，
        P0-c）时退化为整行 hash 兜底命名（`_row_fallback_name`），对象照常
        产出、属性照常写全——绝不因主键缺失退回全量 LLM 抽取。只有「整行
        全空」与「有主键但本行主键列为空」两种情况才跳过该行。

        row_indices：可选，rows 各行在原数据行序列中的索引（表头/说明行
        剔除后传入，保证 source_chunks 的 row_start/row_end 锚点不错位）。

        verdict.column_mapping（§10.2.4）：列名映射为标准属性名后，
        attributes 同时写原列名键与标准名键（S7 槽位命中两种问法都可达）。

        header_parts（层级关系 §10.2.3）：与 header 对齐的多级表头父级链
        （`_compose_header_parts` 产出）。父级链长度 >1 的列记入属性组——
        ``审批授权情况-部门负责人`` 记为属性组 ``审批授权情况`` 下的
        ``部门负责人``（嵌套 dict）；单级表头列保持扁平。
        """
        objects: List[TableObject] = []
        pk_indices = [
            header.index(c) for c in verdict.pk_columns if c in header
        ]
        qual_indices = [
            header.index(c) for c in verdict.qualifier_columns if c in header
        ]
        # [jonex] P0-c：主键彻底识别失败时**不再整表放弃**（原 `return objects`）。
        # 整表放弃 → 该文档全部表格零对象 → 调用方零产出兜底把表格 chunk 全量
        # 丢给 LLM 抽取（本次线上事故 46290 chunk / 4.6h / 23.7GB OOM）。
        # 改为整行 hash 兜底命名：主键不可得时用「行内容摘要」当对象名，属性
        # 照常按列写全，对象不再缺席。hash 不是列，不写入 attributes。
        hash_fallback = not pk_indices

        for r, row in enumerate(rows):
            src_row = row_indices[r] if row_indices else r
            cells = [_cell(row, i) for i in range(len(header))]
            if not any(cells):
                continue  # 整行空值：无内容可承载，跳过
            pk_values = [_cell(row, i) for i in pk_indices]
            pk_values = [v for v in pk_values if v]
            row_hash_used = False
            if not pk_values:
                if not hash_fallback:
                    continue  # 主键列本行无值（``-0-`` 占位）：无法命名，跳过
                pk_main = self._row_fallback_name(cells)
                qual_values: List[str] = []
                row_hash_used = True
            else:
                pk_main = pk_values[0]
                qual_values = [_cell(row, i) for i in qual_indices]
                qual_values = [v for v in qual_values if v]
                # 复合主键：主键值自身就含多段
                if len(pk_values) > 1:
                    pk_main = "|".join(pk_values)
            canonical = (
                f"{pk_main}（{'、'.join(qual_values)}）"
                if qual_values
                else pk_main
            )
            attributes: Dict[str, Any] = {}
            for idx, col in enumerate(header):
                v = cells[idx]
                if not v:
                    continue
                parts = (
                    header_parts[idx]
                    if header_parts is not None and idx < len(header_parts)
                    else None
                )
                if parts and len(parts) > 1:
                    # 层级关系：父级链 → 嵌套属性组，叶子列名过映射
                    leaf = std_attribute_name(parts[-1], verdict.column_mapping)
                    node: Dict[str, Any] = attributes
                    for g in parts[:-1]:
                        child = node.get(g)
                        if not isinstance(child, dict):
                            child = {}
                            node[g] = child
                        node = child
                    node[leaf] = v
                    if leaf != parts[-1]:
                        node[parts[-1]] = v
                else:
                    attributes[col] = v
                    std = std_attribute_name(col, verdict.column_mapping)
                    if std != col:
                        attributes[std] = v
            objects.append(TableObject(
                canonical_name=canonical,
                entity_type=verdict.object_type or "unknown",
                aliases=[pk_main, f"{table_caption}·{pk_main}"]
                if table_caption else [pk_main],
                attributes=attributes,
                source_chunks=[{
                    "row_start": src_row, "row_end": src_row + 1,
                    "table_idx": table_idx,
                    "table_sig": table_sig,
                }],
                pk_value=pk_main,
                table_sig=table_sig,
                name_source="row_hash" if row_hash_used else "pk",
            ))
        return objects

    # ── 关系生成（纯代码 + 规则）──

    def build_group_relations(
        self,
        header: List[str],
        rows: List[List[str]],
        objects: List[TableObject],
        verdict: TableVerdict,
    ) -> List[Dict[str, str]]:
        """分组关系（10.2.3）：forward-fill 分组列 → 包含关系。

        对象列表与 rows 行序一致（build_objects 保序），按分组列值把
        连续同值段的对象挂到「分组名（分组列: 值）」聚合节点下。
        """
        group_col = detect_group_column(header, rows)
        if group_col is None:
            return []
        group_type = header[group_col]
        relations: List[Dict[str, str]] = []
        for obj, row in zip(objects, rows):
            gv = _cell(row, group_col)
            if not gv:
                continue
            relations.append({
                "source_name": f"{group_type}·{gv}",
                "source_type": group_type,
                "target_name": obj.canonical_name,
                "target_type": obj.entity_type,
                "relation_type": _GROUP_RELATION_TYPE,
            })
        return relations

    def build_table_relations(
        self,
        objects: List[TableObject],
        table_sig: str,
    ) -> List[Dict[str, str]]:
        """[jonex] row-to-table：每个行对象一条 `包含` 边到它所属的表节点。

        与 `build_group_relations` 的分工：

        ================  ==============  ======================
        关系              方向            生效条件
        ================  ==============  ======================
        分组关系（现有）    分组节点 → 行    仅**有分组列**的表
        表归属（本方法）    表节点 → 行      **任何表**
        ================  ==============  ======================

        两者**并存**（设计 §3② 选 (a)），故有分组列的表其行对象有 2 条 `包含`
        边。没有分组列的表（实测大量参数表 / 方案表 / 字段说明表——每行一个
        独立条目，`detect_group_column` 正确地返回 None）靠本方法**不再孤立**。

        方向取「容器 → 行」与现有分组边一致（设计文档「修订 2」），下游按
        关系方向分流的既有逻辑因此无需改动。

        实测影响面：本体作答时命中实体的 63% 度≤1（其中孤立 39 个，79% 是行
        对象）——本方法覆盖那部分。
        """
        table_node = table_node_name(table_sig)
        rels: List[Dict[str, str]] = []
        for obj in objects or []:
            rels.append({
                "source_name": table_node,
                "source_type": _TABLE_NODE_TYPE,
                "target_name": obj.canonical_name,
                "target_type": obj.entity_type,
                "relation_type": _TABLE_RELATION_TYPE,
            })
        return rels


# ── [jonex] S1b：行对象的 chunk 证据回挂 ─────────────────────────────
# （docs/ontology/ontology-rag-scoring-unification-and-routing-plan.md
#   §6 S1b / §1.1 例外 / §12.5）
#
# 问题：行对象的 ``source_chunks`` 只有行锚点（table_sig / row_start /
# row_end / table_idx），没有 ``source_id`` / ``file_path``。查询侧
# ``_ontology_refs`` 是「source_chunks 非空 → 逐条取 file_path；为空才走
# doc_ids 文档级兜底」——行锚点实体**非空但条条判空**，于是既不产出 chunk
# 级引用、也不触发文档级兜底 → 行对象在图里贡献 0 引用。
#
# 修法（方案甲：抽取期 join）：抽取链路已把整 doc 的 chunk 读进内存
# （``_run_ontology_extraction`` → ``get_document_chunks``），在
# ``_merge_table_objects`` 里做一次 O(chunk) 内存 join，把命中的
# ``{source_id, file_path}`` 追加进行对象的 ``source_chunks``。此后
# ``_ontology_refs`` **零改动**即可产出引用，零额外 IO。
#
# ⚠️ join 键是「区间包含」，不是三元组相等（§12.5）：行对象是单行
# ``[src_row, src_row+1)``，chunk 是按字符预算打包的**多行**段
# ``[row_start, row_end)``，严格相等只在 chunk 恰好只装一行时命中。
# ⚠️ §12.5 前提修正：chunk 侧 ``table_sig`` 由**规则表头**计算
# （``stages.py`` 在无 LLM 的切块阶段），抽取侧由**表头判定修正后**的表头
# 计算（``task_manager`` 先 ``apply_header_verdict`` 再算 sig）。表头判定
# 改了列名时两者不等——单表文档按「只区间包含」容错（见
# ``attach_row_chunk_evidence``），多表文档计 miss 并打点。

# ``ONTOLOGY_SOURCE_CHUNKS_MAX`` 的默认值（与 native 抽取的
# ``_backfill_source_chunks`` 同一配额口径：0 = 不限）。
_ROW_EVIDENCE_CAP_DEFAULT = 32


def index_row_chunks(
    chunks: List[Dict[str, Any]],
) -> Dict[str, List[Tuple[int, int, str, str]]]:
    """把整 doc 的 chunk 列表按 ``table_sig`` 归集成行区间索引。

    只收 ``ctype=table_row`` 且 ``row_start`` / ``row_end`` / ``table_sig``
    三者齐全的 chunk——``ctype=table`` 的整表 chunk 没有行区间，无法参与
    区间匹配；``table_sig`` 缺失（``_table_grid_v2_enabled()`` 关闭时
    ``stages.py`` 不写该键）的 chunk 同样跳过，由调用方对空索引打点降级。

    Returns:
        ``{table_sig: [(row_start, row_end, chunk_id, file_path), ...]}``，
        内层按 chunk 列表原序（即 chunk_index 升序），保证产出确定性。
    """
    from jonex_core.common.file_source_util import parse_file_source

    index: Dict[str, List[Tuple[int, int, str, str]]] = {}
    for c in chunks or []:
        if not isinstance(c, dict):
            continue
        file_path = str(c.get("file_path") or "")
        chunk_id = str(c.get("chunk_id") or "")
        if not file_path or not chunk_id:
            continue
        try:
            meta = parse_file_source(file_path)
        except Exception as exc:  # noqa: BLE001 — 解析失败按非表格处理
            logger.debug("Row-as-Object S1b file_source 解析降级: %s", exc)
            continue
        if (meta.get("chunk_type") or c.get("chunk_type")) != "table_row":
            continue
        sig = meta.get("table_sig")
        row_start, row_end = meta.get("row_start"), meta.get("row_end")
        if not sig or row_start is None or row_end is None:
            continue
        index.setdefault(str(sig), []).append(
            (int(row_start), int(row_end), chunk_id, file_path)
        )
    return index


def attach_row_chunk_evidence(
    objects: List[TableObject],
    index: Dict[str, List[Tuple[int, int, str, str]]],
    *,
    max_chunks: int = _ROW_EVIDENCE_CAP_DEFAULT,
) -> Tuple[int, int, int]:
    """按区间包含把命中 chunk 追加进行对象的 ``source_chunks``。

    join 条件：``chunk.table_sig == obj.table_sig`` **且**
    ``chunk.row_start <= obj.row_start < chunk.row_end``。

    单表容错：整 doc 只有一份 ``table_sig`` 时（单表文档），无论对象 sig 是否
    相等都退回「只按区间包含」匹配——此时行区间无跨表歧义，sig 失配必然来自
    上方的「表头判定修正」而非表不同。多表文档不做该容错（行号按表重排，
    跨表匹配是错误引用）。

    幂等：``(source_id, file_path)`` 已在 ``source_chunks`` 里的不重复追加。

    Args:
        max_chunks: ``source_chunks`` 总量上限（含行锚点），0 = 不限。
            与 ``ONTOLOGY_SOURCE_CHUNKS_MAX`` 同口径，达配额即停（同一个
            事故根因：无上限的「命中即 append」把宿主内存吃到 OOM）。

    Returns:
        ``(matched, added, missed)``——至少追加了 1 条引用的对象数 / 追加的
        引用总条数 / 一条都没命中的对象数（sig 失配或缺行锚点）。
    """
    if not objects:
        return 0, 0, 0
    single_sig = next(iter(index)) if len(index) == 1 else None
    all_refs = [t for refs in index.values() for t in refs] if single_sig else []
    matched = added = missed = 0
    for obj in objects:
        sig = str(getattr(obj, "table_sig", "") or "")
        refs = index.get(sig)
        if refs is None and single_sig and sig:
            refs = all_refs
        anchor = next(
            (
                sc for sc in (obj.source_chunks or [])
                if isinstance(sc, dict) and sc.get("row_start") is not None
            ),
            None,
        )
        if refs is None or anchor is None:
            missed += 1
            continue
        row_start = int(anchor["row_start"])
        hits = [t for t in refs if t[0] <= row_start < t[1]]
        if not hits:
            missed += 1
            continue
        seen = {
            (sc.get("source_id"), sc.get("file_path"))
            for sc in (obj.source_chunks or [])
        }
        for _rs, _re, chunk_id, file_path in hits:
            if max_chunks > 0 and len(obj.source_chunks) >= max_chunks:
                break
            if (chunk_id, file_path) in seen:
                continue
            seen.add((chunk_id, file_path))
            obj.source_chunks.append(
                {"source_id": chunk_id, "file_path": file_path}
            )
            added += 1
        matched += 1
    return matched, added, missed
