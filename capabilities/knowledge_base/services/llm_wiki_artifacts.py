# [jonex] 新增文件 — llm-wiki 解析产物 → 解析结果页「原文视图」块
"""[jonex] llm-wiki（openkb / jonex_wiki）解析产物 → 解析结果页的原文视图块。

为什么需要这个模块
------------------
- openkb：atomic-rag 写 ``parsed/{doc}/content.md``（重组后的 markdown）；
- jonex_wiki：74b76989 起改产 ``parsed/{doc}/content_list.json``（MinerU 结构化
  数组，对齐独立部署 parse task 的 MINERU 产物），共享卷上不再有 content.md。

``DocumentService._get_llm_wiki_chunks`` 原先只读 content.md，于是 jonex_wiki 文档
的解析结果页恒为 ``total: 0``（页面空白）。本模块把两种产物统一投影成「块列表」，
**不改引擎产物契约**（jonex_wiki 仍只写 content_list.json），也不要求 reparse：
存量已落盘的 content_list.json 立即可读。

读取优先级（与 ``reconciliation_service._jonex_parsed_markdown_rel`` 的既有约定
一致，只是把 jonex_wiki 的首选换成结构化产物）::

    jonex_wiki : content_list.json → raw.md → content.md
    openkb     : content.md

渲染口径
--------
content_list.json 的渲染规则**逐条复刻**旧 content.md 的组装逻辑
（``Reference/Rag-anything/raganything/service/task_manager.py:1638-1698``）：
先按 item 生成片段 → ``"\\n\\n".join(...)`` → 按空行切块。这样块切分与
``chunk_id`` 编号（``{doc}#{kb_type}-{i}``，前端按序号选中）与历史产物逐位一致。

- ``table`` 的 ``table_body``（MinerU HTML）**原样保留**：前端
  ``MarkdownContent`` 的 sanitize 白名单放行 table/thead/tbody/tr/td/th，
  HTML 表即渲染成表格 —— 这就是「表格形状」的来源。
- ``image`` 只产出「VLM 描述 + ``![caption](img_path)``」；新产物写盘时已把
  不存在的图片路径置空（``task_manager.py:1759-1761``），此时该 item 不产片段，
  与旧 content.md 行为一致（旧代码也只在文件存在时才追加图片引用）。
- 未知 type / 空片段一律丢弃；``_jonex_raw_markdown`` 等 stash 字段不参与渲染。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 产物候选（按 kb_type 分派；元素即 parsed/{doc}/ 下的文件名）
_JONEX_WIKI_ARTIFACTS: tuple[str, ...] = ("content_list.json", "raw.md", "content.md")
_OPENKB_ARTIFACTS: tuple[str, ...] = ("content.md",)

# content_list item 里的候选图片路径字段（与 _write_jonex_wiki_raw_artifact:1743 同口径）
_IMAGE_PATH_FIELDS: tuple[str, ...] = (
    "img_path", "image_path", "table_img_path", "equation_img_path",
)

# 媒体类型 → 文本字段回退顺序（旧代码：description or transcription）
_MEDIA_TYPES: tuple[str, ...] = ("video", "audio")


def llm_wiki_artifact_dir(document_id: str) -> Path:
    """[jonex] 共享卷上该文档的 parsed 产物目录：{KB_INPUT_DIR}/parsed/{document_id}。"""
    inputs_root = os.getenv("KB_INPUT_DIR", "/app/inputs")
    return Path(inputs_root) / "parsed" / document_id


def candidate_artifacts(kb_type: str) -> tuple[str, ...]:
    """[jonex] 按 kb_type 返回产物候选文件名（有序）。"""
    return _JONEX_WIKI_ARTIFACTS if kb_type == "jonex_wiki" else _OPENKB_ARTIFACTS


def _as_text(value: Any) -> str:
    """MinerU 字段可能是 str / list[str] / None，统一成去空白字符串。

    list 用空格连接（与旧代码 ``" ".join(str(c) for c in caption)`` 一致）。
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v is not None).strip()
    return str(value).strip()


def _first_non_empty(*values: Any) -> Any:
    """返回第一个真值（旧代码的 ``a or b or ""`` 语义）。"""
    for value in values:
        if value:
            return value
    return ""


