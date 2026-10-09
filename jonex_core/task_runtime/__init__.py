#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务运行 SDK 门面（§4.1 运行方实现、§4.2 运行方调用）。

运行方只需要认识这几样东西：

```python
from jonex_core.capability.response import ApiResponse
from jonex_core.task_runtime import (
    RunnerBase, TaskSubmission, build_task_runtime, get_task_runtime,
    require_execution_context,
)

# 1) 启动入口：每个进程只建一次
build_task_runtime(service_name="<运行方标识>")

# 2) 继承 RunnerBase，只写每个 step 干什么
class MyRunner(RunnerBase):
    runner_code = "<运行方标识>"
    def handlers(self):
        return {"parse": self._parse, "ingest": self._ingest, "compile": self._compile}
    async def _parse(self, execution): ...
    async def remove_task(self, tenant_id, space_id, task_id): ...

# 3) 装配（能力层的 _build_dispatch 里，就一行）
dispatch.update(MyRunner().bind())

# 4) 提交新任务
result = await get_task_runtime().submit(TaskSubmission(definition_id=..., space_id=..., inputs={...}))
```

**门禁一条**：本包（`task_runtime`）不得 import `capabilities.*` —— 它是给接入方用的，
不该把任何一个业务能力的实现拉进来。`tests/unit/task_runtime/test_runtime_imports.py`
用 `sys.modules` 钉住这条。

**依赖方向**：本包依赖 `jonex_core.task_protocol`（协议）+ `jonex_core.capability.response`
（`ApiResponse`）+ §4.4 的 `TaskCenterClient`。协议层住在隔壁、谁也不依赖它。

**两个包的分工**（别混）：

| 包 | 给谁 | 装什么 |
| --- | --- | --- |
| `jonex_core.task_protocol` | 服务端 / 接入方 / 以后的可观测模块 | 跨进程报文、四类 disposition、四类结果与错误码判定 |
| `jonex_core.task_runtime` | **只给接入方（运行方）** | 端口、执行上下文、执行登记、`RunnerBase`、`TaskRuntime` |

⚠️ 端口需要 `ApiResponse`，所以端口住在**本包**而不是协议层 —— 协议层明令不得 import
`jonex_core.capability.*`（那个包的 `__init__.py` 会把能力注册中心拉进去）。
"""

from jonex_core.task_protocol.models import ExecutionDispatch, TaskSubmission

from .client import LegacyControlPlaneAdapter
from .context import (
    ExecutionContext,
    ExecutionScope,
    acquire_execution,
    get_execution_context,
    require_execution_context,
)
from .ports import ControlPlaneClient, RunnerPort
from .registration import ClaimOutcome, ExecutionRegistry, InMemoryExecutionRegistry
from .runner import RunnerBase
from .runtime import TaskRuntime, TerminalReport, build_task_runtime, get_task_runtime

__all__ = [
    "ClaimOutcome",
    "ControlPlaneClient",
    "ExecutionContext",
    "ExecutionDispatch",
    "ExecutionRegistry",
    "ExecutionScope",
    "InMemoryExecutionRegistry",
    "LegacyControlPlaneAdapter",
    "RunnerBase",
    "RunnerPort",
    "TaskRuntime",
    "TaskSubmission",
    "TerminalReport",
    "acquire_execution",
    "build_task_runtime",
    "get_execution_context",
    "get_task_runtime",
    "require_execution_context",
]
