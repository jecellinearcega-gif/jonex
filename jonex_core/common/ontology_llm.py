"""
本体问答 LLM 客户端：根据图谱事实回答查询，不足时返回 INSUFFICIENT。
"""

import json
import logging
import os
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from jonex_core.common.model_config import resolve_llm_model

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _scene(base: str, fast_mode: bool) -> str:
    """[jonex] 查询期思考分档：fast_mode 时派生 `<base>_fast` 变体 scene。

    网关据 LLMGW_DISABLE_THINKING_SCENES 白名单对 `_fast` 变体注入
    thinking={"type":"disabled"}（见 llm_gateway/upstream.py::_maybe_disable_thinking）。
    语义是「这次调用走快档」——精档与非严格模式使用原始 scene 名，天然保留思考。
    计量维度随之可分：`_fast` 占比 = 被快档解决的查询比例。
    """
    return f"{base}_fast" if fast_mode else base


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=os.getenv(
                "ONTOLOGY_LLM_BINDING_HOST",
                "http://llm-gateway:8787/v1",
            ),
            api_key=os.getenv("ONTOLOGY_LLM_BINDING_API_KEY", ""),
        )
    return _client


# ── 引用标记归一化（答案内联 [n] 引用） ────────────────────────────

_REF_MARKER_RE = re.compile(r"\[(ref[-_]?)?(\d+)\]")

_REFERENCES_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]?\s*$",
    re.IGNORECASE,
)

# [jonex] 引用范围压缩：相邻引用编号里，连续长度 ≥ 该值的合并为「起始-结束」范围展示。
_RANGE_MIN_RUN = 3

# LLM 生成的引用范围连接符（[a]–[b]）：en dash / em dash / 连字符 / 波浪线（半角 + 全角）
# [jonex] 补充中文范围词「至」「到」——中文答案里 LLM 常用「[ref_5] 至 [ref_5]」
# 表达范围，漏掉会导致 [5] 至 [5] 无法压缩成 [5]、[1] 至 [4] 无法压缩成 [1-4]。
_RANGE_RE = re.compile(r"\[(\d+)\]\s*[-–—~～至到]\s*\[(\d+)\]")

# 相邻引用序列（两个及以上 [n] 相邻，中间仅空白）
_ADJACENT_RE = re.compile(r"\[\d+\](?:\s*\[\d+\])+")


def _norm_range(m: re.Match) -> str:
    """把 LLM 生成的 [a]–[b] 规范化为 [a-b]（升序；退化时 [a]）。"""
    a = int(m.group(1))
    b = int(m.group(2))
    if a == b:
        return f"[{a}]"
    lo, hi = (a, b) if a < b else (b, a)
    return f"[{lo}-{hi}]"


def _compress_adjacent(m: re.Match) -> str:
    """把相邻引用序列 [1][2][3][4][6][8] 压缩为 [1-4][6][8]。

    相邻去重后，连续长度 ≥ _RANGE_MIN_RUN 的编号合并为 [起始-结束]，
    不足的保持单个 [n]。
    """
    nums = [int(x) for x in re.findall(r"\[(\d+)\]", m.group(0))]
    deduped: list[int] = []
    for n in nums:
        if not deduped or deduped[-1] != n:
            deduped.append(n)
    out: list[str] = []
    i = 0
    while i < len(deduped):
        j = i
        while j + 1 < len(deduped) and deduped[j + 1] == deduped[j] + 1:
            j += 1
        if j - i + 1 >= _RANGE_MIN_RUN:
            out.append(f"[{deduped[i]}-{deduped[j]}]")
        else:
            out.extend(f"[{x}]" for x in deduped[i:j + 1])
        i = j + 1
    return "".join(out)


# 单个引用标记 [n] 或范围 [n-m]
_CITE_RE = re.compile(r"\[(\d+)(?:-(\d+))?\]")


