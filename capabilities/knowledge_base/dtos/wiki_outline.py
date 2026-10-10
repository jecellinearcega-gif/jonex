#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Wiki 阅读目录 DTO（Pydantic v1 语法）。

目录只对 llm-wiki（kb_type=jonex_wiki）开放；请求体**不含 tenant_id**
（租户由 capability 请求上下文注入，见 CLAUDE.md「编辑要求」）。
"""
from typing import Optional

from pydantic import BaseModel, Field

# 目录层级上限（与 outline 校验口径一致，见 wiki_outline_service）
MAX_INSTRUCTION_LEN = 2000


class DraftWikiOutlineRequest(BaseModel):
    """LLM 起草目录请求。"""

    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    # 覆盖默认模型（缺省走 ONTOLOGY_LLM_MODEL）
    model: Optional[str] = Field(default=None, max_length=128)
    # 起草时的额外要求（可选，例如「按季度分章」），与内置规则叠加
    extra_instruction: Optional[str] = Field(default=None, max_length=MAX_INSTRUCTION_LEN)
    # 未归类页面兜底分组的标题（前端按语言传入；缺省中文）
    unclassified_title: Optional[str] = Field(default=None, max_length=64)


class AdjustWikiOutlineRequest(BaseModel):
    """LLM 按指令调整现有目录请求。"""

    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    instruction: str = Field(..., min_length=1, max_length=MAX_INSTRUCTION_LEN)
    model: Optional[str] = Field(default=None, max_length=128)
    # 前端持有的目录版本（仅用于并发提示，不做 CAS 拒绝）
    expected_version: Optional[int] = Field(default=None, ge=0)
    unclassified_title: Optional[str] = Field(default=None, max_length=64)
