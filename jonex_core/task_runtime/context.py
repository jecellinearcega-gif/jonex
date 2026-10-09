#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行上下文：把「现在在跑哪一次执行」绑进当前异步任务（§4.1 ①）。

范式照 `jonex_core/common/tenant.py` 的 `TenantContext`：`contextvars` + `Token` +
`try/finally: reset(token)` —— 异步安全，并发执行不串台。

```python
from jonex_core.task_runtime import acquire_execution, require_execution_context

async def execute(self, execution):
    async with acquire_execution(execution):
        ctx = require_execution_context()      # handler 里随手取，不用一层层传参
        key = f"task-execution:{ctx.execution_id}"
        ...
```

**为什么是 `async with` 而不是 `with`**：`acquire_execution` 将来还要做「往运行方自己的
库条件更新『当前执行是这个 execution_id』」那一步（参考文档 §4.2 的 `pre_claim`），
那是 DB 操作。现在就做成异步，**那一步加进来时所有运行方代码一个字都不用改**。

⚠️ **`with acquire_execution(x):` 会当场 `TypeError`** —— 这是故意的。本模块用**类式**
上下文管理器（只实现 `__aenter__` / `__aexit__`，不实现 `__enter__`），不用
`@asynccontextmanager`：后者被误用时只是拿到一个 `_AsyncGeneratorContextManager`，
**什么都不执行、也不报错**（不像 coroutine 有 "never awaited" 警告），上下文压根没绑上，
错误会在很远的地方以「缺上下文」的形式冒出来。

⚠️ **ContextVar 的继承语义**（预期行为，但会踩）：`asyncio.create_task` 起的子任务
**拷贝**当时的上文，所以 CM 退出后子任务仍持有旧值；反过来，CM **外面**创建、之后才
进 CM 的任务拿不到。**后台巡检那条路本来就没有这一帧** —— 它该走
`TaskRuntime.report_terminal(carrier, ...)` 显式传凭据，不要来取上下文。
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional

from jonex_core.task_protocol.errors import ExecutionContextMissingError
from jonex_core.task_protocol.models import ExecutionDispatch

_execution_ctx: ContextVar[Optional["ExecutionContext"]] = ContextVar(
    "jonex_task_execution", default=None
)


@dataclass(frozen=True)
class ExecutionContext:
    """一次执行的定位信息 —— 就是从派活报文里挑出「干活要用的那几个」。

    定位一条子任务是**三件套缺一不可**（`tenant_id` + `space_id` + `execution_id`，
    §7.4）：管理方按这三个找到唯一那一行。`step_code` 和 `task_id` 不参与定位，
    但运行方干活时要用（分流、按任务落数据）。
    """

    tenant_id: str
    space_id: str
    task_id: str
    step_code: str
    execution_id: str

    @classmethod
    def from_dispatch(cls, execution: ExecutionDispatch) -> "ExecutionContext":
        """从派活报文里取。"""
        return cls(
            tenant_id=execution.tenant_id,
            space_id=execution.space_id,
            task_id=execution.task_id,
            step_code=execution.step_code,
            execution_id=execution.execution_id,
        )


class ExecutionScope:
    """`acquire_execution()` 返回的上下文管理器（**类式**，见模块 docstring）。

    只实现 `__aenter__` / `__aexit__`：误写成同步 `with` 会当场 `TypeError`，
    而不是静默什么都不做。
    """

    __slots__ = ("_context", "_token")

    def __init__(self, context: ExecutionContext) -> None:
        self._context = context
        self._token: Optional[Token[Optional[ExecutionContext]]] = None

    async def __aenter__(self) -> ExecutionContext:
        self._token = _execution_ctx.set(self._context)
        return self._context

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # 无论 handler 正常返回还是抛异常，都要把上下文收回 —— 否则异常之后残留的
        # 上下文会被同一异步任务里的后续代码误用（拿到一次早就结束的执行）。
        if self._token is not None:
            _execution_ctx.reset(self._token)
            self._token = None
        return False  # 不吞异常


def acquire_execution(execution: ExecutionDispatch) -> ExecutionScope:
    """把这次执行绑进当前异步任务；`async with acquire_execution(execution):`。

    **必须 `async with`**（见模块 docstring）。
    """
    return ExecutionScope(ExecutionContext.from_dispatch(execution))


def get_execution_context() -> Optional[ExecutionContext]:
    """取当前执行上下文；不在作用域里就是 `None`（不抛）。"""
    return _execution_ctx.get()


def require_execution_context() -> ExecutionContext:
    """取当前执行上下文；不在作用域里抛 `ExecutionContextMissingError`。

    运行方的 handler 里用它拿 `execution_id` 拼幂等键（`task-execution:{execution_id}`）、
    拿 `tenant_id` / `space_id` 落数据 —— 比一层层传参干净。
    """
    context = _execution_ctx.get()
    if context is None:
        raise ExecutionContextMissingError()
    return context


__all__ = [
    "ExecutionContext",
    "ExecutionScope",
    "acquire_execution",
    "get_execution_context",
    "require_execution_context",
]