def _dedup_global_citations(text: str) -> str:
    """全局去重：每个引用编号在答案里只保留首次出现。

    normalize_citations 按 doc_id 归并后，同一文档被多处引用会产出多个同号角标
    （如「[1] … [1]」或「[1-4] … [3]」）。紧邻去重只覆盖相邻重复，本函数按
    全文首次出现去重：单个 [n] 重复时删除后续整段；范围 [n-m] 里的编号逐个
    登记，已出现过的编号从范围中剔除（连续段按 _RANGE_MIN_RUN 重合并），
    剔空则删除整段。
    """
    seen: set[int] = set()
    out: list[str] = []
    pos = 0
    for m in _CITE_RE.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        keep = [x for x in range(a, b + 1) if x not in seen]
        if not keep:
            continue
        for x in keep:
            seen.add(x)
        rebuilt: list[str] = []
        i = 0
        while i < len(keep):
            j = i
            while j + 1 < len(keep) and keep[j + 1] == keep[j] + 1:
                j += 1
            if j - i + 1 >= _RANGE_MIN_RUN:
                rebuilt.append(f"[{keep[i]}-{keep[j]}]")
            else:
                rebuilt.extend(f"[{x}]" for x in keep[i:j + 1])
            i = j + 1
        out.append("".join(rebuilt))
    out.append(text[pos:])
    return "".join(out)


def normalize_citations(
    answer: str,
    number_to_doc_id: dict[str, str],
    final_references: list[dict],
) -> str:
    """把答案内的引用标记（`[ref_1]` / `[1]`）重排为最终 references 下标。

    Args:
        answer: 含引用标记的答案文本。
        number_to_doc_id: 标记数字字符串（"1"/"2"…）→ doc_id 的映射。
            路径②（answer_from_chunks）的数字 = `[ref_i]` 中的 i；
            路径①（LightRAG）的数字 = 答案内联 `[n]` 的 n。
        final_references: `_build_references` 产出的聚合引用列表，顺序即卡片顺序。

    规则：doc_id 在 final_references 首次出现的位置 = 编号（1-based）；同一 doc
    去重；`[ref_N]` 形态映射不到时丢弃（防幻觉/越权），纯 `[N]` 形态映射不到时
    保留原样（可能是年份/列表等普通文本，避免误删）。
    """
    if not answer or not number_to_doc_id:
        return answer

    doc_to_idx: dict[str, int] = {}
    for idx, ref in enumerate(final_references):
        did = ref.get("doc_id")
        if did and did not in doc_to_idx:
            doc_to_idx[did] = idx + 1
    if not doc_to_idx:
        return answer

    def _repl(m) -> str:
        is_ref = m.group(1) is not None  # [ref_N] 形态
        did = number_to_doc_id.get(m.group(2))
        idx = doc_to_idx.get(did) if did else None
        if idx is not None:
            return f"[{idx}]"
        return "" if is_ref else m.group(0)

    normalized = _REF_MARKER_RE.sub(_repl, answer)
    # [jonex] 引用范围压缩：
    #   1. LLM 生成的 [a]–[b] 规范化为 [a-b]（见 _norm_range）；
    #   2. 相邻连续编号 [1][2][3][4][6][8] 压缩为 [1-4][6][8]（见 _compress_adjacent）；
    #   3. 相邻重复兜底去重 [1][1] → [1]；
    #   4. 全文级去重：同文档多处引用归并出的同号角标只保留首次出现（见
    #      _dedup_global_citations）。
    # 压缩后前端把 [n] / [n-m] 渲染为圆形角标，连续引用不再逐个罗列。
    normalized = _RANGE_RE.sub(_norm_range, normalized)
    normalized = _ADJACENT_RE.sub(_compress_adjacent, normalized)
    normalized = re.sub(r"(\[\d+\])\s*(?:\1\s*)+", r"\1", normalized)
    normalized = _dedup_global_citations(normalized)
    return normalized


_EVIDENCE_HANDLE_RE = re.compile(r"\[E([1-9][0-9]*)\]")


