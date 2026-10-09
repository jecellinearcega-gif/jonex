#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""运行方侧（flows）：内置运行方清单 + 运行方适配器。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §四、§九。
"""

from .registry import RunnerRegistry
from .runner_adapter import (
    RUNNER_EXECUTE_ACTION,
    RUNNER_REMOVE_TASK_ACTION,
    RunnerAdapter,
)

__all__ = [
    "RUNNER_EXECUTE_ACTION",
    "RUNNER_REMOVE_TASK_ACTION",
    "RunnerAdapter",
    "RunnerRegistry",
]
