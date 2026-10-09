#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""运行方 DTO（§八）：运行方清单项 + 「管理方 → 运行方」三个出站报文（§4.1）。

**三个出站报文的定义已经搬到 `jonex_core/task_protocol/models.py`** —— 它们跨进程，
运行方（以及以后的可观测模块）也要用，定义留在 `capabilities/` 里运行方就够不着。
这里改成 re-export，**不是第二份定义**：两处各定义一份会让「任务中心用它造对象、
运行方用它做 isinstance」两边都拿到不同的类，谁都不报错、语义已经错位。

`RunnerItem` **留在原地** —— 它是管理方自己的运行方清单项，不跨进程，运行方不需要知道。
"""

from jonex_core.task_protocol.models import (
    ExecutionDispatch,
    ExecutionEnvelope,
    RemoveTaskDispatch,
    TaskStoppedDispatch,
)

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field


class RunnerItem(BaseModel):
    """运行方清单的一项。"""

    # `id` 与 `code` **取同一个值（逻辑键）**：`runner.get` 只认逻辑键，`id` 要是回
    # DB 主键（`runner-knowledge-base` 那种），「list 里挑一条再 get 详情」这条路就断了。
    # 于是两个接口的 `id` 语义统一到逻辑键，谁也不需要知道主键长什么样。
    id: str = Field(description="主键（= code，逻辑键）")
    code: str = Field(description="运行方唯一标识，如 knowledge_base")
    name: str = Field(description="给人看的名字")
    capability_id: str = Field(description="运行方对外能力 ID（对应管理方那边的 client）")

    class Config:
        extra = "ignore"


__all__ = [
    "ExecutionDispatch",
    "ExecutionEnvelope",
    "RemoveTaskDispatch",
    "TaskStoppedDispatch",
    "RunnerItem",
]