def normalize_evidence_handles(
    answer: str,
    handle_to_doc_id: dict[str, str],
    final_references: list[dict],
) -> str:
    """把 jonex_wiki 引擎答案里的 [E#] 证据 handle 改写为最终 references 下标 [n]。

    与 ``normalize_citations`` 同构，但输入标记是引擎 synthesis 阶段的证据短句柄
    （``[E1]``/``[E2]``，见 Reference/JonexWiki synthesis.py）。``handle_to_doc_id``
    是 ``"[E1]"`` → ``document_id`` 的映射，由引擎 citations 的 ``handle`` +
    ``document_id`` 字段构建；``final_references`` 顺序即卡片顺序。

    规则：``[E#]`` 经 handle → document_id → 卡片下标两级映射到 ``[n]``；映射不到
    （handle 缺失 / document_id 不在卡片里，如表格句柄）时保留原样，避免误删正文。
    """
    if not answer or not handle_to_doc_id:
        return answer

    doc_to_idx: dict[str, int] = {}
    for idx, ref in enumerate(final_references):
        did = ref.get("doc_id")
        if did and did not in doc_to_idx:
            doc_to_idx[did] = idx + 1
    if not doc_to_idx:
        return answer

    def _repl(m: re.Match) -> str:
        handle = f"[E{m.group(1)}]"
        did = handle_to_doc_id.get(handle)
        idx = doc_to_idx.get(did) if did else None
        return f"[{idx}]" if idx is not None else m.group(0)

    normalized = _EVIDENCE_HANDLE_RE.sub(_repl, answer)
    # 复用范围压缩（与 normalize_citations 一致）
    normalized = _RANGE_RE.sub(_norm_range, normalized)
    normalized = _ADJACENT_RE.sub(_compress_adjacent, normalized)
    normalized = re.sub(r"(\[\d+\])\s*(?:\1\s*)+", r"\1", normalized)
    return normalized


def strip_trailing_references(answer: str) -> str:
    """剥掉答案末尾的「References / 参考文献」引用尾注段（LightRAG 原生答案产物）。

    引用已由 references 卡片单独承载，尾注段与卡片重复，需从答案文本移除，避免
    前端把整段当作正文渲染。仅剥尾部：命中首个 References 标题后，其后（含标题）
    整段截断。
    """
    if not answer:
        return answer
    lines = answer.split("\n")
    for i, line in enumerate(lines):
        if _REFERENCES_HEADING_RE.match(line):
            return "\n".join(lines[:i]).rstrip()
    return answer


