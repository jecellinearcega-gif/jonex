#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心 service 层导出。

四块服务 + 两件共享工具：

- `TaskDefinitionService`：定义与版本（建定义、改草稿、发版，§4.3 `def.*`）；
- `TaskTriggerService`：触发（建 / 启停 / 手动触发一次，§4.3 `trig.*`）；
- `TaskService`：任务执行的心脏（起任务、收上报、填值、判定、汇总、重跑，§4.3 `task.*`）；
- `TaskScheduler`：后台循环（扫能跑的 + 超时巡检，§7.4、§7.5）。

`flow_validator` / `expressions` 是发版校验与运行期填值共用的工具：同一份 schema 语义，
不允许「发版时说行、跑起来说不通」。
"""

from .flow_validator import validate_flow_config
from .scheduler_service import (
    TaskScheduler,
    get_scheduler,
    start_scheduler,
    stop_scheduler,
)
from .task_definition_service import TaskDefinitionService
from .task_service import RunContext, TaskService
from .task_trigger_service import TaskTriggerService

__all__ = [
    "RunContext",
    "TaskDefinitionService",
    "TaskScheduler",
    "TaskService",
    "TaskTriggerService",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
    "validate_flow_config",
]
