#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""内置清单 DTO（§八）。

平台预置、只读；编排时「有哪些任务可选」「这任务能用哪些步骤」就查这两样。
"""

from typing import Any, Dict, Optional

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field


class TaskCatalogItem(BaseModel):
    """内置任务清单的一项。"""

    id: str = Field(description="主键")
    code: str = Field(description="任务标识，如 file_processing_ontology")
    name: str = Field(description="给人看的名字")
    description: Optional[str] = Field(default=None, description="描述")

    class Config:
        extra = "ignore"


class TaskCatalogStepItem(BaseModel):
    """内置步骤清单的一项：步骤的「固有形状」都在这。"""

    id: str = Field(description="主键")
    task_catalog_code: str = Field(description="属于哪个内置任务")
    step_code: str = Field(description="步骤名（编排里 steps[].step_code 引用它）")
    name: str = Field(description="给人看的名字")
    runner_code: str = Field(description="这步交给哪个运行方（runner_definitions.code）")
    max_concurrency: Optional[int] = Field(
        default=None, description="这步最多同时跑几个（运行方并发上限）；不填=不限"
    )
    timeout_seconds: int = Field(default=300, description="默认超时时间（秒），建编排时预填")
    input_schema: Dict[str, Any] = Field(default_factory=dict, description="{ 字段名: FieldSpec }")
    output_schema: Dict[str, Any] = Field(default_factory=dict, description="{ 字段名: FieldSpec }")

    class Config:
        extra = "ignore"


__all__ = ["TaskCatalogItem", "TaskCatalogStepItem"]