async def arbitrate_answers(
    query: str,
    ontology_answer: str,
    ontology_evidence: str,
    rag_answer: str,
    rag_evidence: str,
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict:
    """[jonex] S1+S7 双向校验裁决（改动 48）。

    对称呈现两侧答案与证据，输出严格 JSON
    ``{verdict, confidence, reason, answer}``：
    - verdict: "ontology" / "rag"（采纳哪一侧）
    - answer: 采纳的答案文本（可做小幅订正）
    - confidence: 0~1
    - reason: 裁决理由（分歧必须可见）

    裁决偏向按**证据类型**（结构化属性 > 自由文本 > 摘要；带锚点 > 无锚点），
    不按来源类型。解析失败抛异常，由调用方回退原答案（绝不因裁决故障丢答案）。
    计量场景头 ``ontology_arbitration``。
    """
    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    system_prompt = (
        "You are an answer arbitration expert. Compare two candidate answers "
        "to the same question and decide which one to adopt.\n"
        "Evidence hierarchy (most reliable first): structured attributes "
        "(key=value fields) > free-text descriptions > table summaries; "
        "evidence with location anchors (row/page/timestamp) beats evidence "
        "without. Prefer the answer backed by stronger evidence type, NOT by "
        "its source.\n"
        "If both sides agree, adopt the more specific one. If they conflict, "
        "pick the side with stronger evidence. Never fabricate facts; you may "
        "only choose between the two candidates (optionally trimming obviously "
        "wrong fragments).\n"
        "Output strict JSON: {\"verdict\": \"ontology\"|\"rag\", "
        "\"confidence\": 0.0-1.0, \"reason\": \"short reason\", "
        "\"answer\": \"adopted answer text\"}"
    )
    user_prompt = json.dumps(
        {
            "query": query,
            "ontology_answer": ontology_answer,
            "ontology_evidence": ontology_evidence[:3000],
            "rag_answer": rag_answer,
            "rag_evidence": rag_evidence[:3000],
        },
        ensure_ascii=False,
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": "ontology_arbitration",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    import uuid

    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"arbitration:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    raw = resp.choices[0].message.content or ""
    result = json.loads(raw)
    result.setdefault("verdict", "ontology")
    result.setdefault("answer", ontology_answer)
    result.setdefault("reason", "")
    result.setdefault("confidence", 0.5)
    return result


# ── 送 LLM 的 payload 瘦身（引用锚点不进 prompt） ──────────────────

# 只用于**回指原文**的字段（`_build_references` / `_facts_number_to_doc_id` 消费），
# 对作答没有信息量。实测它们能占 payload 的 95%——见 `answer_from_facts` 的注释。
_ANCHOR_FIELDS = ("source_chunks", "relation_source_chunks")


def _lean_entity(ent: dict) -> dict:
    """实体的 LLM 视图：浅拷贝并剔除锚点字段。

    ⚠️ 必须**新建 dict**——就地 `del` 会把调用方的 `facts` / `hits` 改坏。
    """
    if not isinstance(ent, dict):
        return ent
    return {k: v for k, v in ent.items() if k not in _ANCHOR_FIELDS}


def _lean_fact(f: dict) -> dict:
    """fact 的 LLM 视图：剔除锚点，并对其 `target_entity` 递归剔除。"""
    if not isinstance(f, dict):
        return f
    out = {k: v for k, v in f.items() if k not in _ANCHOR_FIELDS}
    te = out.get("target_entity")
    if isinstance(te, dict):
        out["target_entity"] = _lean_entity(te)
    return out


async def answer_from_facts(
    query: str,
    hits: list[dict],
    facts: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    allow_common_sense: bool = False,
    fast_mode: bool = False,
) -> str:
    """根据本体实体 + 邻居事实回答，不足返回 "INSUFFICIENT"。

    Args:
        allow_common_sense: [jonex] P2-7 放宽常识边界。
            为 True 时允许模型在已取到确定领域事实的基础上，叠加通用常识/数学
            （物理常量、semver 语义、单位换算），但必须标注假设。
        fast_mode: [jonex] 查询期思考分档（docs/ontology-query-thinking-latency-fix-plan.md §3）。
            为 True 时 scene 派生 `ontology_qa_fast`，由网关白名单注入禁思考
            （严格模式快档）；False 保留原 scene 与思考（精档/非严格模式）。
    """
    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    system_prompt = (
        "You are a knowledge graph query assistant. Answer based solely on the "
        "provided facts. If the facts do not contain enough information to answer "
        'the question, respond with exactly "INSUFFICIENT". Do not make up information. '
        "Facts may include multi-hop relationships. Each fact has a \"hop\" field (1 = direct "
        "neighbor, 2+ = indirect via intermediate entities), a \"path\" listing the entity "
        "chain, and a \"target_entity\" object with the target entity's full content "
        "(description, attributes, aliases). The same target entity may appear more than "
        "once via different relation paths; treat them as distinct evidence. Prefer "
        "lower-hop facts; use higher-hop facts only as supporting context. "
        'Each relation fact carries a "_no" field (its 1-based reference number). '
        'When your answer relies on a specific fact, cite it inline using [ref_i] markers '
        '(e.g. "[ref_1][ref_3]") at the end of the corresponding sentence. Cite only '
        "the facts you actually use; never cite a number you did not rely on."
    )
    # [jonex] P2-7 放宽常识边界：允许叠加通用常识/数学，但必须标注假设
    if allow_common_sense:
        common_sense_append = os.getenv("ONTOLOGY_ANSWER_COMMON_SENSE_APPEND", "")
        if not common_sense_append:
            common_sense_append = (
                "\n\nIf the question can be answered by applying well-known physical constants "
                "(e.g. specific heat of water ≈ 4186 J/(kg·K), boiling point of water ≈ 100°C), "
                "basic math, or standard semantics (e.g. semver version ordering), you may do so "
                "on top of the provided domain facts. HOWEVER, you MUST: "
                "1) explicitly state the assumption (e.g. \"Assuming c≈4186 J/(kg·K)\"), "
                "2) never fabricate product parameters, hardware specs, or domain facts, "
                "3) clearly separate which parts of the answer come from provided facts "
                "and which come from general knowledge."
            )
        system_prompt += common_sense_append

    # [jonex] 本体引用编号：给每个 relation 分配 1-based `_no` 编号，LLM 以
    # [ref_i] 引用；调用方按相同索引提取 doc_id 做 normalize_citations。
    #
    # ⚠️ 送 LLM 的 payload 必须**剔除引用锚点**（`source_chunks`）。线上实测
    # （2026-09-24，火炬库「军用多层瓷介电容器的产品特性是什么」）：命中 18 个实体
    # → 邻域并集 **97 条 fact**，而 `target_entity.source_chunks` 是每个实体命中的
    # **几百条 `{source_id, file_path}`**（单个实体 318 条 / 85,320 字符，且因该实体
    # 在多条 fact 里重复而**被塞了 7 遍**）——占 payload 的 **95%**：
    #
    #     relations 原样（含锚点）= 1,616,040 字符 → 剔除锚点 = 74,437 字符（−95%）
    #
    # 对应网关侧 `prompt=896488` token、上游生成 **40 秒** → 平台
    # `ONTOLOGY_ANSWER_TIMEOUT`（30s）判超时失败 → SDK 重试再来 **21 秒**，大部分
    # 是白跑。锚点对作答没有信息量：system prompt 只声明模型需要 `hop` / `path` /
    # `target_entity` 的 `description, attributes, aliases`；而 doc_id 是**平台侧**
    # 从 `facts` 取的（`_facts_number_to_doc_id`），不依赖模型看见锚点。
    #
    # ⚠️ 用**浅拷贝重建**，不得就地删字段 —— `facts` / `hits` 之后还要被
    # `_facts_number_to_doc_id` / `_cited_doc_ids` / 引用构建读取。
    numbered_relations = [
        {**_lean_fact(f), "_no": i} for i, f in enumerate(facts, 1)
    ]
    facts_text = json.dumps(
        {"entities": [_lean_entity(e) for e in hits],
         "relations": numbered_relations},
        ensure_ascii=False,
    )

    # 注入计量上下文头
    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("ontology_qa", fast_mode),
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    # 链路追踪 ID：本次逻辑问答生成一次，SDK 内部重试会复用同一头 + 同一 body，
    # 网关据此 + body 哈希派生稳定的计量幂等键，实现重试去重。
    # 不再注入 X-Jonex-Request-Id（避免每次随机值导致重试被重复计量）。
    import uuid
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"ontology_qa:{uuid.uuid4().hex}"

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Facts:\n{facts_text}\n\nQuestion: {query}",
                },
            ],
            # [jonex] 四屏修复 (a)③：0.1→0。两侧答案生成非确定时（temp=0.1 微漂），
            # 裁决输入漂移 → 同 query 裁决结论在 rag/ontology 间反复翻转。
            # 答案温度归零消除输入漂移，裁决（arbitrate_answers 本已 temp=0）收敛。
            temperature=0,
            max_tokens=8192,
            extra_headers=extra_headers,
        )
        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason
        content = (msg.content or "").strip()

        if not content:
            # [jonex] 思考模式下正文可能落在 reasoning_content、content 为空
            # （上游 tokenhub 行为，Reference/Rag-anything 侧 _metered_llm() 已同源修复过一次）。
            # ⚠️ 仅在**正常结束**时兜底：finish_reason=="length" 说明思考链自身被截断，
            #    此时 reasoning_content 是不完整的思考文本而非答案，且直接返回会泄露思考链。
            if finish == "length":
                logger.warning(
                    "本体 LLM 输出预算耗尽（finish_reason=length）且正文为空——"
                    "非事实不足，而是 max_tokens 被思考 token 吃光；返回 INSUFFICIENT 交由升档重跑"
                )
                return "INSUFFICIENT"
            content = (getattr(msg, "reasoning_content", None) or "").strip()
            if content:
                logger.info("本体 LLM content 为空，已用 reasoning_content 兜底（finish_reason=%s）", finish)

        if not content:
            logger.warning("本体 LLM 返回空内容 finish_reason=%s（兜底后仍为空）", finish)
            return "INSUFFICIENT"
        return content
    except Exception as e:
        logger.warning("本体 LLM 调用失败: %s", e)
        return "INSUFFICIENT"