def flatten_content_items(payload: Any) -> list[dict]:
    """摊平 MinerU content-list 的嵌套数组，返回 item dict 列表。

    MinerU-HTML 会把 items 包进一层额外数组（``[[{...}]]``）。与引擎侧
    ``jonex_wiki/domain/parsing/mineru/common.py:280`` 同口径：只沿 list 下钻
    收集 dict，不进入 item 的字段（避免把 caption 列表误当顶层 items）。
    """
    flat: list[dict] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            flat.append(node)

    visit(payload)
    return flat


def _table_body_text(item: dict) -> str:
    """表格正文：``table_body``（HTML）优先，回退 ``text``；list 形态按行/列连接。"""
    body = _first_non_empty(item.get("table_body"), item.get("text"))
    if isinstance(body, list):
        return "\n".join(
            "\t".join(str(cell) for cell in row if cell is not None)
            for row in body if row
        )
    return _as_text(body)


def _image_ref(item: dict) -> str:
    """图片引用路径。

    新产物（``_write_jonex_wiki_raw_artifact:1743-1754``）已把路径相对化为
    ``images/{name}`` 并把不存在的图片置空，这里只做取值。
    """
    for field in _IMAGE_PATH_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\\", "/")
    return ""


def item_to_md_part(item: dict) -> str:
    """单个 content_list item → md 片段（逐条对齐 task_manager.py:1638-1698）。

    空片段（未知类型 / 无内容）返回 ""，由调用方丢弃。
    """
    item_type = item.get("type", "text")

    if item_type == "text":
        return _as_text(item.get("text"))

    if item_type == "equation":
        text = _as_text(item.get("text"))
        return f"$$\n{text}\n$$" if text else ""

    if item_type == "table":
        caption = _as_text(
            _first_non_empty(item.get("table_caption"), item.get("caption"))
        )
        body = _table_body_text(item)
        # 注意：这里不把 HTML 表格转 markdown —— 解析结果页要的是原文形状，
        # 转 markdown 由引擎侧检索投影负责（source_prep/blocks.py:323）。
        return ((caption + "\n") if caption else "") + body

    if item_type == "image":
        parts: list[str] = []
        description = _as_text(item.get("description"))
        if description:
            parts.append(description)
        img = _image_ref(item)
        if img:
            caption = _as_text(
                _first_non_empty(item.get("image_caption"), item.get("img_caption"))
            )
            parts.append(f"![{caption}]({img})")
        return "\n\n".join(parts)

    if item_type in _MEDIA_TYPES:
        return _as_text(
            _first_non_empty(item.get("description"), item.get("transcription"))
        )

    return ""


def content_list_to_markdown(payload: Any) -> str:
    """content_list（MinerU 结构化数组）→ 与旧 content.md 同形的 markdown 文本。"""
    parts = [
        part for part in (item_to_md_part(it) for it in flatten_content_items(payload))
        if part.strip()
    ]
    if not parts:
        return ""
    return "\n\n".join(parts).strip() + "\n"


def split_markdown_blocks(text: str) -> list[str]:
    """按空行切块（与旧 ``_get_llm_wiki_chunks`` 的切分口径完全一致）。"""
    return [block.strip() for block in text.split("\n\n") if block.strip()]


def load_llm_wiki_blocks(kb_type: str, document_id: str) -> tuple[list[str], str]:
    """读共享卷 parsed 产物 → 原文视图块列表。

    返回 ``(blocks, artifact_name)``；两者同时为空表示共享卷上没有可用产物
    （调用方据此返回 ``total: 0`` 契约）。单个候选读取/解析失败只告警并继续
    下一个候选，绝不抛错——解析结果页不能因为产物损坏变 500。
    """
    doc_dir = llm_wiki_artifact_dir(document_id)
    for name in candidate_artifacts(kb_type):
        path = doc_dir / name
        if not path.is_file():
            continue
        try:
            if path.suffix == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                blocks = split_markdown_blocks(content_list_to_markdown(payload))
            else:
                blocks = split_markdown_blocks(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "[jonex] llm-wiki 解析产物读取失败，尝试下一个候选: %s",
                path, exc_info=True,
            )
            continue
        if blocks:
            logger.info(
                "[jonex] llm-wiki 原文视图产物命中: kb_type=%s doc=%s artifact=%s blocks=%d",
                kb_type, document_id, name, len(blocks),
            )
            return blocks, name
        logger.warning("[jonex] llm-wiki 解析产物无可用块: %s", path)
    return [], ""


__all__ = [
    "candidate_artifacts",
    "content_list_to_markdown",
    "flatten_content_items",
    "item_to_md_part",
    "llm_wiki_artifact_dir",
    "load_llm_wiki_blocks",
    "split_markdown_blocks",
]