async def answer_from_chunks(
    query: str,
    chunks: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    fast_mode: bool = False,
) -> tuple[str, list[dict]]:
    """[jonex] 方案 A：基于召回 chunk 原文做一次平台侧作答（场景 rag_chunk_qa）。

    与 answer_from_facts 的关键差异：
    - 送进 prompt 的 chunk 就是答案的证据来源（答案与引用同源）；
    - LLM 异常**向上抛出**，由调用方（_rag_fallback_multi）转为
      `ServiceUnavailableError` 上报，不吞掉异常返回占位文本（避免假答案）。

    Args:
        chunks: 排序/截断后的 chunk 列表，每项含
            doc_id / file_name / kb_id / chunk_id / text / relevance，
            以及 [jonex] 表格方案改动 64 的 ctype / row_start / row_end。
            ⚠️ 检索侧 refs 由 parse_file_source 产出，类型键名是 chunk_type
            （非 ctype）——读取时两键兼容；全量 reparse 前存量 chunk 无类型
            字段，prompt 无类型标签属预期。
        kb_id: 多 KB 场景取主 KB（kb_ids[0]），与检索侧引用重排口径一致。

    Returns:
        (content, budgeted): content 为答案文本（含 `[ref_i]` 内联标记）；budgeted
        为实际送入 prompt 的 chunk 列表（与 `[ref_i]` 一一对应，供调用方做引用
        编号归一化，避免重复复刻预算截断逻辑）。
    """
    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    # [jonex] S4（§6「截断单点化」）：预算截断的唯一权威点是检索侧
    # `_rag_platform_answer` 第 4 步——它已按同一个 `RAG_ANSWER_MAX_CONTEXT_CHARS`
    # 装好箱再把 `prompt_refs` 递进来。本函数**不再做第二道独立截断**，这里的
    # 循环降级为「断言 + 兜底」：口径若被改漂（调用方换了预算、或有人直接调本
    # 函数），也不静默丢证据，而是留痕并保持与检索侧一致的贪心语义。
    #
    # 为什么原实现（`if total + len(text) > max_chars: break`）是错的：`break`
    # 让一个超长 chunk 把**其后全部**候选一并挡在 prompt 外。候选按分数降序，
    # 低分大 chunk 排在前面时，后面所有高分小 chunk 都进不来——检索侧改成贪心
    # 装箱后两边口径也会不一致。`continue` 则只跳过装不下的那一条。
    max_chars = int(os.getenv("RAG_ANSWER_MAX_CONTEXT_CHARS", "12000"))
    budgeted: list[dict] = []
    total = 0
    oversized: list[dict] = []
    for c in chunks:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            oversized.append(c)
            continue
        budgeted.append(c)
        total += len(text)
    if oversized:
        logger.warning(
            "[answer_from_chunks] 收到 %d 条候选、预算 %d 字符装下 %d 条，"
            "跳过 %d 条超长项（首个 %d 字符）——正常情况下调用方应已按同一预算"
            "装箱，出现本告警说明两处口径漂移，请核对 RAG_ANSWER_MAX_CONTEXT_CHARS",
            len(chunks), max_chars, len(budgeted), len(oversized),
            len((oversized[0].get("text") or "")),
        )

    system_prompt = (
        "You are a retrieval-augmented QA assistant. Answer the question based "
        "solely on the provided reference chunks. Each chunk is prefixed with a "
        "[ref_i] marker; cite the markers you rely on (e.g. \"[ref_1][ref_3]\") at "
        "the end of the corresponding sentences. Do not make up information that is "
        "not in the chunks. If the chunks are insufficient, say so explicitly and "
        "list what is missing. Chunks may include table rows (labeled 表格明细) and "
        "table overviews (labeled 表格概览) — for precise value lookups prefer 表格明细; "
        "for structural summaries prefer 表格概览. "
        "Chunks labeled 图片描述 are visual descriptions of images in the document — "
        "they describe what appears in a picture, not the document's textual content. "
        "Do not treat image descriptions as factual statements from the document; "
        "only use them when the user is explicitly asking about visual content. "
        "Chunks labeled 图表数据 are numbers read off charts and figures; they ARE "
        "document content (a datasheet's curve is part of the spec), so use them to "
        "answer — but they are estimated from the axes, not measured. Trust the shape, "
        "trend and order of magnitude; do not quote an exact figure as precise, and say "
        "the value is read from a chart when the precision matters."
    )

    lines: list[str] = []
    for i, c in enumerate(budgeted, 1):
        fname = c.get("file_name") or c.get("doc_id") or ""
        # 检索侧 chunk 的类型键是 chunk_type（parse_file_source 产物）；
        # ctype 仅为历史/其他调用方兜底（见 answer_from_chunks docstring）。
        ctype = c.get("ctype") or c.get("chunk_type")
        label = ""
        if ctype == "table_row":
            rs, re_ = c.get("row_start"), c.get("row_end")
            label = f"表格明细（行 {rs}-{re_}）" if rs is not None and re_ is not None else "表格明细"
        elif ctype == "table_summary":
            label = "表格概览"
        elif ctype == "image":
            label = "图片描述"
        # [jonex] `chart` **不借**「图片描述」这个标签：那条规则说「只在用户明确问视觉
        # 内容时才用」——对图表明明太严（数据手册的曲线就是答案本身，用户问「105℃ 时
        # 容值变化多少」正是在问图里的数）。它需要的是另一条：数值是**估读**的。
        elif ctype == "chart":
            label = "图表数据"
        # [jonex] 视频内容的三类来源（见 stages.py 的 _SCENE_SOURCE_CTYPE）——
        # 让作答时能区分引用来自语义章节 / 幻灯片 OCR 原文 / 语音转写。
        elif ctype == "video_chapter":
            label = "视频章节"
        elif ctype == "video_slide":
            label = "幻灯片"
        elif ctype == "audio_segment":
            label = "语音转写"
        header = f"[ref_{i}] {fname}" + (f"（{label}）" if label else "")
        lines.append(f"{header}\n{c.get('text', '')}")

    # 注入计量上下文头（新场景 rag_chunk_qa，与 ontology_qa / rag_fusion 分离统计）
    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("rag_chunk_qa", fast_mode),
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    # 链路追踪 ID：与 answer_from_facts / fuse_rag_answers 同一幂等去重机制
    import uuid
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"rag_chunk_qa:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Reference chunks:\n\n" + "\n\n".join(lines)
                           + f"\n\nQuestion: {query}",
            },
        ],
        # [jonex] 四屏修复 (a)③：0.1→0，消除裁决两侧答案输入漂移（同 answer_from_facts）。
        temperature=0,
        max_tokens=8192,
        extra_headers=extra_headers,
    )
    msg = resp.choices[0].message
    finish = resp.choices[0].finish_reason
    content = (msg.content or "").strip()
    if not content:
        # [jonex] 与 answer_from_facts 同源的 reasoning_content 兜底：
        # finish_reason=="length" 时思考链自身被截断（不完整思考文本而非答案，
        # 直接返回会泄露思考链）→ 保持「抛异常、不返回占位文本」的既有语义。
        if finish != "length":
            content = (getattr(msg, "reasoning_content", None) or "").strip()
            if content:
                logger.info("answer_from_chunks content 为空，已用 reasoning_content 兜底（finish_reason=%s）", finish)
    if not content:
        # 空答案视为失败 → 抛出，由调用方转为 ServiceUnavailableError（不返回空答案）
        raise RuntimeError(
            f"answer_from_chunks 返回空内容 finish_reason={finish}"
        )
    return content, budgeted


async def fuse_rag_answers(
    query: str,
    per_kb_answers: list[dict],
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    fast_mode: bool = False,
) -> str:
    """将多个 KB 的 RAG 答案融合为一个准确、无重复、标注来源的统一回答。

    Args:
        query: 原始查询
        per_kb_answers: [{"kb_id": ..., "answer": ...}, ...] 至少 2 条

    Returns:
        融合后的回答字符串
    """
    if len(per_kb_answers) < 2:
        return per_kb_answers[0]["answer"] if per_kb_answers else ""

    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    system_prompt = (
        "You are a RAG answer fusion assistant. Given multiple answers from different "
        "knowledge bases for the same question, produce a single, accurate, non-redundant "
        "answer. Resolve factual conflicts. When noting which knowledge base a piece of "
        "information comes from, you MUST ONLY use the exact knowledge base names provided "
        "in the input — a name may list multiple knowledge bases separated by a Chinese "
        "enumeration comma '、', in which case attribute the information to that group "
        "rather than picking one of them. If you cannot attribute a piece of information "
        "confidently, do not label it. The placeholder \"unspecified\" means the source "
        "knowledge bases could not be located — never print \"unspecified\" in your "
        "answer and do not label such information. Never invent knowledge base names. "
        "Do not make up information. "
        # [jonex] 方案 B：融合前调用方已把单侧答案内的引用编号重写为「最终合并
        # 引用卡片」的下标（见 search_service _fuse 前 normalize_citations）。
        # 此处要求 LLM 原样保留这些 [n] 标记并紧跟其引用的内容——不丢弃、不
        # 重排、不合并；未带编号的答案片段保持不标注即可。
        "Some input answers carry inline citation markers like [1] or [2] that refer "
        "to numbered source cards. Preserve these markers verbatim and keep each marker "
        "attached to the sentence it cites — do not drop, renumber, merge, or move them. "
        "Answers without markers are fine; leave them unmarked."
    )

    import uuid

    # [jonex] 只把「知识库显示名 + 答案」喂给 LLM——此前 json.dumps(per_kb) 会把
    # kb_id（UUID 不可读）和内部 source 字段（如 "llm-wiki"）直接暴露给 LLM，
    # 产出「知识库 `llm-wiki`」「知识库 `470a9519...`」这类错误标注。
    # 标签为空（该侧引用未定位到 KB）时落 unspecified，prompt 已指示不输出该词。
    def _kb_label(a: dict) -> str:
        return str(a.get("kb_name") or "unspecified")

    kb_texts = "\n\n".join(
        f"[Knowledge base: {_kb_label(a)}]\n{a.get('answer', '')}"
        for a in per_kb_answers
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("rag_fusion", fast_mode),
    }
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"rag_fusion:{uuid.uuid4().hex}"

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Query: {query}\n\n"
                        f"Answers from different knowledge bases:\n{kb_texts}\n\n"
                        "Please produce a single fused answer."
                    ),
                },
            ],
            # [jonex] 四屏修复 (a)③：0.1→0，消除裁决两侧答案输入漂移（同 answer_from_facts）。
            temperature=0,
            max_tokens=4096,
            extra_headers=extra_headers,
        )
        content = resp.choices[0].message.content
        return (content or "").strip() or "\n\n---\n\n".join(
            [a["answer"] for a in per_kb_answers]
        )
    except Exception as e:
        logger.warning("RAG 答案融合 LLM 调用失败: %s", e)
        # 降级：直接拼接各 KB 答案
        parts = [a["answer"] for a in per_kb_answers]
        return "\n\n---\n\n".join(parts)


async def anchor_answer_to_references(
    answer: str,
    references: list[dict],
    query: str = "",
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> str:
    """把无内联引用标记的答案锚定到引用卡片：LLM 在答案里插入 [n] 标记。

    用于 openkb（llm-wiki）这类「答案无 [n]/[E#] 内联标记」的管线。references 顺序
    即卡片顺序；锚定素材是文档名 + 知识库名（openkb 引用为文档级，无 excerpt）。
    LLM 只标注能确定来源的句子（不确定不标），不改正文措辞、绝不编造来源。
    LLM 失败或无可锚定素材时原样返回 answer（降级不阻塞检索）。
    """
    if not answer or not references:
        return answer

    doc_lines: list[str] = []
    for i, ref in enumerate(references, 1):
        name = str(ref.get("file_name") or ref.get("doc_id") or f"document {i}")
        kb = str(ref.get("kb_name") or "")
        doc_lines.append(f"{i}. {name}" + (f"（{kb}）" if kb else ""))
    if not doc_lines:
        return answer

    import uuid

    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    system_prompt = (
        "You are a citation anchoring assistant. Given an answer and a numbered "
        "list of source documents, insert inline citation markers [n] into the "
        "answer at the claims attributable to document n. Only mark claims you "
        "can confidently attribute; leave the rest unmarked. Do not change the "
        "wording of the answer and never invent an attribution. Return only the "
        "answer text with markers, no extra explanation."
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("citation_anchor", False),
    }
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"citation_anchor:{uuid.uuid4().hex}"

    user_content = (
        (f"Query: {query}\n\n" if query else "")
        + "Documents:\n"
        + "\n".join(doc_lines)
        + "\n\nAnswer:\n"
        + answer
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=4096,
            extra_headers=extra_headers,
        )
        content = resp.choices[0].message.content
        return (content or "").strip() or answer
    except Exception as e:
        logger.warning("答案引用锚定 LLM 调用失败: %s", e)
        return answer